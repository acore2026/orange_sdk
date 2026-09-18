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

@Serializable
enum class ComputeRequestType { CREATE, QUERY, CANCEL, RELEASE }

@Serializable
enum class ComputeInputFormat { NATURAL_LANGUAGE, STRUCTURED }

@Serializable
enum class ComputeRole { consumer, producer }

@Serializable
data class AcnContext(
    @SerialName("group_id") val groupId: String,
    @SerialName("requester_agent_id") val requesterAgentId: String,
    @SerialName("target_agent_id") val targetAgentId: String,
)

@Serializable
data class ComputeResources(
    @SerialName("cpu_millicores") val cpuMillicores: Long? = null,
    @SerialName("memory_mib") val memoryMib: Long? = null,
    @SerialName("gpu_count") val gpuCount: Long? = null,
    @SerialName("gpu_model") val gpuModel: String? = null,
)

@Serializable
data class ComputeConstraints(
    @SerialName("capability_id") val capabilityId: String,
    @SerialName("api_version") val apiVersion: String? = null,
    @SerialName("image_id") val imageId: String? = null,
    val resources: ComputeResources? = null,
    val dnn: String? = null,
    val snssai: String? = null,
    @SerialName("allow_base_qos") val allowBaseQos: Boolean? = null,
    @SerialName("max_duration_ms") val maxDurationMs: Long? = null,
    @SerialName("placement_region") val placementRegion: String? = null,
    @SerialName("data_residency_region") val dataResidencyRegion: String? = null,
)

@Serializable
data class ComputeSessionRequest(
    @SerialName("message_type") val messageType: String,
    @SerialName("request_type") val requestType: ComputeRequestType,
    @SerialName("input_format") val inputFormat: ComputeInputFormat,
    @SerialName("request_id") val requestId: String,
    @SerialName("acn_context") val acnContext: AcnContext? = null,
    val text: String? = null,
    val constraints: ComputeConstraints? = null,
    @SerialName("compute_service_session_id")
    val computeServiceSessionId: String? = null,
    @SerialName("target_request_id") val targetRequestId: String? = null,
    @SerialName("ui_locale") val uiLocale: String? = null,
)

data class ComputeSessionStatus(
    val messageType: String,
    val requestId: String,
    val status: String,
    val cause: String,
    val computeServiceSessionId: String? = null,
    val statusRevision: String? = null,
    val missingFields: List<String> = emptyList(),
    val result: JsonObject? = null,
)

data class Snssai(
    val sst: Int,
    val sd: String? = null,
)

data class RuntimeDataPlane(
    val accessType: String,
    val sessionSelection: String,
)

data class ComputeNetworkBinding(
    val pduSessionId: Int,
    val dnn: String,
    val snssai: Snssai,
    val ueIpv4: String,
    val runtimeDataPlane: RuntimeDataPlane,
)

data class ComputeConnectionParameters(
    val mediaConnectionsPath: String,
    val transport: String,
    val recognitionTargetPathTemplate: String? = null,
    val videoCodec: String? = null,
)

data class ComputingSession(
    val computeServiceSessionId: String,
    val computeInstanceId: String,
    val bindingRef: String,
    val role: ComputeRole,
    val receiverAgentId: String,
    val serviceEndpoint: String,
    val networkBinding: ComputeNetworkBinding,
    val connectionParameters: ComputeConnectionParameters,
    val expiresAt: Instant? = null,
)

data class ComputingContext(
    val computeServiceSessionId: String,
    val computeInstanceId: String,
    val bindingRef: String,
    val role: ComputeRole,
    val agentId: String,
)

data class RecognitionTarget(
    val label: String,
    val prompt: String,
)

data class RecognitionTargetStatus(
    val requestId: String,
    val computingContext: ComputingContext,
    val status: String,
    val targetRevision: String,
    val target: RecognitionTarget,
)

@Serializable
enum class ControlInputType { TEXT, STRUCTURED }

@Serializable
enum class ControlAction { movement, grab, search_object }

@Serializable
enum class ControlTargetRole { producer, sandbox }

data class ControlActionTarget(
    val role: ControlTargetRole,
    val agentId: String? = null,
)

data class ControlActionRequest(
    val requestId: String,
    val inputType: ControlInputType,
    val action: ControlAction? = null,
    val text: String? = null,
    val language: String? = null,
    val parameters: JsonObject? = null,
    val target: ControlActionTarget? = null,
)

data class AudioControlActionRequest(
    val requestId: String,
    val audio: ByteArray,
    val fileName: String,
    val contentType: String,
    val language: String = "zh",
)

data class AudioTranscriptionRequest(
    val audio: ByteArray,
    val fileName: String,
    val contentType: String,
    val requestId: String,
    val language: String? = null,
)

data class DiscoveryIntent(
    val type: String,
    val parameters: Map<String, String>,
) {
    val area: String? get() = parameters["area"]
}

data class AudioTranscriptionResult(
    val requestId: String,
    val text: String,
    val intent: DiscoveryIntent,
    val requiredSkills: List<String>,
)

data class RuntimeAudioIntent(
    val executor: String?,
    val intent: String,
    val direction: String?,
    val matched: Boolean,
    val backend: String?,
)

data class RuntimeAudioRecognitionResult(
    val requestId: String,
    val text: String,
    val intent: RuntimeAudioIntent,
)

/**
 * Normalized result returned by the standalone pruned_sandbox intent service.
 *
 * [intent] uses the public business names (for example `security patrol`) while [scene]
 * preserves the classifier's internal name (for example `patrol`). [slots] contains the
 * extracted business arguments such as `area`, `direction`, or `object`.
 */
data class IntentRecognitionResult(
    val status: String,
    val intent: String,
    val scene: String,
    val executor: String?,
    val slots: Map<String, String>,
    val matched: Boolean,
    val confidence: Double?,
    val backend: String?,
) {
    val area: String? get() = slots["area"]
    val direction: String? get() = slots["direction"]
    val objectName: String? get() = slots["object"]
}

data class VoiceTranscription(
    val text: String,
    val language: String,
    val transcriptId: String? = null,
)

data class ControlActionStatus(
    val requestId: String,
    val actionId: String,
    val status: String,
    val cause: String,
    val computingContext: ComputingContext? = null,
    val normalizedAction: ControlAction? = null,
    val normalizedParameters: JsonObject? = null,
    val result: JsonObject? = null,
    val transcription: VoiceTranscription? = null,
)
