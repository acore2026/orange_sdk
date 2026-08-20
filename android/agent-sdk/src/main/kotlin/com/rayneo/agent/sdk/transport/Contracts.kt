package com.rayneo.agent.sdk.transport

import com.rayneo.agent.sdk.model.NetworkMessageAction
import com.rayneo.agent.sdk.model.NetworkMessageType
import com.rayneo.agent.sdk.model.OffloadingSession
import kotlinx.serialization.json.JsonObject

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
    suspend fun connect()
    suspend fun startDownlink(
        handler: suspend (String, Int, JsonObject) -> NetworkMessageAction,
    )
    suspend fun registerEndpoint(
        localIp: String,
        tcpPort: Int,
        udpPort: Int,
    ): EndpointRegistration
    suspend fun request(method: String, path: String, body: JsonObject): JsonObject
    suspend fun close()
}

data class EndpointRegistration(
    val ueIp: String,
    val uePrefixLength: Int,
) {
    val agentTunCidr: String get() = "$ueIp/$uePrefixLength"
}

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

interface MasqueTransport {
    val connected: Boolean
    suspend fun start(tunFd: Int, configuration: MasqueConfiguration)
    suspend fun replaceTunFd(tunFd: Int)
    suspend fun close()
}

interface LocalServer {
    suspend fun start(
        physicalIp: String,
        agentIp: String,
        tcpPort: Int,
        udpPort: Int,
        onA2aMessage: suspend (JsonObject) -> Unit,
    )
    suspend fun close()
}

interface PeerMessenger {
    suspend fun send(ip: String, port: Int, body: JsonObject, timeoutMillis: Long): JsonObject
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

interface MediaOffloadAdapter {
    suspend fun connect(
        session: OffloadingSession,
        signaling: JsonObject,
        timeoutSeconds: Double,
    )

    suspend fun startVideoUpload(
        session: OffloadingSession,
        cameraId: String,
        width: Int,
        height: Int,
        fps: Int,
        bitrateKbps: Int,
    ): VideoUploadHandle

    suspend fun getProcessedVideoTrack(
        session: OffloadingSession,
        timeoutSeconds: Double,
    ): VideoTrack

    suspend fun close()
}
