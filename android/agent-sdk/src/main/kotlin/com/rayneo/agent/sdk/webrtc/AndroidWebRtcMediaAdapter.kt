package com.rayneo.agent.sdk.webrtc

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.util.Log
import androidx.core.content.ContextCompat
import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import com.rayneo.agent.sdk.model.ComputingSession
import com.rayneo.agent.sdk.transport.LocalProcessedVideo
import com.rayneo.agent.sdk.transport.MediaOffloadAdapter
import com.rayneo.agent.sdk.transport.PreparedMediaConnection
import com.rayneo.agent.sdk.transport.VideoTrack
import com.rayneo.agent.sdk.transport.VideoUploadHandle
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeout
import org.webrtc.Camera2Enumerator
import org.webrtc.CameraVideoCapturer
import org.webrtc.DataChannel
import org.webrtc.DefaultVideoDecoderFactory
import org.webrtc.DefaultVideoEncoderFactory
import org.webrtc.EglBase
import org.webrtc.IceCandidate
import org.webrtc.MediaConstraints
import org.webrtc.MediaStream
import org.webrtc.MediaStreamTrack
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.RtpReceiver
import org.webrtc.RtpTransceiver
import org.webrtc.SdpObserver
import org.webrtc.SessionDescription
import org.webrtc.SurfaceTextureHelper
import org.webrtc.VideoCapturer
import org.webrtc.VideoSink
import java.util.concurrent.CopyOnWriteArraySet
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/** Android WebRTC implementation used internally by AgentSdk media APIs. */
internal class AndroidWebRtcMediaAdapter(context: Context) : MediaOffloadAdapter {
    private val appContext = context.applicationContext
    private val eglDelegate = lazy(LazyThreadSafetyMode.SYNCHRONIZED) { EglBase.create() }
    private val egl: EglBase by eglDelegate
    private val peers = CopyOnWriteArraySet<PeerConnection>()
    private val closed = AtomicBoolean(false)
    private val factoryDelegate = lazy(LazyThreadSafetyMode.SYNCHRONIZED, ::createFactory)
    private val factory: PeerConnectionFactory by factoryDelegate

    private fun createFactory(): PeerConnectionFactory {
        // AgentSdk creates this adapter before initialize() establishes the VPN. Delay the
        // native media stack until the first media call so interface enumeration happens
        // after the SDK-managed CONNECT-IP TUN interface exists.
        Log.i(TAG, "Creating WebRTC factory after CONNECT-IP VPN initialization")
        if (factoryInitialized.compareAndSet(false, true)) {
            PeerConnectionFactory.initialize(
                PeerConnectionFactory.InitializationOptions.builder(appContext)
                    .createInitializationOptions(),
            )
        }
        val options = PeerConnectionFactory.Options().apply {
            // Keep libwebrtc from binding ICE sockets to a ConnectivityManager network.
            // AgentVpnService is non-bypassable, so ordinary unprotected sockets follow the
            // CONNECT-IP route and the native interface enumeration can expose the UE/TUN IP.
            // This is the same network model used by the previously device-verified media path.
            disableNetworkMonitor = true
        }
        return PeerConnectionFactory.builder()
            .setOptions(options)
            .setVideoEncoderFactory(
                DefaultVideoEncoderFactory(egl.eglBaseContext, true, true),
            )
            // Byte-buffer decoder output can be consumed by an App renderer without
            // requiring the App to share the SDK's private EGL context.
            .setVideoDecoderFactory(DefaultVideoDecoderFactory(null))
            .createPeerConnectionFactory()
    }

    override fun supportsVideoCodec(codec: String): Boolean {
        val wanted = codec.removePrefix("video/")
        return factory.getRtpSenderCapabilities(MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO)
            .codecs.any { it.name.equals(wanted, ignoreCase = true) }
    }

