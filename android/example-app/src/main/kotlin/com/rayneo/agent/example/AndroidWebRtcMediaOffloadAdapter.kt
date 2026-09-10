package com.rayneo.agent.example

import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.ContextCompat
import com.rayneo.agent.sdk.model.OffloadingSession
import com.rayneo.agent.sdk.transport.MediaOffloadAdapter
import com.rayneo.agent.sdk.transport.VideoTrack
import com.rayneo.agent.sdk.transport.VideoUploadHandle
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
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
import org.webrtc.MediaCodecHighProfileVideoEncoderFactory
import org.webrtc.MediaStream
import org.webrtc.MediaStreamTrack
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.RtpReceiver
import org.webrtc.RtpTransceiver
import org.webrtc.RTCStatsCollectorCallback
import org.webrtc.RTCStatsReport
import org.webrtc.SdpObserver
import org.webrtc.SessionDescription
import org.webrtc.SurfaceTextureHelper
import org.webrtc.VideoCapturer
import org.webrtc.VideoSink
import java.util.concurrent.CopyOnWriteArraySet
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.Locale
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/** Concrete WebRTC bridge used by the integration App, not by the platform-neutral SDK. */
class AndroidWebRtcMediaOffloadAdapter(
    context: Context,
    sharedEglContext: EglBase.Context? = null,
    private val onEvent: (String) -> Unit = {},
) : MediaOffloadAdapter {
    private val appContext = context.applicationContext
    // Decoder/encoder texture frames must live in the same EGL share group as the UI renderer.
    // The adapter owns this child context; the Activity owns the root context and releases it
    // only after AgentSdk (and therefore this adapter) has closed.
    private val egl = EglBase.create(sharedEglContext)
    private val http = OkHttpClient.Builder()
        // The Mock waits up to 12 seconds for the first source frame. Keep the client
        // read timeout above that window so the HTTP 504 body is not masked by OkHttp.
        .readTimeout(30, TimeUnit.SECONDS)
        .build()
    private val json = Json { ignoreUnknownKeys = true }
    private val peers = CopyOnWriteArraySet<PeerConnection>()
    private val producerResources = CopyOnWriteArraySet<ProducerResources>()
    private val consumerResources = CopyOnWriteArraySet<ConsumerResources>()
    private val diagnosticScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val closed = AtomicBoolean(false)
    private val factory: PeerConnectionFactory
    private val highProfileEncoderFactory: MediaCodecHighProfileVideoEncoderFactory
    private val usesByteBufferDecoderOutput =
        Build.MANUFACTURER.equals("HUAWEI", ignoreCase = true)

    init {
        AndroidWebRtcMediaDiagnostics.reset()
        if (FACTORY_INITIALIZED.compareAndSet(false, true)) {
            PeerConnectionFactory.initialize(
                PeerConnectionFactory.InitializationOptions.builder(appContext)
                    .createInitializationOptions(),
            )
        }
        val defaultEncoderFactory = DefaultVideoEncoderFactory(egl.eglBaseContext, true, true)
        highProfileEncoderFactory = MediaCodecHighProfileVideoEncoderFactory(
            egl.eglBaseContext,
            defaultEncoderFactory,
        ) { message -> onEvent(message) }
        val decoderEglContext = if (usesByteBufferDecoderOutput) null else egl.eglBaseContext
        if (usesByteBufferDecoderOutput) {
            onEvent(
                "Huawei 下行解码兼容模式=MediaCodec ByteBuffer；" +
                    "绕过 OMX.hisi DynamicANWBuffer/Surface 输出故障",
            )
        }
        factory = PeerConnectionFactory.builder()
            .setOptions(PeerConnectionFactory.Options().apply {
                // Do not pin WebRTC sockets to Wi-Fi/cellular. The OS must route them through
                // AgentVpnService for the N6 Video Server host route.
                disableNetworkMonitor = true
            })
            .setVideoEncoderFactory(highProfileEncoderFactory)
            .setVideoDecoderFactory(DefaultVideoDecoderFactory(decoderEglContext))
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
        check(highProfileEncoderFactory.isHighProfileAvailable) {
            "This device has no MediaCodec H264 High Profile encoder"
        }
        onEvent(
            "B 端 H264 High 能力已确认：encoder=" +
                highProfileEncoderFactory.highProfileEncoderName,
        )
        val endpoint = checkNotNull(session.producer) { "Producer endpoint is missing" }
        onEvent("请求 Video Server 拉源 Offer：${endpoint.sourceStartUrl}")
        val offerPayload = postJson(
            endpoint.sourceStartUrl,
            buildJsonObject { put("action", "create_offer") },
        )
        val remoteOffer = offerPayload.requireSdp("sdp_offer", SessionDescription.Type.OFFER)
        val offeredSourceProfile = requireH264HighVideoCodec(
            remoteOffer.description,
            "Video Server source offer",
        )
        onEvent("Video Server 源流协商=H264 High Profile $offeredSourceProfile")

        val iceReady = CompletableDeferred<Unit>()
        val observer = peerObserver(
            iceReady = iceReady,
            onConnection = { onEvent("B→VideoServer WebRTC=$it") },
        )
        val pc = createPeer(observer)
        peers += pc
        var resources: ProducerResources? = null
        try {
            // This endpoint is the answerer: apply the Video Server offer first so its
            // video m-line owns the transceiver. Pre-creating a local transceiver here is
            // not portable across WebRTC implementations and can yield an inactive answer.
            pc.awaitSetRemote(remoteOffer)
            val offeredVideoTransceivers = pc.transceivers.filter {
                !it.isStopped &&
                    it.mediaType == MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO &&
                    !it.mid.isNullOrBlank()
            }
            check(offeredVideoTransceivers.size == 1) {
                "Video Server offer must contain exactly one active video transceiver"
            }
            val transceiver = offeredVideoTransceivers.single()

            val capturer = createCameraCapturer(cameraId)
            val texture = SurfaceTextureHelper.create("AgentVideoCapture", egl.eglBaseContext)
            val source = factory.createVideoSource(false)
            capturer.initialize(texture, appContext, source.capturerObserver)
            val localTrack = factory.createVideoTrack("source-${session.sessionId}", source)
            resources = ProducerResources(
                pc = pc,
                capturer = capturer,
                texture = texture,
                source = source,
                track = localTrack,
                stopUrl = endpoint.sourceStopUrl,
            )
            producerResources += resources

            check(transceiver.setDirection(RtpTransceiver.RtpTransceiverDirection.SEND_ONLY)) {
                "Unable to set offered video transceiver to SEND_ONLY"
            }
            check(transceiver.sender.setTrack(localTrack, false)) {
                "Unable to bind camera track to offered video transceiver"
            }
            transceiver.sender.parameters.also { parameters ->
                parameters.encodings.forEach { it.maxBitrateBps = bitrateKbps * 1000 }
                check(transceiver.sender.setParameters(parameters)) {
                    "Unable to configure source video bitrate"
                }
            }
            onEvent("摄像头轨道已绑定 Server Offer：mid=${transceiver.mid}，direction=SEND_ONLY")
            onEvent("摄像头采集参数=${width}x${height}@${fps}fps，目标码率=${bitrateKbps}kbps")
            capturer.startCapture(width, height, fps)

            val answer = pc.awaitCreateAnswer()
            pc.awaitSetLocal(answer)
            awaitIceComplete(pc, iceReady)
            val local = checkNotNull(pc.localDescription) { "Local SDP answer is unavailable" }
            val answerMid = requireH264HighSendingVideoAnswer(local.description)
            val negotiatedSourceProfile = requireH264HighVideoCodec(
                local.description,
                "Source SDP answer",
            )
            check(answerMid == transceiver.mid) {
                "Camera track was negotiated on unexpected video mid=$answerMid"
            }
            onEvent("B→VideoServer 实际协商=H264 High Profile $negotiatedSourceProfile")
            val connected = postJson(
                endpoint.sourceStartUrl,
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
            resources?.let {
                producerResources -= it
                it.release(sendStop = false)
            } ?: run {
                peers -= pc
                pc.close()
            }
            throw error
        }
    }

    override suspend fun getProcessedVideoTrack(
        session: OffloadingSession,
        timeoutSeconds: Double,
    ): VideoTrack {
        check(!closed.get()) { "WebRTC adapter is closed" }
        check(timeoutSeconds > 0.0) { "timeoutSeconds must be greater than zero" }
        val endpoint = checkNotNull(session.processedStream) { "Processed endpoint is missing" }
        val iceReady = CompletableDeferred<Unit>()
        val unifiedPlanTrackCallbackSeen = AtomicBoolean(false)
        val observer = peerObserver(
            iceReady = iceReady,
            onConnection = { onEvent("VideoServer→Consumer WebRTC=$it") },
            onVideoTrack = {
                if (unifiedPlanTrackCallbackSeen.compareAndSet(false, true)) {
                    onEvent("收到 Unified Plan onTrack 回调，track=${it.id()}")
                }
            },
        )
        val pc = createPeer(observer)
        peers += pc
        pc.addTransceiver(
            MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO,
            RtpTransceiver.RtpTransceiverInit(RtpTransceiver.RtpTransceiverDirection.RECV_ONLY),
        ) ?: error("Unable to add receive-only video transceiver")
        var consumer: ConsumerResources? = null
        try {
            val offer = pc.awaitCreateOffer()
            val preferredOffer = SessionDescription(
                offer.type,
                preferH264InOfferSdp(offer.description),
            )
            pc.awaitSetLocal(preferredOffer)
            awaitIceComplete(pc, iceReady)
            val local = checkNotNull(pc.localDescription) { "Local SDP offer is unavailable" }
            onEvent("向 Video Server 拉处理流：${endpoint.offerUrl}")
            val response = postJson(
                endpoint.offerUrl,
                buildJsonObject {
                    put("sdp_offer", buildJsonObject {
                        put("type", local.type.canonicalForm())
                        put("sdp", local.description)
                    })
                },
            )
            pc.awaitSetRemote(response.requireSdp("sdp_answer", SessionDescription.Type.ANSWER))
            // setRemoteDescription success means Unified Plan transceivers have been committed.
            // Select the receiver deterministically instead of racing the legacy onAddStream,
            // onAddTrack and onTrack callbacks for the same native track.
            val receivingTransceivers = pc.transceivers.filter {
                !it.isStopped &&
                    it.mediaType == MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO &&
                    it.receiver.track() is org.webrtc.VideoTrack
            }
            check(receivingTransceivers.size == 1) {
                "Video Server answer must contain exactly one receiving video transceiver"
            }
            val transceiver = receivingTransceivers.single()
            val remoteTrack = transceiver.receiver.track() as org.webrtc.VideoTrack
            val diagnostics = MutableInboundRtpDiagnostics(
                sessionId = session.sessionId,
                trackId = remoteTrack.id(),
                peerConnectionState = pc.connectionState().name,
            )
            val diagnosticSink = VideoSink { frame ->
                val callbackCount = diagnostics.recordDecodedSinkFrame(
                    width = frame.buffer.width,
                    height = frame.buffer.height,
                )
                if (callbackCount == 1L) {
                    onEvent(
                        "A 端解码首帧回调，session=${session.sessionId}，" +
                            "track=${remoteTrack.id()}，${frame.buffer.width}x${frame.buffer.height}",
                    )
                }
            }
            remoteTrack.addSink(diagnosticSink)
            val resources = ConsumerResources(
                pc = pc,
                track = remoteTrack,
                diagnosticSink = diagnosticSink,
                diagnostics = diagnostics,
            )
            consumer = resources
            consumerResources += resources
            resources.statsJob = diagnosticScope.launch {
                while (isActive && !closed.get()) {
                    runCatching { refreshInboundRtp(resources) }
                    delay(INBOUND_STATS_INTERVAL_MILLIS)
                }
            }
            onEvent(
                "已取得服务端处理流，track=${remoteTrack.id()}，mid=${transceiver.mid}；" +
                    "inbound-rtp 统计已启动",
            )
            return AndroidVideoTrack(remoteTrack)
        } catch (error: Throwable) {
            consumer?.let { resources ->
                resources.statsJob?.cancel()
                runCatching { resources.track.removeSink(resources.diagnosticSink) }
                consumerResources -= resources
            }
            peers -= pc
            pc.close()
            throw error
        }
    }

    override suspend fun close() {
        if (!closed.compareAndSet(false, true)) return
        producerResources.toList().forEach { it.release(sendStop = true) }
        producerResources.clear()
        consumerResources.toList().forEach { resources ->
            runCatching { refreshInboundRtp(resources) }
            resources.statsJob?.cancel()
            runCatching { resources.track.removeSink(resources.diagnosticSink) }
            peers -= resources.pc
            resources.pc.close()
            resources.diagnostics.updatePeerConnectionState(PeerConnection.PeerConnectionState.CLOSED.name)
        }
        consumerResources.clear()
        diagnosticScope.cancel()
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

    private suspend fun postJson(url: String, body: JsonObject): JsonObject =
        withContext(Dispatchers.IO) {
            val request = Request.Builder()
                .url(url)
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

    private suspend fun refreshInboundRtp(resources: ConsumerResources) {
        val report = withTimeout(INBOUND_STATS_TIMEOUT_MILLIS) { resources.pc.awaitStats() }
        val inboundVideo = report.statsMap.values.filter { stat ->
            stat.type == "inbound-rtp" &&
                (stat.members["kind"] == "video" || stat.members["mediaType"] == "video")
        }
        resources.diagnostics.updateInboundRtp(
            peerConnectionState = resources.pc.connectionState().name,
            packetsReceived = inboundVideo.sumOf { it.members.longValue("packetsReceived") },
            bytesReceived = inboundVideo.sumOf { it.members.longValue("bytesReceived") },
            framesDecoded = inboundVideo.sumOf { it.members.longValue("framesDecoded") },
            framesDropped = inboundVideo.sumOf { it.members.longValue("framesDropped") },
        )
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
        override fun onAddStream(stream: MediaStream) = Unit
        override fun onRemoveStream(stream: MediaStream) = Unit
        override fun onDataChannel(channel: DataChannel) = Unit
        override fun onRenegotiationNeeded() = Unit
        override fun onAddTrack(receiver: RtpReceiver, streams: Array<out MediaStream>) = Unit
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

    private suspend fun PeerConnection.awaitStats(): RTCStatsReport =
        suspendCancellableCoroutine { continuation ->
            getStats(RTCStatsCollectorCallback { report ->
                if (continuation.isActive) continuation.resume(report)
            })
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
    ) {
        private val released = AtomicBoolean(false)

        suspend fun release(sendStop: Boolean) {
            if (!released.compareAndSet(false, true)) return
            if (sendStop) runCatching { postJson(stopUrl, buildJsonObject { put("action", "stop") }) }
            runCatching { capturer.stopCapture() }
            capturer.dispose()
            track.dispose()
            source.dispose()
            texture.dispose()
            peers -= pc
            pc.close()
        }
    }

    private class ConsumerResources(
        val pc: PeerConnection,
        val track: org.webrtc.VideoTrack,
        val diagnosticSink: VideoSink,
        val diagnostics: MutableInboundRtpDiagnostics,
    ) {
        @Volatile
        var statsJob: Job? = null
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
        const val INBOUND_STATS_INTERVAL_MILLIS = 1_000L
        const val INBOUND_STATS_TIMEOUT_MILLIS = 2_000L
    }
}

private fun Map<String, Any>.longValue(key: String): Long =
    (this[key] as? Number)?.toLong() ?: this[key]?.toString()?.toLongOrNull() ?: 0L

internal data class InboundRtpDiagnosticSnapshot(
    val sessionId: String,
    val trackId: String,
    val peerConnectionState: String,
    val packetsReceived: Long,
    val bytesReceived: Long,
    val framesDecoded: Long,
    val framesDropped: Long,
    val decodedSinkCallbacks: Long,
    val lastFrameSize: String,
    val statsSamples: Long,
)

internal object AndroidWebRtcMediaDiagnostics {
    private val snapshots = ConcurrentHashMap<String, InboundRtpDiagnosticSnapshot>()

    fun reset() = snapshots.clear()

    fun update(snapshot: InboundRtpDiagnosticSnapshot) {
        snapshots[snapshot.sessionId] = snapshot
    }

    fun render(): String = snapshots.values
        .sortedBy(InboundRtpDiagnosticSnapshot::sessionId)
        .joinToString("\n") { snapshot ->
            "session_id=${snapshot.sessionId} track_id=${snapshot.trackId} " +
                "peer_connection_state=${snapshot.peerConnectionState} " +
                "packetsReceived=${snapshot.packetsReceived} " +
                "bytesReceived=${snapshot.bytesReceived} " +
                "framesDecoded=${snapshot.framesDecoded} " +
                "framesDropped=${snapshot.framesDropped} " +
                "decoded_sink_callbacks=${snapshot.decodedSinkCallbacks} " +
                "last_frame_size=${snapshot.lastFrameSize} " +
                "stats_samples=${snapshot.statsSamples}"
        }
        .ifBlank { "<none>" }
}

private class MutableInboundRtpDiagnostics(
    private val sessionId: String,
    private val trackId: String,
    peerConnectionState: String,
) {
    private var peerConnectionState = peerConnectionState
    private var packetsReceived = 0L
    private var bytesReceived = 0L
    private var framesDecoded = 0L
    private var framesDropped = 0L
    private val decodedSinkCallbacks = AtomicLong(0L)
    private var lastFrameSize = "<none>"
    private var statsSamples = 0L

    init {
        publish()
    }

    @Synchronized
    fun updateInboundRtp(
        peerConnectionState: String,
        packetsReceived: Long,
        bytesReceived: Long,
        framesDecoded: Long,
        framesDropped: Long,
    ) {
        this.peerConnectionState = peerConnectionState
        this.packetsReceived = maxOf(this.packetsReceived, packetsReceived)
        this.bytesReceived = maxOf(this.bytesReceived, bytesReceived)
        this.framesDecoded = maxOf(this.framesDecoded, framesDecoded)
        this.framesDropped = maxOf(this.framesDropped, framesDropped)
        statsSamples += 1
        publish()
    }

    @Synchronized
    fun recordDecodedSinkFrame(width: Int, height: Int): Long {
        val count = decodedSinkCallbacks.incrementAndGet()
        lastFrameSize = "${width}x$height"
        publish()
        return count
    }

    @Synchronized
    fun updatePeerConnectionState(state: String) {
        peerConnectionState = state
        publish()
    }

    private fun publish() {
        AndroidWebRtcMediaDiagnostics.update(
            InboundRtpDiagnosticSnapshot(
                sessionId = sessionId,
                trackId = trackId,
                peerConnectionState = peerConnectionState,
                packetsReceived = packetsReceived,
                bytesReceived = bytesReceived,
                framesDecoded = framesDecoded,
                framesDropped = framesDropped,
                decodedSinkCallbacks = decodedSinkCallbacks.get(),
                lastFrameSize = lastFrameSize,
                statsSamples = statsSamples,
            ),
        )
    }
}

/** Return the MID of the active video section that the local SDP answer will send. */
internal fun requireSendingVideoAnswer(sdp: String): String {
    val normalized = sdp.replace("\r\n", "\n")
    val mediaSections = normalized
        .split(Regex("(?=^m=)", RegexOption.MULTILINE))
        .filter { it.startsWith("m=video ") }
    val sendingSections = mediaSections.filter { section ->
        val lines = section.lineSequence().map(String::trim).filter(String::isNotEmpty).toList()
        val port = lines.first().split(Regex("\\s+")).getOrNull(1)
        port != null && port != "0" && lines.any { it == "a=sendonly" || it == "a=sendrecv" }
    }
    check(sendingSections.size == 1) {
        "SDP answer must contain exactly one active sending video section"
    }
    return sendingSections.single().lineSequence()
        .map(String::trim)
        .firstOrNull { it.startsWith("a=mid:") }
        ?.removePrefix("a=mid:")
        ?.takeIf(String::isNotBlank)
        ?: error("Sending video section has no MID")
}

/** Require the source Answer to keep the H264 High profile selected by the Server Offer. */
internal fun requireH264HighSendingVideoAnswer(sdp: String): String {
    val mid = requireSendingVideoAnswer(sdp)
    requireH264HighVideoCodec(sdp, "Source SDP answer")
    return mid
}

/** Return the selected High profile-level-id, rejecting codec/profile fallback. */
internal fun requireH264HighVideoCodec(sdp: String, label: String): String {
    val normalized = sdp.replace("\r\n", "\n")
    val videoSections = normalized
        .split(Regex("(?=^m=)", RegexOption.MULTILINE))
        .filter { section ->
            if (!section.startsWith("m=video ")) return@filter false
            section.lineSequence().first().trim().split(Regex("\\s+")).getOrNull(1) != "0"
        }
    check(videoSections.size == 1) { "$label must contain exactly one active video section" }
    val lines = videoSections.single().lineSequence().map(String::trim).toList()
    val payloads = lines.first().split(Regex("\\s+")).drop(3)
    val codecByPayload = lines.mapNotNull { line ->
        Regex("""^a=rtpmap:([^\s]+)\s+([^/\s]+)(?:/\d+)?""", RegexOption.IGNORE_CASE)
            .find(line)
            ?.let { it.groupValues[1] to it.groupValues[2] }
    }.toMap()
    val fmtpByPayload = lines.mapNotNull { line ->
        Regex("""^a=fmtp:([^\s]+)\s+(.+)$""", RegexOption.IGNORE_CASE)
            .find(line)
            ?.let { match ->
                val parameters = match.groupValues[2]
                    .split(";")
                    .mapNotNull { item ->
                        val parts = item.trim().split("=", limit = 2)
                        if (parts.size == 2) {
                            parts[0].lowercase(Locale.US) to parts[1].lowercase(Locale.US)
                        } else {
                            null
                        }
                    }
                    .toMap()
                match.groupValues[1] to parameters
            }
    }.toMap()
    val selectedPayload = payloads.firstOrNull { payload ->
        !codecByPayload[payload].equals("rtx", ignoreCase = true)
    } ?: error("$label has no negotiated base video codec")
    val codec = codecByPayload[selectedPayload] ?: "<unknown>"
    val parameters = fmtpByPayload[selectedPayload].orEmpty()
    val profileLevelId = parameters["profile-level-id"] ?: "<missing>"
    val packetizationMode = parameters["packetization-mode"] ?: "0"
    check(
        codec.equals("H264", ignoreCase = true) &&
            profileLevelId in H264_HIGH_PROFILE_LEVEL_IDS &&
            packetizationMode == "1"
    ) {
        "$label must negotiate H264 High Profile with packetization-mode=1; " +
            "got codec=$codec profile-level-id=$profileLevelId " +
            "packetization-mode=$packetizationMode"
    }
    return profileLevelId
}

/** Put H264 and its RTX payloads first without removing VP8/VP9 fallback. */
internal fun preferH264InOfferSdp(sdp: String): String {
    val lineEnding = if (sdp.contains("\r\n")) "\r\n" else "\n"
    val trailingLineEnding = sdp.endsWith(lineEnding)
    val body = if (trailingLineEnding) sdp.dropLast(lineEnding.length) else sdp
    val lines = if (body.isEmpty()) emptyList() else body.split(lineEnding)
    val codecByPayload = mutableMapOf<String, String>()
    val aptByPayload = mutableMapOf<String, String>()
    val rtpMap = Regex("""^a=rtpmap:([^\s]+)\s+([^/\s]+)""", RegexOption.IGNORE_CASE)
    val fmtp = Regex("""^a=fmtp:([^\s]+)\s+(.+)$""", RegexOption.IGNORE_CASE)
    val apt = Regex("""(?:^|[;\s])apt=([^\s;]+)""", RegexOption.IGNORE_CASE)

    lines.forEach { line ->
        rtpMap.find(line)?.let { match ->
            codecByPayload[match.groupValues[1]] = match.groupValues[2].uppercase(Locale.US)
        }
        fmtp.find(line)?.let { match ->
            apt.find(match.groupValues[2])?.let { aptMatch ->
                aptByPayload[match.groupValues[1]] = aptMatch.groupValues[1]
            }
        }
    }
    val h264Payloads = codecByPayload.filterValues { it == "H264" }.keys
    if (h264Payloads.isEmpty()) return sdp
    val preferredPayloads = h264Payloads + aptByPayload
        .filterValues { it in h264Payloads }
        .keys
    var changed = false
    val reordered = lines.map { line ->
        if (!line.startsWith("m=video ")) return@map line
        val parts = line.trim().split(Regex("""\s+"""))
        if (parts.size <= 3) return@map line
        val payloads = parts.drop(3)
        val preferred = payloads.filter { it in preferredPayloads }
        if (preferred.isEmpty()) return@map line
        val result = preferred + payloads.filterNot { it in preferredPayloads }
        if (result == payloads) line else {
            changed = true
            (parts.take(3) + result).joinToString(" ")
        }
    }
    if (!changed) return sdp
    return reordered.joinToString(lineEnding) + if (trailingLineEnding) lineEnding else ""
}

private val H264_HIGH_PROFILE_LEVEL_IDS = setOf("64001f", "640c1f")
