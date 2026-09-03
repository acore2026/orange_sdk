package com.rayneo.agent.example

import android.content.Context
import android.content.pm.PackageManager
import androidx.core.content.ContextCompat
import com.rayneo.agent.sdk.model.OffloadingSession
import com.rayneo.agent.sdk.transport.MediaOffloadAdapter
import com.rayneo.agent.sdk.transport.VideoTrack
import com.rayneo.agent.sdk.transport.VideoUploadHandle
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
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
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/** Concrete WebRTC bridge used by the integration App, not by the platform-neutral SDK. */
class AndroidWebRtcMediaOffloadAdapter(
    context: Context,
    private val onEvent: (String) -> Unit = {},
) : MediaOffloadAdapter {
    private val appContext = context.applicationContext
    private val egl = EglBase.create()
    private val http = OkHttpClient.Builder().build()
    private val json = Json { ignoreUnknownKeys = true }
    private val peers = CopyOnWriteArraySet<PeerConnection>()
    private val producerResources = CopyOnWriteArraySet<ProducerResources>()
    private val closed = AtomicBoolean(false)
    private val factory: PeerConnectionFactory

    init {
        if (FACTORY_INITIALIZED.compareAndSet(false, true)) {
            PeerConnectionFactory.initialize(
                PeerConnectionFactory.InitializationOptions.builder(appContext)
                    .createInitializationOptions(),
            )
        }
        factory = PeerConnectionFactory.builder()
            .setOptions(PeerConnectionFactory.Options().apply {
                // Do not pin WebRTC sockets to Wi-Fi/cellular. The OS must route them through
                // AgentVpnService for the N6 Video Server host route.
                disableNetworkMonitor = true
            })
            .setVideoEncoderFactory(DefaultVideoEncoderFactory(egl.eglBaseContext, true, true))
            .setVideoDecoderFactory(DefaultVideoDecoderFactory(egl.eglBaseContext))
            .createPeerConnectionFactory()
    }

    override suspend fun startVideoUpload(
        session: OffloadingSession,
        cameraId: String,
        width: Int,
        height: Int,
        fps: Int,
        bitrateKbps: Int,
    ): VideoUploadHandle {
        check(!closed.get()) { "WebRTC adapter is closed" }
        check(
            ContextCompat.checkSelfPermission(appContext, android.Manifest.permission.CAMERA) ==
                PackageManager.PERMISSION_GRANTED
        ) { "Camera permission is required before startVideoUpload" }
        val endpoint = checkNotNull(session.producer) { "Producer endpoint is missing" }
        onEvent("请求 Video Server 拉源 Offer：${endpoint.sourceStartUrl}")
        val offerPayload = postJson(
            endpoint.sourceStartUrl,
            endpoint.accessToken,
            buildJsonObject { put("action", "create_offer") },
        )
        val remoteOffer = offerPayload.requireSdp("sdp_offer", SessionDescription.Type.OFFER)

        val iceReady = CompletableDeferred<Unit>()
        val observer = peerObserver(
            iceReady = iceReady,
            onConnection = { onEvent("B→VideoServer WebRTC=$it") },
        )
        val pc = createPeer(observer)
        peers += pc
        val capturer = createCameraCapturer(cameraId)
        val texture = SurfaceTextureHelper.create("AgentVideoCapture", egl.eglBaseContext)
        val source = factory.createVideoSource(false)
        capturer.initialize(texture, appContext, source.capturerObserver)
        capturer.startCapture(width, height, fps)
        val localTrack = factory.createVideoTrack("source-${session.sessionId}", source)
        val transceiver = pc.addTransceiver(
            localTrack,
            RtpTransceiver.RtpTransceiverInit(RtpTransceiver.RtpTransceiverDirection.SEND_ONLY),
        ) ?: error("Unable to add source video transceiver")
        transceiver.sender.parameters.also { parameters ->
            parameters.encodings.forEach { it.maxBitrateBps = bitrateKbps * 1000 }
            transceiver.sender.setParameters(parameters)
        }
        val resources = ProducerResources(
            pc = pc,
            capturer = capturer,
            texture = texture,
            source = source,
            track = localTrack,
            stopUrl = endpoint.sourceStopUrl,
            token = endpoint.accessToken,
        )
        producerResources += resources
        try {
            pc.awaitSetRemote(remoteOffer)
            val answer = pc.awaitCreateAnswer()
            pc.awaitSetLocal(answer)
            awaitIceComplete(pc, iceReady)
            val local = checkNotNull(pc.localDescription) { "Local SDP answer is unavailable" }
            val connected = postJson(
                endpoint.sourceStartUrl,
                endpoint.accessToken,
                buildJsonObject {
                    put("sdp_answer", buildJsonObject {
                        put("type", local.type.canonicalForm())
                        put("sdp", local.description)
                    })
                },
            )
            check(connected.string("state") == "SOURCE_CONNECTED") {
                "Video Server did not confirm SOURCE_CONNECTED"
            }
            onEvent("Video Server 已收到 B 的首帧，track=${localTrack.id()}")
            return AndroidVideoUploadHandle(resources, session.sessionId)
        } catch (error: Throwable) {
            producerResources -= resources
            resources.release(sendStop = false)
            throw error
        }
    }

    override suspend fun getProcessedVideoTrack(
        session: OffloadingSession,
        timeoutSeconds: Double,
    ): VideoTrack {
        check(!closed.get()) { "WebRTC adapter is closed" }
        val endpoint = checkNotNull(session.processedStream) { "Processed endpoint is missing" }
        val trackReady = CompletableDeferred<org.webrtc.VideoTrack>()
        val iceReady = CompletableDeferred<Unit>()
        val observer = peerObserver(
            iceReady = iceReady,
            onConnection = { onEvent("VideoServer→Consumer WebRTC=$it") },
            onVideoTrack = { if (!trackReady.isCompleted) trackReady.complete(it) },
        )
        val pc = createPeer(observer)
        peers += pc
        pc.addTransceiver(
            MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO,
            RtpTransceiver.RtpTransceiverInit(RtpTransceiver.RtpTransceiverDirection.RECV_ONLY),
        ) ?: error("Unable to add receive-only video transceiver")
        try {
            val offer = pc.awaitCreateOffer()
            pc.awaitSetLocal(offer)
            awaitIceComplete(pc, iceReady)
            val local = checkNotNull(pc.localDescription) { "Local SDP offer is unavailable" }
            onEvent("向 Video Server 拉处理流：${endpoint.offerUrl}")
            val response = postJson(
                endpoint.offerUrl,
                endpoint.accessTicket,
                buildJsonObject {
                    put("sdp_offer", buildJsonObject {
                        put("type", local.type.canonicalForm())
                        put("sdp", local.description)
                    })
                },
            )
            pc.awaitSetRemote(response.requireSdp("sdp_answer", SessionDescription.Type.ANSWER))
            val remoteTrack = withTimeout((timeoutSeconds * 1000).toLong()) { trackReady.await() }
            onEvent("已取得服务端处理流，track=${remoteTrack.id()}")
            return AndroidVideoTrack(remoteTrack)
        } catch (error: Throwable) {
            peers -= pc
            pc.close()
            throw error
        }
    }

    override suspend fun close() {
        if (!closed.compareAndSet(false, true)) return
        producerResources.toList().forEach { it.release(sendStop = true) }
        producerResources.clear()
        peers.toList().forEach(PeerConnection::close)
        peers.clear()
        factory.dispose()
        egl.release()
        http.dispatcher.executorService.shutdown()
        http.connectionPool.evictAll()
    }

    private fun createPeer(observer: PeerConnection.Observer): PeerConnection =
        factory.createPeerConnection(
            PeerConnection.RTCConfiguration(emptyList()).apply {
                sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            },
            observer,
        ) ?: error("Unable to create WebRTC PeerConnection")

    private fun createCameraCapturer(cameraId: String): CameraVideoCapturer {
        val enumerator = Camera2Enumerator(appContext)
        val names = enumerator.deviceNames.toList()
        val selected = names.firstOrNull { it == cameraId }
            ?: names.firstOrNull { enumerator.isBackFacing(it) }
            ?: names.firstOrNull()
            ?: error("No Android camera is available")
        onEvent("开启摄像头：$selected")
        return enumerator.createCapturer(selected, null)
            ?: error("Unable to open camera $selected")
    }

    private suspend fun postJson(url: String, bearer: String, body: JsonObject): JsonObject =
        withContext(Dispatchers.IO) {
            val request = Request.Builder()
                .url(url)
                .header("Authorization", "Bearer $bearer")
                .post(body.toString().toRequestBody(JSON_MEDIA_TYPE))
                .build()
            http.newCall(request).execute().use { response ->
                val text = response.body?.string().orEmpty()
                check(response.isSuccessful) {
                    "Video Server HTTP ${response.code}: ${text.take(500)}"
                }
                json.parseToJsonElement(text) as? JsonObject
                    ?: error("Video Server response must be a JSON object")
            }
        }

    private suspend fun awaitIceComplete(
        pc: PeerConnection,
        ready: CompletableDeferred<Unit>,
    ) {
        if (pc.iceGatheringState() == PeerConnection.IceGatheringState.COMPLETE) return
        withTimeout(8_000) { ready.await() }
    }

    private fun peerObserver(
        iceReady: CompletableDeferred<Unit>,
        onConnection: (PeerConnection.PeerConnectionState) -> Unit,
        onVideoTrack: (org.webrtc.VideoTrack) -> Unit = {},
    ): PeerConnection.Observer = object : PeerConnection.Observer {
        override fun onSignalingChange(state: PeerConnection.SignalingState) = Unit
        override fun onIceConnectionChange(state: PeerConnection.IceConnectionState) = Unit
        override fun onConnectionChange(state: PeerConnection.PeerConnectionState) = onConnection(state)
        override fun onIceConnectionReceivingChange(receiving: Boolean) = Unit
        override fun onIceGatheringChange(state: PeerConnection.IceGatheringState) {
            if (state == PeerConnection.IceGatheringState.COMPLETE && !iceReady.isCompleted) {
                iceReady.complete(Unit)
            }
        }
        override fun onIceCandidate(candidate: IceCandidate) = Unit
        override fun onIceCandidatesRemoved(candidates: Array<IceCandidate>) = Unit
        override fun onAddStream(stream: MediaStream) {
            stream.videoTracks.firstOrNull()?.let(onVideoTrack)
        }
        override fun onRemoveStream(stream: MediaStream) = Unit
        override fun onDataChannel(channel: DataChannel) = Unit
        override fun onRenegotiationNeeded() = Unit
        override fun onAddTrack(receiver: RtpReceiver, streams: Array<out MediaStream>) {
            (receiver.track() as? org.webrtc.VideoTrack)?.let(onVideoTrack)
        }
        override fun onTrack(transceiver: RtpTransceiver) {
            (transceiver.receiver.track() as? org.webrtc.VideoTrack)?.let(onVideoTrack)
        }
    }

    private suspend fun PeerConnection.awaitCreateOffer(): SessionDescription =
        suspendCancellableCoroutine { continuation ->
            createOffer(object : SimpleSdpObserver() {
                override fun onCreateSuccess(value: SessionDescription?) {
                    if (value != null) continuation.resume(value)
                    else continuation.resumeWithException(IllegalStateException("Empty SDP offer"))
                }
                override fun onCreateFailure(error: String?) {
                    continuation.resumeWithException(IllegalStateException(error ?: "createOffer failed"))
                }
            }, MediaConstraints())
        }

    private suspend fun PeerConnection.awaitCreateAnswer(): SessionDescription =
        suspendCancellableCoroutine { continuation ->
            createAnswer(object : SimpleSdpObserver() {
                override fun onCreateSuccess(value: SessionDescription?) {
                    if (value != null) continuation.resume(value)
                    else continuation.resumeWithException(IllegalStateException("Empty SDP answer"))
                }
                override fun onCreateFailure(error: String?) {
                    continuation.resumeWithException(IllegalStateException(error ?: "createAnswer failed"))
                }
            }, MediaConstraints())
        }

    private suspend fun PeerConnection.awaitSetLocal(value: SessionDescription) =
        suspendCancellableCoroutine { continuation ->
            setLocalDescription(object : SimpleSdpObserver() {
                override fun onSetSuccess() = continuation.resume(Unit)
                override fun onSetFailure(error: String?) {
                    continuation.resumeWithException(IllegalStateException(error ?: "setLocalDescription failed"))
                }
            }, value)
        }

    private suspend fun PeerConnection.awaitSetRemote(value: SessionDescription) =
        suspendCancellableCoroutine { continuation ->
            setRemoteDescription(object : SimpleSdpObserver() {
                override fun onSetSuccess() = continuation.resume(Unit)
                override fun onSetFailure(error: String?) {
                    continuation.resumeWithException(IllegalStateException(error ?: "setRemoteDescription failed"))
                }
            }, value)
        }

    private fun JsonObject.requireSdp(
        field: String,
        expectedType: SessionDescription.Type,
    ): SessionDescription {
        val value = this[field]?.jsonObject ?: error("Video Server response has no $field")
        val type = value.string("type") ?: error("$field.type is missing")
        val sdp = value.string("sdp") ?: error("$field.sdp is missing")
        val parsedType = SessionDescription.Type.fromCanonicalForm(type.lowercase())
        check(parsedType == expectedType) { "$field.type must be ${expectedType.canonicalForm()}" }
        return SessionDescription(parsedType, sdp)
    }

    private fun JsonObject.string(field: String): String? =
        this[field]?.jsonPrimitive?.contentOrNull

    private inner class AndroidVideoUploadHandle(
        private val resources: ProducerResources,
        private val sessionId: String,
    ) : VideoUploadHandle {
        override val trackId: String get() = resources.track.id()
        override val state: String get() = if (resources.track.enabled()) "STREAMING" else "PAUSED"
        override suspend fun pause() { resources.track.setEnabled(false) }
        override suspend fun resume() { resources.track.setEnabled(true) }
        override suspend fun stop() {
            producerResources -= resources
            resources.release(sendStop = true)
            onEvent("已停止视频上传 session=$sessionId")
        }
    }

    private inner class ProducerResources(
        val pc: PeerConnection,
        val capturer: VideoCapturer,
        val texture: SurfaceTextureHelper,
        val source: org.webrtc.VideoSource,
        val track: org.webrtc.VideoTrack,
        val stopUrl: String,
        val token: String,
    ) {
        private val released = AtomicBoolean(false)

        suspend fun release(sendStop: Boolean) {
            if (!released.compareAndSet(false, true)) return
            if (sendStop) runCatching { postJson(stopUrl, token, buildJsonObject { put("action", "stop") }) }
            runCatching { capturer.stopCapture() }
            capturer.dispose()
            track.dispose()
            source.dispose()
            texture.dispose()
            peers -= pc
            pc.close()
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

    private open class SimpleSdpObserver : SdpObserver {
        override fun onCreateSuccess(value: SessionDescription?) = Unit
        override fun onSetSuccess() = Unit
        override fun onCreateFailure(error: String?) = Unit
        override fun onSetFailure(error: String?) = Unit
    }

    private companion object {
        val FACTORY_INITIALIZED = AtomicBoolean(false)
        val JSON_MEDIA_TYPE = "application/json; charset=utf-8".toMediaType()
    }
}