    override suspend fun prepareVideoUpload(
        session: ComputingSession,
        cameraId: String,
        width: Int,
        height: Int,
        fps: Int,
        bitrateKbps: Int,
        timeoutSeconds: Double,
    ): PreparedMediaConnection<VideoUploadHandle> {
        ensureOpen()
        if (
            ContextCompat.checkSelfPermission(appContext, Manifest.permission.CAMERA) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            throw AgentSdkException(
                ErrorCode.CAMERA_PERMISSION_DENIED,
                "CAMERA permission must be granted before startVideoUpload",
            )
        }
        val signals = PeerSignals()
        val pc = createPeer(signals)
        val capturer = createCameraCapturer(cameraId)
        val texture = SurfaceTextureHelper.create("AgentSdkVideoCapture", egl.eglBaseContext)
        val source = factory.createVideoSource(false)
        capturer.initialize(texture, appContext, source.capturerObserver)
        val track = factory.createVideoTrack("source-${session.computeServiceSessionId}", source)
        var phase = "initialize"
        fun enterPhase(next: String) {
            phase = next
            Log.i(TAG, "Preparing producer Offer session=${session.computeServiceSessionId} phase=$phase")
        }
        try {
            enterPhase("start camera capture")
            capturer.startCapture(width, height, fps)
            enterPhase("add send-only video transceiver")
            val transceiver = pc.addTransceiver(
                track,
                RtpTransceiver.RtpTransceiverInit(
                    RtpTransceiver.RtpTransceiverDirection.SEND_ONLY,
                ),
            ) ?: mediaFailure("unable to add the send-only video transceiver")
            enterPhase("apply video codec preference")
            setCodecPreference(transceiver, session.connectionParameters.videoCodec)
            val parameters = transceiver.sender.parameters
            parameters.encodings.forEach { it.maxBitrateBps = bitrateKbps * 1_000 }
            if (!transceiver.sender.setParameters(parameters)) {
                mediaFailure("unable to configure the video bitrate")
            }
            enterPhase("create SDP Offer")
            val offer = pc.awaitCreateOffer()
            enterPhase("set local SDP Offer")
            pc.awaitSetLocal(offer)
            enterPhase("gather ICE candidates")
            awaitIceComplete(pc, signals, timeoutSeconds)
            val local = pc.localDescription
                ?: mediaFailure("local WebRTC Offer is unavailable")
            val resources = ProducerResources(pc, capturer, texture, source, track)
            enterPhase("publish CONNECT-IP ICE candidates")
            return PreparedUpload(
                resources,
                publishUserPlaneOffer(
                    local.description,
                    session.networkBinding.ueIpv4,
                    signals.localCandidates,
                ),
                signals,
            )
        } catch (error: Throwable) {
            releaseProducer(ProducerResources(pc, capturer, texture, source, track))
            if (error is CancellationException) throw error
            mediaFailure("failed to prepare the camera WebRTC Offer during $phase", error)
        }
    }

    override suspend fun prepareProcessedVideo(
        session: ComputingSession,
        timeoutSeconds: Double,
    ): PreparedMediaConnection<LocalProcessedVideo> {
        ensureOpen()
        val signals = PeerSignals()
        val pc = createPeer(signals)
        var phase = "initialize"
        fun enterPhase(next: String) {
            phase = next
            Log.i(TAG, "Preparing consumer Offer session=${session.computeServiceSessionId} phase=$phase")
        }
        try {
            enterPhase("add receive-only video transceiver")
            val transceiver = pc.addTransceiver(
                MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO,
                RtpTransceiver.RtpTransceiverInit(
                    RtpTransceiver.RtpTransceiverDirection.RECV_ONLY,
                ),
            ) ?: mediaFailure("unable to add the receive-only video transceiver")
            enterPhase("apply video codec preference")
            setCodecPreference(transceiver, session.connectionParameters.videoCodec)
            enterPhase("create SDP Offer")
            val offer = pc.awaitCreateOffer()
            enterPhase("set local SDP Offer")
            pc.awaitSetLocal(offer)
            enterPhase("gather ICE candidates")
            awaitIceComplete(pc, signals, timeoutSeconds)
            val local = pc.localDescription
                ?: mediaFailure("local WebRTC Offer is unavailable")
            enterPhase("publish CONNECT-IP ICE candidates")
            return PreparedConsumer(
                pc,
                publishUserPlaneOffer(
                    local.description,
                    session.networkBinding.ueIpv4,
                    signals.localCandidates,
                ),
                signals,
            )
        } catch (error: Throwable) {
            closePeer(pc)
            if (error is CancellationException) throw error
            mediaFailure("failed to prepare the processed-video WebRTC Offer during $phase", error)
        }
    }

    override suspend fun close() {
        if (!closed.compareAndSet(false, true)) return
        peers.toList().forEach(::closePeer)
        peers.clear()
        if (factoryDelegate.isInitialized()) factory.dispose()
        if (eglDelegate.isInitialized()) egl.release()
    }

