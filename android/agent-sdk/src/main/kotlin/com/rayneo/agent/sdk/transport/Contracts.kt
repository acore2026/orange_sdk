package com.rayneo.agent.sdk.transport

import com.rayneo.agent.sdk.model.NetworkMessageAction
import com.rayneo.agent.sdk.model.NetworkMessageType
import com.rayneo.agent.sdk.model.ComputingSession
import kotlinx.serialization.json.JsonObject
import java.net.URI

internal fun interface ProofVerifier {
    suspend fun verifyGroupConfig(payload: JsonObject)
}

internal fun interface ControlRequestAuthenticator {
    suspend fun authenticate(path: String, payload: JsonObject): JsonObject
}

internal interface DevicePublicKeyProvider {
    fun ensure()
    val publicKeyBase64: String
}

internal interface MessageSigner {
    suspend fun signA2a(payload: JsonObject): JsonObject
}

internal interface MessageSignatureVerifier {
    suspend fun verifyA2a(payload: JsonObject, expectedDidKey: String)
}

interface RuntimeTransport {
    suspend fun getUeInfo(): JsonObject
    suspend fun getAcnStatus(): JsonObject
    suspend fun startDownlink(
        onReconnected: suspend () -> Unit = {},
        handler: suspend (String, Int, JsonObject) -> JsonObject?,
    )
    suspend fun request(method: String, path: String, body: JsonObject): JsonObject
    suspend fun requestWithStatus(
        method: String,
        path: String,
        body: JsonObject,
    ): RuntimeHttpResponse = RuntimeHttpResponse(200, request(method, path, body))
    suspend fun requestWithStatus(
        method: String,
        path: String,
        body: JsonObject,
        timeoutSeconds: Double,
    ): RuntimeHttpResponse = requestWithStatus(method, path, body)
    suspend fun close()
}

internal interface SandboxTransport {
    suspend fun requestWithStatus(
        method: String,
        url: String,
        body: JsonObject?,
        timeoutSeconds: Double,
        sourceIpv4: String?,
    ): RuntimeHttpResponse
    suspend fun uploadWithStatus(
        url: String,
        fields: Map<String, String>,
        fileFieldName: String,
        fileName: String,
        contentType: String,
        content: ByteArray,
        timeoutSeconds: Double,
        sourceIpv4: String?,
    ): RuntimeHttpResponse
    suspend fun close()
}

data class RuntimeHttpResponse(
    val statusCode: Int,
    val body: JsonObject,
)

data class TunnelConfiguration(
    val agentTunCidr: String,
    val routes: Set<String>,
    val mtu: Int,
)

interface TunnelController {
    val tunFd: Int
    val clientIdentityDirectory: String
    suspend fun establish(configuration: TunnelConfiguration)
    suspend fun replaceGroupPeers(groupId: String, peerIps: Set<String>)
    fun currentAllowedPeerIps(): Set<String>
    fun setTunFdSwapper(swapper: suspend (Int) -> Unit)
    fun setTunReplacedListener(listener: suspend () -> Unit) = Unit
    suspend fun close()
}

data class MasqueConfiguration(
    val serverUrl: String,
    val authorization: String?,
    val localVlanIp: String,
    val agentTunCidr: String,
    val mtu: Int,
    val identityDirectory: String,
)

fun interface LocalAddressResolver {
    fun resolve(serverUri: URI): String
}

interface MasqueTransport {
    val connected: Boolean
    suspend fun start(tunFd: Int, configuration: MasqueConfiguration)
    suspend fun replaceTunFd(tunFd: Int)
    fun statistics(): MasqueTransportStatistics = MasqueTransportStatistics()
    suspend fun close()
}

data class MasqueTransportStatistics(
    val downlinkPackets: Long = 0,
    val downlinkPacketsOverTunMtu: Long = 0,
    val downlinkReadBufferTooSmall: Long = 0,
    val uplinkDatagramTooLarge: Long = 0,
    val maxDownlinkPacketBytes: Long = 0,
)

interface LocalServer {
    suspend fun start(
        agentIp: String,
        tcpPort: Int,
        udpPort: Int,
        onA2aMessage: suspend (JsonObject) -> Unit,
    )
    suspend fun close()
}

interface PeerMessenger {
    suspend fun send(endpoint: String, body: JsonObject, timeoutMillis: Long): JsonObject
}

fun interface NetworkMessageListener {
    suspend fun onNetworkMessage(
        messageType: NetworkMessageType,
        payload: JsonObject,
    ): NetworkMessageAction
}

fun interface GroupMessageListener {
    suspend fun onGroupMessage(
        groupId: String,
        senderAgentId: String,
        payload: JsonObject,
    )
}

interface VideoUploadHandle {
    val trackId: String
    val state: String
    suspend fun pause()
    suspend fun resume()
    suspend fun stop()
}

/** Wrapper around the platform WebRTC VideoTrack owned by a media adapter. */
interface VideoTrack {
    val trackId: String
    fun addSink(sink: Any)
    fun removeSink(sink: Any)
}

interface ProcessedVideoStream {
    val track: VideoTrack
    val state: String
    suspend fun close()
}

internal interface LocalProcessedVideo {
    val track: VideoTrack
    suspend fun close()
}

internal interface PreparedMediaConnection<T> {
    val offerSdp: String
    suspend fun applyAnswer(answerSdp: String, timeoutSeconds: Double): T
    suspend fun abort()
}

internal interface MediaOffloadAdapter {
    fun supportsVideoCodec(codec: String): Boolean

    suspend fun prepareVideoUpload(
        session: ComputingSession,
        cameraId: String,
        width: Int,
        height: Int,
        fps: Int,
        bitrateKbps: Int,
        timeoutSeconds: Double,
    ): PreparedMediaConnection<VideoUploadHandle>

    suspend fun prepareProcessedVideo(
        session: ComputingSession,
        timeoutSeconds: Double,
    ): PreparedMediaConnection<LocalProcessedVideo>

    suspend fun close()
}
