package com.rayneo.agent.sdk.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import java.time.Instant

enum class NetworkMessageType { GROUP_INVITATION, GROUP_CONFIG, UNKNOWN }
enum class NetworkMessageAction { ACCEPT, REJECT, ACK }

@Serializable
data class GroupMemberWire(
    @SerialName("agent_id") val agentId: String,
    @SerialName("agent_name") val agentName: String,
    val skills: List<String>,
    @SerialName("agent_ip") val agentIp: String,
    @SerialName("service_endpoints") val serviceEndpoints: String,
)

@Serializable
data class GroupConfigWire(
    @SerialName("notification_type") val notificationType: String,
    val version: String,
    val timestamp: String,
    @SerialName("target_agent_id") val targetAgentId: String? = null,
    @SerialName("group_id") val groupId: String,
    val members: Map<String, GroupMemberWire>,
    val proof: JsonObject,
)

data class GroupMemberInfo(
    val agentId: String,
    val agentName: String,
    val capabilities: List<String>,
    val agentIp: String,
    val tcpPort: Int,
    val udpPort: Int,
    val didKey: String,
    val serviceEndpoint: String = "",
) {
    val skills: List<String>
        get() = capabilities
}

data class GroupConfigSnapshot(
    val groupId: String,
    val version: String,
    val notificationTimestamp: Instant,
    val membersByAgentId: Map<String, GroupMemberInfo>,
    val generation: Long,
)

data class SdkInitResult(
    val runtimeConnected: Boolean,
    val masqueConnected: Boolean,
    val localTcpEndpoint: String,
    val localUdpEndpoint: String,
    val agentTcpEndpoint: String,
    val agentUdpEndpoint: String,
    val agentTunCidr: String,
    val masqueProxyEndpoint: String,
    val masqueOuterSourceIp: String = "",
)

data class AgentProfile(
    val agentId: String,
    val agentName: String,
    val identityVc: JsonObject,
)

data class GroupInfo(
    val groupId: String,
    val groupName: String,
    var status: String = "PENDING",
)

data class MessageReceipt(
    val messageId: String,
    val delivered: Boolean,
    val deliveredAt: Instant?,
)

data class OperationResult(
    val success: Boolean,
    val operationId: String,
    val message: String,
)

data class NetworkAbility(
    val abilityVc: JsonObject,
    val abilities: List<String>,
    val validUntil: Instant?,
)

data class DiscoveredAgent(
    val agentId: String,
    val serviceEndpoints: String,
    val skills: List<String>,
    val priority: Int,
)

data class OffloadingSession(
    val sessionId: String,
    val sandboxId: String,
    val state: String,
    val expiresAt: Instant?,
    val metadata: JsonObject,
    val role: OffloadingSessionRole = OffloadingSessionRole.PRODUCER,
    val groupId: String = "",
    val sourceAgentId: String = "",
    val producer: VideoUploadEndpoint? = null,
    val processedStream: ProcessedVideoEndpoint? = null,
)

enum class OffloadingSessionRole { PRODUCER, CONSUMER }

data class VideoUploadEndpoint(
    val videoServerIp: String,
    val sourceStartUrl: String,
    val sourceStopUrl: String,
    val accessToken: String,
) {
    override fun toString(): String =
        "VideoUploadEndpoint(videoServerIp=$videoServerIp, " +
            "sourceStartUrl=$sourceStartUrl, sourceStopUrl=$sourceStopUrl, " +
            "accessToken=[REDACTED])"
}

data class ProcessedVideoEndpoint(
    val videoServerIp: String,
    val offerUrl: String,
    val accessTicket: String,
    val protocol: String = "webrtc",
    val signaling: String = "non-trickle",
) {
    override fun toString(): String =
        "ProcessedVideoEndpoint(videoServerIp=$videoServerIp, offerUrl=$offerUrl, " +
            "accessTicket=[REDACTED], protocol=$protocol, signaling=$signaling)"
}