    private inner class PreparedUpload(
        private val resources: ProducerResources,
        override val offerSdp: String,
        private val signals: PeerSignals,
    ) : PreparedMediaConnection<VideoUploadHandle> {
        private val handedOff = AtomicBoolean(false)

        override suspend fun applyAnswer(
            answerSdp: String,
            timeoutSeconds: Double,
        ): VideoUploadHandle {
            resources.pc.awaitSetRemote(
                SessionDescription(SessionDescription.Type.ANSWER, answerSdp),
            )
            awaitConnected(resources.pc, signals, timeoutSeconds)
            handedOff.set(true)
            return AndroidVideoUploadHandle(resources)
        }

        override suspend fun abort() {
            if (!handedOff.get()) releaseProducer(resources)
        }
    }

    private inner class PreparedConsumer(
        private val pc: PeerConnection,
        override val offerSdp: String,
        private val signals: PeerSignals,
    ) : PreparedMediaConnection<LocalProcessedVideo> {
        private val handedOff = AtomicBoolean(false)

        override suspend fun applyAnswer(
            answerSdp: String,
            timeoutSeconds: Double,
        ): LocalProcessedVideo {
            pc.awaitSetRemote(SessionDescription(SessionDescription.Type.ANSWER, answerSdp))
            val track = withTimeout((timeoutSeconds * 1_000).toLong()) {
                signals.remoteVideo.await()
            }
            awaitConnected(pc, signals, timeoutSeconds)
            handedOff.set(true)
            return AndroidProcessedVideo(pc, track)
        }

        override suspend fun abort() {
            if (!handedOff.get()) closePeer(pc)
        }
    }

    private inner class AndroidVideoUploadHandle(
        private val resources: ProducerResources,
    ) : VideoUploadHandle {
        private val stopped = AtomicBoolean(false)
        override val trackId: String get() = resources.track.id()
        override val state: String
            get() = when {
                stopped.get() -> "STOPPED"
                resources.track.enabled() -> "RUNNING"
                else -> "PAUSED"
            }

        override suspend fun pause() {
            if (!stopped.get()) resources.track.setEnabled(false)
        }

        override suspend fun resume() {
            if (!stopped.get()) resources.track.setEnabled(true)
        }

        override suspend fun stop() {
            if (stopped.compareAndSet(false, true)) releaseProducer(resources)
        }
    }

    private inner class AndroidProcessedVideo(
        private val pc: PeerConnection,
        delegate: org.webrtc.VideoTrack,
    ) : LocalProcessedVideo {
        private val closed = AtomicBoolean(false)
        override val track: VideoTrack = AndroidVideoTrack(delegate)
        override suspend fun close() {
            if (closed.compareAndSet(false, true)) closePeer(pc)
        }
    }

    private class AndroidVideoTrack(
        private val delegate: org.webrtc.VideoTrack,
    ) : VideoTrack {
        override val trackId: String get() = delegate.id()
        override fun addSink(sink: Any) {
            require(sink is VideoSink) { "Android WebRTC VideoSink is required" }
            delegate.addSink(sink)
        }
        override fun removeSink(sink: Any) {
            require(sink is VideoSink) { "Android WebRTC VideoSink is required" }
            delegate.removeSink(sink)
        }
    }

    private data class ProducerResources(
        val pc: PeerConnection,
        val capturer: VideoCapturer,
        val texture: SurfaceTextureHelper,
        val source: org.webrtc.VideoSource,
        val track: org.webrtc.VideoTrack,
    ) {
        val released = AtomicBoolean(false)
    }

    private class PeerSignals {
        val iceComplete = CompletableDeferred<Unit>()
        val connectionChanged = CompletableDeferred<Unit>()
        val remoteVideo = CompletableDeferred<org.webrtc.VideoTrack>()
        val localCandidates = CopyOnWriteArrayList<GatheredIceCandidate>()
    }

    private fun createPeer(signals: PeerSignals): PeerConnection {
        val configuration = PeerConnection.RTCConfiguration(emptyList()).apply {
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            continualGatheringPolicy = PeerConnection.ContinualGatheringPolicy.GATHER_ONCE
            disableIPv6OnWifi = true
        }
        return factory.createPeerConnection(configuration, peerObserver(signals))
            ?.also { peers += it }
            ?: mediaFailure("unable to create a WebRTC PeerConnection")
    }

    private fun peerObserver(signals: PeerSignals): PeerConnection.Observer =
        object : PeerConnection.Observer {
            override fun onSignalingChange(state: PeerConnection.SignalingState) = Unit
            override fun onIceConnectionChange(state: PeerConnection.IceConnectionState) = Unit
            override fun onConnectionChange(state: PeerConnection.PeerConnectionState) {
                if (
                    state in setOf(
                        PeerConnection.PeerConnectionState.CONNECTED,
                        PeerConnection.PeerConnectionState.FAILED,
                        PeerConnection.PeerConnectionState.CLOSED,
                    ) && !signals.connectionChanged.isCompleted
                ) signals.connectionChanged.complete(Unit)
            }
            override fun onIceConnectionReceivingChange(receiving: Boolean) = Unit
            override fun onIceGatheringChange(state: PeerConnection.IceGatheringState) {
                if (
                    state == PeerConnection.IceGatheringState.COMPLETE &&
                    !signals.iceComplete.isCompleted
                ) signals.iceComplete.complete(Unit)
            }
            override fun onIceCandidate(candidate: IceCandidate) {
                val gathered = GatheredIceCandidate(candidate.sdp, candidate.adapterType)
                signals.localCandidates += gathered
                Log.i(
                    TAG,
                    "ICE candidate gathered adapter=${candidate.adapterType.name} " +
                        "address=${candidateAddress(candidate.sdp) ?: "unknown"} " +
                        "type=${candidateType(candidate.sdp) ?: "unknown"}",
                )
            }
            override fun onIceCandidatesRemoved(candidates: Array<IceCandidate>) = Unit
            override fun onAddStream(stream: MediaStream) = Unit
            override fun onRemoveStream(stream: MediaStream) = Unit
            override fun onDataChannel(channel: DataChannel) = Unit
            override fun onRenegotiationNeeded() = Unit
            override fun onAddTrack(receiver: RtpReceiver, streams: Array<out MediaStream>) = Unit
            override fun onTrack(transceiver: RtpTransceiver) {
                val track = transceiver.receiver.track() as? org.webrtc.VideoTrack ?: return
                if (!signals.remoteVideo.isCompleted) signals.remoteVideo.complete(track)
            }
        }

    private suspend fun awaitIceComplete(
        pc: PeerConnection,
        signals: PeerSignals,
        timeoutSeconds: Double,
    ) {
        if (pc.iceGatheringState() == PeerConnection.IceGatheringState.COMPLETE) return
        withTimeout((timeoutSeconds * 1_000).toLong()) { signals.iceComplete.await() }
    }

    private suspend fun awaitConnected(
        pc: PeerConnection,
        signals: PeerSignals,
        timeoutSeconds: Double,
    ) {
        if (pc.connectionState() != PeerConnection.PeerConnectionState.CONNECTED) {
            withTimeout((timeoutSeconds * 1_000).toLong()) {
                signals.connectionChanged.await()
            }
        }
        if (pc.connectionState() != PeerConnection.PeerConnectionState.CONNECTED) {
            mediaFailure("WebRTC connection entered ${pc.connectionState().name}")
        }
    }

    private fun setCodecPreference(transceiver: RtpTransceiver, requested: String?) {
        val codec = requested ?: return
        val wanted = codec.removePrefix("video/")
        val codecs = factory
            .getRtpSenderCapabilities(MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO)
            .codecs.filter { it.name.equals(wanted, ignoreCase = true) }
        if (codecs.isEmpty()) mediaFailure("required video codec is not supported: $codec")
        transceiver.setCodecPreferences(codecs).throwError()
    }

    private fun createCameraCapturer(cameraId: String): CameraVideoCapturer {
        val enumerator = Camera2Enumerator(appContext)
        val names = enumerator.deviceNames.toList()
        val selected = names.firstOrNull { it == cameraId }
            ?: cameraId.toIntOrNull()?.let(names::getOrNull)
            ?: names.firstOrNull { enumerator.isBackFacing(it) }
            ?: names.firstOrNull()
            ?: mediaFailure("no Android camera is available")
        return enumerator.createCapturer(selected, null)
            ?: mediaFailure("unable to open camera $selected")
    }

    private suspend fun releaseProducer(resources: ProducerResources) {
        if (!resources.released.compareAndSet(false, true)) return
        runCatching { resources.capturer.stopCapture() }
        resources.capturer.dispose()
        resources.track.dispose()
        resources.source.dispose()
        resources.texture.dispose()
        closePeer(resources.pc)
    }

    private fun closePeer(pc: PeerConnection) {
        peers -= pc
        pc.close()
    }

    private fun ensureOpen() {
        if (closed.get()) mediaFailure("WebRTC adapter is closed")
    }

    private suspend fun PeerConnection.awaitCreateOffer(): SessionDescription =
        suspendCancellableCoroutine { continuation ->
            createOffer(object : SimpleSdpObserver() {
                override fun onCreateSuccess(value: SessionDescription?) {
                    if (value != null) continuation.resume(value)
                    else continuation.resumeWithException(mediaException("empty SDP Offer"))
                }
                override fun onCreateFailure(error: String?) {
                    continuation.resumeWithException(mediaException(error ?: "createOffer failed"))
                }
            }, MediaConstraints())
        }

    private suspend fun PeerConnection.awaitSetLocal(value: SessionDescription) =
        suspendCancellableCoroutine { continuation ->
            setLocalDescription(object : SimpleSdpObserver() {
                override fun onSetSuccess() = continuation.resume(Unit)
                override fun onSetFailure(error: String?) {
                    continuation.resumeWithException(
                        mediaException(error ?: "setLocalDescription failed"),
                    )
                }
            }, value)
        }

    private suspend fun PeerConnection.awaitSetRemote(value: SessionDescription) =
        suspendCancellableCoroutine { continuation ->
            setRemoteDescription(object : SimpleSdpObserver() {
                override fun onSetSuccess() = continuation.resume(Unit)
                override fun onSetFailure(error: String?) {
                    continuation.resumeWithException(
                        mediaException(error ?: "setRemoteDescription failed"),
                    )
                }
            }, value)
        }

    private open class SimpleSdpObserver : SdpObserver {
        override fun onCreateSuccess(value: SessionDescription?) = Unit
        override fun onSetSuccess() = Unit
        override fun onCreateFailure(error: String?) = Unit
        override fun onSetFailure(error: String?) = Unit
    }

    private companion object {
        const val TAG = "AgentSdkWebRtc"
        val factoryInitialized = AtomicBoolean(false)
    }
}

internal data class GatheredIceCandidate(
    val sdp: String,
    val adapterType: PeerConnection.AdapterType,
)

/**
 * Publishes only candidates carrying the C-02 UE IPv4 address.
 *
 * The factory deliberately disables Android's libwebrtc network monitor so its sockets remain
 * ordinary, unprotected application sockets routed by AgentVpnService. Adapter labels are not
 * authoritative in that mode; the address assigned to the TUN is the user-plane identity.
 */
internal fun publishUserPlaneOffer(
    sdp: String,
    ueIpv4: String,
    gatheredCandidates: List<GatheredIceCandidate>,
): String {
    val observed = gatheredCandidates.groupBy { normalizeCandidate(it.sdp) }
    val selectedAdapters = linkedSetOf<PeerConnection.AdapterType>()
    var selectedCount = 0
    val lines = sdp.split("\r\n")
    val published = lines.mapNotNull { line ->
        if (!line.startsWith("a=candidate:")) return@mapNotNull line
        val records = observed[normalizeCandidate(line)].orEmpty()
        val address = candidateAddress(line)
        val selected = records.firstOrNull { address == ueIpv4 }
        if (selected == null) return@mapNotNull null
        selectedAdapters += selected.adapterType
        selectedCount += 1
        line
    }
    if (selectedCount == 0) {
        val summary = gatheredCandidates
            .groupingBy { it.adapterType.name }
            .eachCount()
            .entries
            .sortedBy { it.key }
            .joinToString { "${it.key}=${it.value}" }
            .ifBlank { "none" }
        throw AgentSdkException(
            ErrorCode.MEDIA_NEGOTIATION_FAILED,
            "WebRTC did not gather an ICE candidate for the C-02 UE IPv4 $ueIpv4 " +
                "through the SDK VPN/TUN (observed adapters: $summary)",
        )
    }
    Log.i(
        "AgentSdkWebRtc",
        "Publishing $selectedCount user-plane ICE candidate(s) for UE IPv4 $ueIpv4 " +
            "adapters=${selectedAdapters.joinToString { it.name }}",
    )
    return published.joinToString("\r\n")
}

private fun normalizeCandidate(line: String): String =
    line.trim().removePrefix("a=")

internal fun candidateAddress(line: String): String? =
    candidateParts(line).getOrNull(4)

internal fun candidateType(line: String): String? =
    candidateParts(line).getOrNull(7)

private fun candidateParts(line: String): List<String> =
    line.trim().split(Regex("\\s+"))

private fun mediaFailure(message: String, cause: Throwable? = null): Nothing {
    val detailedMessage = if (cause == null) {
        message
    } else {
        "$message: ${cause.message?.takeIf(String::isNotBlank) ?: cause::class.java.simpleName}"
    }
    Log.e("AgentSdkWebRtc", detailedMessage, cause)
    throw AgentSdkException(
        ErrorCode.MEDIA_NEGOTIATION_FAILED,
        detailedMessage,
        cause = cause,
    )
}

private fun mediaException(message: String, cause: Throwable? = null) =
    AgentSdkException(ErrorCode.MEDIA_NEGOTIATION_FAILED, message, cause = cause)
