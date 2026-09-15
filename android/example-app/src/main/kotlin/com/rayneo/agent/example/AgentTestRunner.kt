package com.rayneo.agent.example

import com.rayneo.agent.sdk.AgentSdk
import com.rayneo.agent.sdk.model.AcnContext
import com.rayneo.agent.sdk.model.AgentLifecycleState
import com.rayneo.agent.sdk.model.AgentProfile
import com.rayneo.agent.sdk.model.AudioControlActionRequest
import com.rayneo.agent.sdk.model.AudioTranscriptionRequest
import com.rayneo.agent.sdk.model.ComputeConstraints
import com.rayneo.agent.sdk.model.ComputeInputFormat
import com.rayneo.agent.sdk.model.ComputeRequestType
import com.rayneo.agent.sdk.model.ComputeSessionRequest
import com.rayneo.agent.sdk.model.ControlActionRequest
import com.rayneo.agent.sdk.model.ControlInputType
import com.rayneo.agent.sdk.model.GroupConfigSnapshot
import com.rayneo.agent.sdk.model.MessageReceipt
import com.rayneo.agent.sdk.model.NetworkMessageAction
import com.rayneo.agent.sdk.model.NetworkMessageType
import com.rayneo.agent.sdk.model.OperationResult
import com.rayneo.agent.sdk.model.SdkInitResult
import com.rayneo.agent.sdk.transport.GroupMessageListener
import com.rayneo.agent.sdk.transport.NetworkMessageListener
import com.rayneo.agent.sdk.transport.ProcessedVideoStream
import com.rayneo.agent.sdk.transport.VideoTrack
import com.rayneo.agent.sdk.transport.VideoUploadHandle
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.webrtc.VideoSink
import java.util.UUID

enum class LabLogLevel { INFO, SUCCESS, WARNING, ERROR }

data class RunnerStatus(
    val title: String,
    val detail: String,
    val canRetry: Boolean = false,
)

data class ManualMessageSession(
    val groupId: String,
    val localAgentId: String,
    val targetAgentId: String,
    val targetAgentName: String,
)

data class SdkFeatureState(
    val cardPublished: Boolean,
    val groupReady: Boolean,
    val computeSessionId: String?,
    val computeStatus: String?,
    val videoUploadState: String?,
    val processedVideoState: String?,
    val recognitionTargetRevision: String?,
    val controlActionId: String?,
    val lastTranscription: String?,
) {
    val computeSessionReady: Boolean get() = !computeSessionId.isNullOrBlank()
    val producerVideoReady: Boolean get() = videoUploadState != null
}

private data class PendingComputeNotification(
    val groupId: String,
    val senderAgentId: String,
    val sessionId: String,
)

internal fun selectManualMessageSession(
    snapshot: GroupConfigSnapshot,
    localAgentId: String,
): ManualMessageSession {
    check(snapshot.membersByAgentId.containsKey(localAgentId)) {
        "群组配置中不存在本端 Agent"
    }
    val peers = snapshot.membersByAgentId.values.filter { it.agentId != localAgentId }
    check(peers.size == 1) {
        "A/B 联调 App 要求群组中恰好有一个对端，实际为 ${peers.size}"
    }
    val peer = peers.single()
    return ManualMessageSession(
        groupId = snapshot.groupId,
        localAgentId = localAgentId,
        targetAgentId = peer.agentId,
        targetAgentName = peer.agentName,
    )
}

internal suspend fun deregisterIdentityForStop(
    sdk: AgentSdk,
    onLog: (LabLogLevel, String, String) -> Unit,
): OperationResult? {
    val profile = sdk.localProfile
    if (profile == null) {
        onLog(LabLogLevel.INFO, "H-ID DELETE", "本地没有已申请身份，无需发送去注册请求")
        return null
    }
    onLog(LabLogLevel.INFO, "H-ID DELETE", "停止前发送去注册请求 agent_id=${profile.agentId}")
    return try {
        sdk.deregisterIdentity(profile.agentId, "normal").also { result ->
            if (result.success) {
                onLog(LabLogLevel.SUCCESS, "H-ID DELETE", "去注册成功，网侧与本地身份已清除")
            } else {
                onLog(
                    LabLogLevel.ERROR,
                    "H-ID DELETE",
                    "去注册被拒绝：${result.message.ifBlank { "Runtime 未返回原因" }}",
                )
            }
        }
    } catch (error: CancellationException) {
        throw error
    } catch (error: Exception) {
        onLog(
            LabLogLevel.ERROR,
            "H-ID DELETE",
            "去注册请求失败：${error.message ?: error::class.java.simpleName}",
        )
        throw error
    }
}

class AgentTestRunner(
    private val sdk: AgentSdk,
    private val config: TestConfig,
    private val onLog: (LabLogLevel, String, String) -> Unit,
    private val onStatus: (RunnerStatus) -> Unit,
    private val onManualMessageSession: (ManualMessageSession?) -> Unit,
    private val onResetAvailability: (Boolean) -> Unit = {},
    private val onComputeActionAvailability: (Boolean) -> Unit = {},
    private val onProducerSessionReady: () -> Unit = {},
    private val processedVideoRenderSinks: () -> List<VideoSink> = { emptyList() },
    private val onProcessedVideoStatus: (String, String) -> Unit = { _, _ -> },
    private val onFeatureStateChanged: (SdkFeatureState) -> Unit = {},
) {
    private val retrySignal = Channel<Unit>(Channel.CONFLATED)
    private val sendMutex = Mutex()
    private val operationMutex = Mutex()
    private var networkListener: AutoCloseable? = null
    private var groupListener: AutoCloseable? = null

    @Volatile private var localAgentId: String? = null
    @Volatile private var initResult: SdkInitResult? = null
    @Volatile private var manualMessageSession: ManualMessageSession? = null
    @Volatile private var resetRequested = false
    @Volatile private var activeComputeSessionId: String? = null
    @Volatile private var createComputeRequest: ComputeSessionRequest? = null
    @Volatile private var videoUploadHandle: VideoUploadHandle? = null
    @Volatile private var processedVideoStream: ProcessedVideoStream? = null
    @Volatile private var pendingComputeNotification: PendingComputeNotification? = null
    @Volatile private var lastComputeStatus: String? = null
    @Volatile private var recognitionTargetRevision: String? = null
    @Volatile private var lastControlActionId: String? = null
    @Volatile private var lastTranscription: String? = null
    private val receivedVideoSinks = mutableListOf<Pair<VideoTrack, VideoSink>>()

    fun retryCurrentStep() {
        retrySignal.trySend(Unit)
    }

    suspend fun run() {
        installListeners()
        onLog(
            LabLogLevel.INFO,
            "BOOT",
            "角色=${config.role.name}，Runtime=http://${config.serverIp}:${config.runtimePort}，" +
                "MASQUE=${config.masqueServerUrl}",
        )
        val initialized = retryableStep("INIT", "建立端侧链路") {
            sdk.initialize(
                agentRuntimeIp = config.serverIp,
                agentRuntimePort = config.runtimePort,
                localTcpPort = config.localTcpPort,
                localUdpPort = config.localUdpPort,
                masqueServerUrl = config.masqueServerUrl,
                masqueAuthorization = config.masqueToken?.let { "Bearer $it" },
            )
        }
        initResult = initialized
        onLog(
            LabLogLevel.SUCCESS,
            "INIT",
            "系统选择MASQUE出口=${initialized.masqueOuterSourceIp}，" +
                "Agent TUN=${initialized.agentTunCidr}，A2A=${initialized.agentTcpEndpoint}",
        )
        onResetAvailability(true)

        var lifecycleState = sdk.agentLifecycleState
        var profile = sdk.localProfile
        onLog(
            LabLogLevel.INFO,
            "AGENT STATE",
            "恢复状态=$lifecycleState，agent_id=${profile?.agentId ?: "<无>"}",
        )
        if (lifecycleState == AgentLifecycleState.NO_IDENTITY) {
            profile = retryableStep("H-ID", "状态1：申请 Agent 数字身份") {
                sdk.applyIdentity(
                    owner = config.owner,
                    name = config.agentName,
                    description = "Android ${config.role.name} MASQUE integration test",
                    metadata = buildJsonObject {
                        put("region", "CN")
                        put("os", "Android")
                        put("version", "0.2.40")
                    },
                )
            }
            lifecycleState = AgentLifecycleState.IDENTITY_READY
            onLog(LabLogLevel.SUCCESS, "H-ID", "agent_id=${profile.agentId}")
        } else {
            onLog(LabLogLevel.SUCCESS, "H-ID", "复用已保存身份 agent_id=${profile?.agentId}")
        }
        val activeProfile = checkNotNull(profile) { "Persisted Agent state has no profile" }
        localAgentId = activeProfile.agentId

        if (lifecycleState == AgentLifecycleState.IDENTITY_READY) {
            val networkAbility = retryableStep(
                "H-NETWORK-ABILITY",
                "状态2：获取运营商网络能力",
            ) { sdk.getNetworkAbility(activeProfile.agentId) }
            onLog(
                LabLogLevel.SUCCESS,
                "H-NETWORK-ABILITY",
                "network_abilities=${networkAbility.abilities.ifEmpty { listOf("<未声明>") }}",
            )
            retryableStep("H-PROFILE", "状态2：发布 Agent Card") {
                sdk.registerCapabilities(
                    agentId = activeProfile.agentId,
                    priority = 1,
                    credentials = listOf(networkAbility.abilityVc),
                    capabilities = if (config.role == TestRole.B) {
                        listOf(config.capability)
                    } else {
                        emptyList()
                    },
                    agentName = activeProfile.agentName,
                )
            }
            onLog(
                LabLogLevel.SUCCESS,
                "H-PROFILE",
                if (config.role == TestRole.B) {
                    "已发布能力 ${config.capability}，等待 Agent A 发现"
                } else {
                    "Agent A Profile 已发布"
                },
            )
        } else {
            onLog(LabLogLevel.SUCCESS, "H-PROFILE", "Agent Card 已发布，跳过重复 registerCapabilities")
        }
        emitFeatureState()
        if (config.role == TestRole.A) runAgentA(activeProfile) else runAgentB()
    }

    fun featureState(): SdkFeatureState = SdkFeatureState(
        cardPublished = sdk.agentLifecycleState == AgentLifecycleState.CARD_PUBLISHED,
        groupReady = manualMessageSession != null,
        computeSessionId = activeComputeSessionId,
        computeStatus = lastComputeStatus,
        videoUploadState = videoUploadHandle?.state,
        processedVideoState = processedVideoStream?.state,
        recognitionTargetRevision = recognitionTargetRevision,
        controlActionId = lastControlActionId,
        lastTranscription = lastTranscription,
    )

    private fun emitFeatureState() = onFeatureStateChanged(featureState())

    fun close() {
        onResetAvailability(false)
        onComputeActionAvailability(false)
        runCatching { networkListener?.close() }
        runCatching { groupListener?.close() }
        detachReceivedVideoSinks()
        onProcessedVideoStatus("视频已停止", "等待下一次处理流会话")
        manualMessageSession = null
        onManualMessageSession(null)
        retrySignal.close()
    }

    suspend fun resetAgent(): OperationResult {
        resetRequested = true
        retrySignal.trySend(Unit)
        return operationMutex.withLock { sdk.resetAgent() }
    }

    suspend fun stopComputingSession() = operationMutex.withLock {
        val sessionId = activeComputeSessionId
        val createRequestId = createComputeRequest?.requestId
        var sessionClosed = sessionId == null && createRequestId == null
        try {
            if (sessionId != null) {
                terminateComputingSession(ComputeRequestType.RELEASE)
                sessionClosed = true
            } else if (createRequestId != null) {
                onLog(
                    LabLogLevel.INFO,
                    "COMPUTE CANCEL",
                    "CREATE 尚未返回 session_id，按 target_request_id=$createRequestId 取消",
                )
                val status = sdk.cancelComputingSession(
                    computeRequest(
                        requestType = ComputeRequestType.CANCEL,
                        targetRequestId = createRequestId,
                    ),
                    timeoutSeconds = 30.0,
                )
                lastComputeStatus = status.status
                onLog(
                    LabLogLevel.SUCCESS,
                    "COMPUTE CANCEL",
                    "target_request_id=$createRequestId，status=${status.status}",
                )
                sessionClosed = true
            }
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            onLog(
                LabLogLevel.ERROR,
                "COMPUTE RELEASE",
                "释放失败：${error.message ?: error::class.java.simpleName}；继续清理本地媒体",
            )
            throw error
        } finally {
            withContext(NonCancellable) { closeLocalMedia() }
            if (sessionClosed) {
                activeComputeSessionId = null
                createComputeRequest = null
                lastComputeStatus = "CLOSED"
            }
            emitFeatureState()
        }
    }

    suspend fun deregisterAgentForStop(): OperationResult? =
        operationMutex.withLock { deregisterIdentityForStop(sdk, onLog) }

    internal fun diagnosticLocalTcpEndpoint(): String? = initResult?.agentTcpEndpoint

    internal fun diagnosticSummary(): String {
        val initialized = initResult
        val messaging = manualMessageSession
        val masqueStatistics = sdk.getMasqueTransportStatistics()
        return buildString {
            appendLine("lifecycle_state=${sdk.agentLifecycleState}")
            appendLine("agent_id=${sdk.localProfile?.agentId ?: "<none>"}")
            appendLine("init_complete=${initialized != null}")
            appendLine("init_runtime_connected=${initialized?.runtimeConnected ?: false}")
            appendLine("init_masque_connected=${initialized?.masqueConnected ?: false}")
            appendLine("agent_tun_cidr=${initialized?.agentTunCidr ?: "<unavailable>"}")
            appendLine("agent_tcp_endpoint=${initialized?.agentTcpEndpoint ?: "<unavailable>"}")
            appendLine("agent_udp_endpoint=${initialized?.agentUdpEndpoint ?: "<unavailable>"}")
            appendLine("masque_outer_source_ip=${initialized?.masqueOuterSourceIp ?: "<unavailable>"}")
            appendLine("masque_downlink_packets=${masqueStatistics.downlinkPackets}")
            appendLine("masque_downlink_packets_over_tun_mtu=${masqueStatistics.downlinkPacketsOverTunMtu}")
            appendLine("masque_downlink_read_buffer_too_small=${masqueStatistics.downlinkReadBufferTooSmall}")
            appendLine("masque_uplink_datagram_too_large=${masqueStatistics.uplinkDatagramTooLarge}")
            appendLine("masque_max_downlink_packet_bytes=${masqueStatistics.maxDownlinkPacketBytes}")
            appendLine("active_group_id=${messaging?.groupId ?: "<none>"}")
            appendLine("message_target_agent_id=${messaging?.targetAgentId ?: "<none>"}")
            appendLine("compute_service_session_id=${activeComputeSessionId ?: "<none>"}")
            appendLine("compute_status=${lastComputeStatus ?: "<none>"}")
            appendLine("video_upload_state=${videoUploadHandle?.state ?: "<none>"}")
            appendLine("processed_video_state=${processedVideoStream?.state ?: "<none>"}")
            appendLine("recognition_target_revision=${recognitionTargetRevision ?: "<none>"}")
            appendLine("last_control_action_id=${lastControlActionId ?: "<none>"}")
            append("last_transcription=${lastTranscription ?: "<none>"}")
        }
    }

    suspend fun sendManualMessage(content: String): MessageReceipt = sendMutex.withLock {
        operationMutex.withLock {
            ensureResetNotRequested()
            val normalized = content.trim()
            require(normalized.isNotEmpty()) { "消息内容不能为空" }
            val session = checkNotNull(manualMessageSession) { "群组尚未就绪，不能发送消息" }
            onLog(
                LabLogLevel.INFO,
                "A2A SEND",
                "to=${session.targetAgentName}(${session.targetAgentId})，" +
                    "group_id=${session.groupId}，content=${normalized.take(300)}",
            )
            try {
                sdk.sendMessage(
                    groupId = session.groupId,
                    targetAgentId = session.targetAgentId,
                    jsonMessage = buildJsonObject {
                        put("type", "text")
                        put("content", normalized)
                    },
                    messageType = "text",
                    taskId = "android-ab-manual-message",
                    timeoutSeconds = 10.0,
                ).also { receipt ->
                    check(receipt.delivered) { "对端未返回 status=OK" }
                    onLog(LabLogLevel.SUCCESS, "A2A SEND", "message_id=${receipt.messageId}，投递成功")
                }
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                onLog(
                    LabLogLevel.ERROR,
                    "A2A SEND",
                    "发送失败：${error.message ?: error::class.java.simpleName}；SDK 保持运行",
                )
                throw error
            }
        }
    }

    suspend fun inspectGroupSnapshot(): String = operationMutex.withLock {
        ensureResetNotRequested()
        val route = checkNotNull(manualMessageSession) { "群组尚未就绪" }
        val snapshot = checkNotNull(sdk.getGroupSnapshot(route.groupId)) { "SDK 中没有群组快照" }
        val summary = "group_id=${snapshot.groupId}，version=${snapshot.version}，" +
            "generation=${snapshot.generation}，members=" +
            snapshot.membersByAgentId.values.joinToString(prefix = "[", postfix = "]") { member ->
                "${member.agentName}:${member.agentIp}:${member.tcpPort}" +
                    member.capabilities.takeIf { it.isNotEmpty() }?.joinToString(
                        prefix = " skills=",
                        separator = ",",
                    ).orEmpty()
            }
        onLog(LabLogLevel.SUCCESS, "GROUP SNAPSHOT", summary)
        summary
    }

    suspend fun updatePublishedCapability(
        skillName: String,
        add: Boolean,
        credential: JsonObject? = null,
    ): OperationResult = operationMutex.withLock {
        ensureResetNotRequested()
        val normalizedSkill = skillName.trim().takeIf(String::isNotEmpty)
            ?: error("能力名称不能为空")
        val credentials = if (add) {
            val value = requireNotNull(credential) { "新增能力必须提供对应的 VC JSON" }
            listOf(value)
        } else {
            emptyList()
        }
        val referenceId = credentials.firstOrNull()
            ?.get("id")?.jsonPrimitive?.contentOrNull?.takeIf(String::isNotBlank)
        if (add && referenceId == null) error("能力 VC 必须包含非空 id")
        val profile = checkNotNull(sdk.localProfile) { "Agent Profile 尚未就绪" }
        credentials.firstOrNull()?.let { value ->
            val claims = value["claims"] as? JsonObject ?: error("能力 VC 必须包含 claims")
            check(claims["skill_name"]?.jsonPrimitive?.contentOrNull == normalizedSkill) {
                "能力 VC 的 claims.skill_name 与输入的技能名称不一致"
            }
            check(claims["agent_id"]?.jsonPrimitive?.contentOrNull == profile.agentId) {
                "能力 VC 的 claims.agent_id 与当前 Agent 不一致"
            }
        }
        val update = buildJsonObject {
            put("update_type", if (add) "add_skill" else "remove_skill")
            put("skill_name", normalizedSkill)
            referenceId?.let { put("reference_vc_id", it) }
        }
        onLog(
            LabLogLevel.INFO,
            "CAPABILITY UPDATE",
            "${if (add) "新增" else "删除"} skill=$normalizedSkill",
        )
        sdk.updateCapabilities(profile.agentId, listOf(update), credentials).also { result ->
            check(result.success) { result.message.ifBlank { "Agent Card 更新被 Runtime 拒绝" } }
            onLog(
                LabLogLevel.SUCCESS,
                "CAPABILITY UPDATE",
                "skill=$normalizedSkill 更新成功；agent_id 保持 ${profile.agentId}",
            )
            emitFeatureState()
        }
    }

    suspend fun queryActiveComputingSession(): String = operationMutex.withLock {
        ensureResetNotRequested()
        val request = computeTargetRequest(ComputeRequestType.QUERY)
        val status = sdk.queryComputingSession(request, timeoutSeconds = 30.0)
        lastComputeStatus = status.status
        val summary = computeStatusSummary(status.status, status.cause, status.computeServiceSessionId)
        onLog(LabLogLevel.SUCCESS, "COMPUTE QUERY", summary)
        emitFeatureState()
        summary
    }

    suspend fun cancelActiveComputingSession(): String = operationMutex.withLock {
        ensureResetNotRequested()
        terminateComputingSession(ComputeRequestType.CANCEL)
    }

    suspend fun releaseActiveComputingSession(): String = operationMutex.withLock {
        ensureResetNotRequested()
        terminateComputingSession(ComputeRequestType.RELEASE)
    }

    suspend fun toggleVideoUpload(): String = operationMutex.withLock {
        ensureResetNotRequested()
        val upload = checkNotNull(videoUploadHandle) { "B 端视频上传尚未启动" }
        if (upload.state == "PAUSED") upload.resume() else upload.pause()
        val state = upload.state
        onLog(
            LabLogLevel.SUCCESS,
            "VIDEO UPLOAD",
            if (state == "PAUSED") "摄像头上传已暂停" else "摄像头上传已恢复",
        )
        emitFeatureState()
        state
    }

    suspend fun updateRecognitionTarget(text: String): String = operationMutex.withLock {
        ensureResetNotRequested()
        val sessionId = requireConsumerSessionId()
        val normalized = text.trim().takeIf(String::isNotEmpty) ?: error("识别目标不能为空")
        val target = sdk.updateRecognitionTarget(
            computeServiceSessionId = sessionId,
            requestId = UUID.randomUUID().toString(),
            text = normalized,
            language = "zh",
        )
        recognitionTargetRevision = target.targetRevision
        val summary = "status=${target.status}，revision=${target.targetRevision}，" +
            "label=${target.target.label}，prompt=${target.target.prompt}"
        onLog(LabLogLevel.SUCCESS, "RECOGNITION PUT", summary)
        emitFeatureState()
        summary
    }

    suspend fun getRecognitionTarget(): String = operationMutex.withLock {
        ensureResetNotRequested()
        val target = sdk.getRecognitionTarget(requireConsumerSessionId())
        recognitionTargetRevision = target.targetRevision
        val summary = "status=${target.status}，revision=${target.targetRevision}，" +
            "label=${target.target.label}，prompt=${target.target.prompt}"
        onLog(LabLogLevel.SUCCESS, "RECOGNITION GET", summary)
        emitFeatureState()
        summary
    }

    suspend fun createControlAction(text: String): String = operationMutex.withLock {
        ensureResetNotRequested()
        val normalized = text.trim().takeIf(String::isNotEmpty) ?: error("控制指令不能为空")
        val action = sdk.createControlAction(
            requireConsumerSessionId(),
            ControlActionRequest(
                requestId = UUID.randomUUID().toString(),
                inputType = ControlInputType.TEXT,
                text = normalized,
                language = "zh",
            ),
        )
        lastControlActionId = action.actionId
        val summary = "action_id=${action.actionId}，status=${action.status}，" +
            "normalized_action=${action.normalizedAction ?: "<none>"}"
        onLog(LabLogLevel.SUCCESS, "CONTROL CREATE", summary)
        emitFeatureState()
        summary
    }

    suspend fun transcribeAudio(
        audio: ByteArray,
        fileName: String,
        contentType: String,
    ): String = operationMutex.withLock {
        ensureResetNotRequested()
        val operationId = UUID.randomUUID().toString()
        val transcription = sdk.transcribeAudio(
            asrUrl = "http://${config.serverIp}:9004/api/v1/transcribe",
            request = AudioTranscriptionRequest(
                audio = audio,
                fileName = fileName,
                contentType = contentType,
                sessionId = activeComputeSessionId ?: "agent-link-$operationId",
                taskId = "asr-$operationId",
                source = "android-${config.role.name.lowercase()}",
                language = "zh",
            ),
        )
        lastTranscription = transcription.text
        val summary = "transcript_id=${transcription.transcriptId}，" +
            "language=${transcription.language ?: "<unknown>"}，text=${transcription.text}"
        onLog(LabLogLevel.SUCCESS, "ASR 9004", summary)
        emitFeatureState()
        summary
    }

    suspend fun createAudioControlAction(
        audio: ByteArray,
        fileName: String,
        contentType: String,
    ): String = operationMutex.withLock {
        ensureResetNotRequested()
        val action = sdk.createAudioControlAction(
            computeServiceSessionId = requireConsumerSessionId(),
            request = AudioControlActionRequest(
                requestId = UUID.randomUUID().toString(),
                audio = audio,
                fileName = fileName,
                contentType = contentType,
                language = "zh",
            ),
        )
        lastControlActionId = action.actionId
        lastTranscription = action.transcription?.text
        val summary = "text=${action.transcription?.text ?: "<none>"}，" +
            "action_id=${action.actionId}，status=${action.status}，" +
            "normalized_action=${action.normalizedAction ?: "<none>"}"
        onLog(LabLogLevel.SUCCESS, "VOICE CONTROL", summary)
        emitFeatureState()
        summary
    }

    suspend fun getControlAction(): String = operationMutex.withLock {
        ensureResetNotRequested()
        val actionId = checkNotNull(lastControlActionId) { "尚未创建控制动作" }
        val action = sdk.getControlAction(requireConsumerSessionId(), actionId)
        val summary = "action_id=${action.actionId}，status=${action.status}，" +
            "cause=${action.cause.ifBlank { "<none>" }}，result=${action.result ?: "<none>"}"
        onLog(LabLogLevel.SUCCESS, "CONTROL GET", summary)
        emitFeatureState()
        summary
    }

    suspend fun startVideoOffload() = operationMutex.withLock {
        ensureResetNotRequested()
        onComputeActionAvailability(false)
        try {
            if (config.role == TestRole.A) createAndReceiveProcessedVideo()
            else startProducerVideoUpload()
        } catch (error: Throwable) {
            onComputeActionAvailability(true)
            throw error
        }
    }

    private suspend fun createAndReceiveProcessedVideo() {
        processedVideoStream?.let { return }
        val route = checkNotNull(manualMessageSession) { "群组尚未就绪，不能申请算力会话" }
        val request = createComputeRequest ?: ComputeSessionRequest(
            messageType = COMPUTE_REQUEST_MESSAGE_TYPE,
            requestType = ComputeRequestType.CREATE,
            inputFormat = ComputeInputFormat.STRUCTURED,
            requestId = UUID.randomUUID().toString(),
            acnContext = AcnContext(
                groupId = route.groupId,
                requesterAgentId = route.localAgentId,
                targetAgentId = route.targetAgentId,
            ),
            constraints = ComputeConstraints(
                capabilityId = config.capability,
                dnn = config.dnn,
                allowBaseQos = true,
            ),
        ).also { createComputeRequest = it }
        onLog(
            LabLogLevel.INFO,
            "COMPUTE CREATE",
            "A 调用 createComputingSession，group_id=${route.groupId}，target=${route.targetAgentId}",
        )
        val status = sdk.createComputingSession(request, timeoutSeconds = 30.0)
        val sessionId = status.computeServiceSessionId?.takeIf(String::isNotBlank)
            ?: error("CREATE 响应缺少 compute_service_session_id（status=${status.status}, cause=${status.cause}）")
        activeComputeSessionId = sessionId
        lastComputeStatus = status.status
        emitFeatureState()
        onLog(
            LabLogLevel.SUCCESS,
            "COMPUTE CREATE",
            "request_id=${request.requestId}，session_id=$sessionId，status=${status.status}",
        )
        val delivery = sdk.sendMessage(
            groupId = route.groupId,
            targetAgentId = route.targetAgentId,
            jsonMessage = buildJsonObject { put(COMPUTE_SESSION_ID_FIELD, sessionId) },
            messageType = COMPUTE_SESSION_MESSAGE_TYPE,
            taskId = "computing:$sessionId",
            timeoutSeconds = 10.0,
        )
        check(delivery.delivered) { "compute_service_session_id 未送达 ${route.targetAgentName}" }
        onLog(
            LabLogLevel.SUCCESS,
            "COMPUTE NOTIFY",
            "只向 ${route.targetAgentName} 发送 compute_service_session_id；等待本端 consumer C-02",
        )
        connectProcessedVideo(sessionId)
    }

    private suspend fun startProducerVideoUpload() {
        videoUploadHandle?.let { return }
        val sessionId = activeComputeSessionId
            ?: error("尚未收到 Agent A 的 compute_service_session_id")
        onLog(
            LabLogLevel.INFO,
            "VIDEO UPLOAD",
            "默认采集本机 0 号摄像头；等待 producer C-02 后开始 WebRTC 协商",
        )
        videoUploadHandle = sdk.startVideoUpload(
            computeServiceSessionId = sessionId,
            cameraId = "0",
            width = 640,
            height = 480,
            fps = 30,
            bitrateKbps = 2400,
            timeoutSeconds = 120.0,
        ).also { handle ->
            onLog(
                LabLogLevel.SUCCESS,
                "VIDEO UPLOAD",
                "producer media connection 已建立；session_id=$sessionId，track=${handle.trackId}",
            )
            onStatus(RunnerStatus("视频上传已启动", "B → Sandbox；等待 A 接收处理流"))
            emitFeatureState()
        }
    }

    private fun installListeners() {
        networkListener = sdk.registerNetworkMessageListener(
            NetworkMessageListener { type, payload ->
                when (type) {
                    NetworkMessageType.GROUP_INVITATION -> {
                        onLog(
                            LabLogLevel.SUCCESS,
                            "DOWNLINK",
                            "收到建组邀请并自动 ACCEPT：${compact(payload)}",
                        )
                        NetworkMessageAction.ACCEPT
                    }
                    NetworkMessageType.GROUP_CONFIG -> {
                        val groupId = payload["group_id"]?.jsonPrimitive?.content
                        onLog(LabLogLevel.SUCCESS, "DOWNLINK", "群组配置已缓存，group_id=$groupId")
                        if (!groupId.isNullOrBlank()) activateManualMessaging(groupId)
                        NetworkMessageAction.ACK
                    }
                    NetworkMessageType.UNKNOWN -> {
                        onLog(LabLogLevel.WARNING, "DOWNLINK", "忽略未知消息：${compact(payload)}")
                        NetworkMessageAction.REJECT
                    }
                }
            },
        )
        groupListener = sdk.registerGroupMessageListener(
            GroupMessageListener { groupId, senderAgentId, payload ->
                onLog(
                    LabLogLevel.SUCCESS,
                    "A2A RECEIVE",
                    "group_id=$groupId，from=$senderAgentId，payload=${compact(payload)}",
                )
                if (payload.containsKey(COMPUTE_SESSION_ID_FIELD)) {
                    receiveComputeSessionId(groupId, senderAgentId, payload)
                } else {
                    onStatus(RunnerStatus("已收到 A2A 消息", "链路验证成功，SDK 继续运行"))
                }
            },
        )
    }

    private fun receiveComputeSessionId(
        groupId: String,
        senderAgentId: String,
        payload: JsonObject,
    ) {
        if (config.role != TestRole.B) {
            onLog(LabLogLevel.WARNING, "COMPUTE NOTIFY", "Agent A 忽略意外的算力 session 通知")
            return
        }
        val sessionId = payload[COMPUTE_SESSION_ID_FIELD]
            ?.jsonPrimitive?.contentOrNull?.takeIf(String::isNotBlank)
        if (sessionId == null) {
            onLog(LabLogLevel.ERROR, "COMPUTE NOTIFY", "session 通知缺少 compute_service_session_id")
            return
        }
        val route = manualMessageSession
        if (route == null) {
            pendingComputeNotification = PendingComputeNotification(groupId, senderAgentId, sessionId)
            onLog(LabLogLevel.INFO, "COMPUTE NOTIFY", "群组配置尚未提交，暂存 session 通知")
            return
        }
        if (route.groupId != groupId || route.targetAgentId != senderAgentId) {
            onLog(LabLogLevel.ERROR, "COMPUTE NOTIFY", "session 通知的群组或发送方与当前路由不一致")
            return
        }
        if (activeComputeSessionId == sessionId && videoUploadHandle != null) return
        activeComputeSessionId = sessionId
        emitFeatureState()
        onLog(
            LabLogLevel.SUCCESS,
            "COMPUTE NOTIFY",
            "收到 session_id=$sessionId；Sandbox 配置继续由 SDK 内部等待 C-02",
        )
        onStatus(RunnerStatus("算力会话已下发", "正在准备 B 端摄像头和 producer WebRTC"))
        onComputeActionAvailability(true)
        onProducerSessionReady()
    }

    private suspend fun runAgentA(profile: AgentProfile) {
        val discovered = retryableStep("H-DISCOVERY", "按能力发现 Agent B") {
            sdk.discoverAgents(
                agentId = profile.agentId,
                taskDescription = "Android A/B MASQUE end-to-end test",
                requiredSkills = listOf(config.capability),
                maxResults = 10,
            ).firstOrNull { candidate ->
                candidate.agentId != profile.agentId && config.capability in candidate.skills
            } ?: error("没有发现声明 ${config.capability} 能力的 Agent B")
        }
        onLog(
            LabLogLevel.SUCCESS,
            "H-DISCOVERY",
            "发现 Agent B=${discovered.agentId}，endpoint=${discovered.serviceEndpoints}",
        )
        val group = retryableStep("H-GROUP", "与 Agent B 建组") {
            sdk.createGroup(
                agentId = profile.agentId,
                targetAgentIds = listOf(discovered.agentId),
                groupName = config.groupName,
                dnn = config.dnn,
                maxMembers = 2,
            )
        }
        onLog(LabLogLevel.SUCCESS, "H-GROUP", "group_id=${group.groupId}")
        retryableStep("GROUP CONFIG", "等待双方群组配置", serialized = false) {
            withTimeout(120_000) {
                while (sdk.getGroupSnapshot(group.groupId) == null) delay(500)
            }
        }
        onLog(LabLogLevel.SUCCESS, "GROUP CONFIG", "成员路由已进入 SDK 缓存")
        activateManualMessaging(group.groupId)
        waitUntilCancelled()
    }

    private suspend fun runAgentB() {
        onStatus(RunnerStatus("Agent B 已就绪", "收到算力会话后默认启动本机 0 号摄像头"))
        onLog(
            LabLogLevel.INFO,
            "READY",
            "等待建组邀请、群组配置和 compute_service_session_id；视频源=本机 0 号摄像头",
        )
        waitUntilCancelled()
    }

    private suspend fun activateManualMessaging(groupId: String) {
        val localId = localAgentId ?: run {
            onLog(LabLogLevel.WARNING, "A2A READY", "本端 Agent ID 尚未就绪")
            return
        }
        val snapshot = sdk.getGroupSnapshot(groupId) ?: run {
            onLog(LabLogLevel.WARNING, "A2A READY", "群组配置尚未进入 SDK 缓存")
            return
        }
        val session = try {
            selectManualMessageSession(snapshot, localId)
        } catch (error: Exception) {
            onLog(LabLogLevel.ERROR, "A2A READY", error.message ?: "无法解析群组对端")
            return
        }
        if (manualMessageSession == session) return
        manualMessageSession = session
        onManualMessageSession(session)
        emitFeatureState()
        onComputeActionAvailability(config.role == TestRole.A && processedVideoStream == null)
        pendingComputeNotification?.let { pending ->
            pendingComputeNotification = null
            receiveComputeSessionId(
                pending.groupId,
                pending.senderAgentId,
                buildJsonObject { put(COMPUTE_SESSION_ID_FIELD, pending.sessionId) },
            )
        }
        onLog(
            LabLogLevel.SUCCESS,
            "A2A READY",
            "${config.role.name} → ${session.targetAgentName}，手动发送已启用",
        )
        onStatus(
            RunnerStatus(
                if (config.role == TestRole.A) "群组已就绪 · 可申请算力会话" else "群组已就绪 · 等待算力会话",
                "目标 ${session.targetAgentName} · ${session.groupId}",
            ),
        )
    }

    private suspend fun connectProcessedVideo(sessionId: String) {
        try {
            val stream = sdk.getProcessedVideoStream(sessionId, timeoutSeconds = 120.0)
            processedVideoStream = stream
            emitFeatureState()
            val track = stream.track
            var frames = 0L
            val diagnosticSink = VideoSink { frame ->
                frames += 1
                if (frames == 1L || frames % 120L == 0L) {
                    onLog(
                        LabLogLevel.SUCCESS,
                        "VIDEO FRAME",
                        "processed track=${track.trackId}，frames=$frames，" +
                            "${frame.buffer.width}x${frame.buffer.height}",
                    )
                    if (frames == 1L) {
                        onStatus(RunnerStatus("已收到处理后视频首帧", "Sandbox WebRTC 下行正常"))
                    }
                }
            }
            onProcessedVideoStatus("处理流已连接", "等待 Sandbox 的第一帧")
            val sinks = processedVideoRenderSinks() + diagnosticSink
            val attachedSinks = mutableListOf<VideoSink>()
            try {
                sinks.forEach { sink ->
                    track.addSink(sink)
                    attachedSinks += sink
                }
            } catch (error: Throwable) {
                attachedSinks.forEach { sink -> runCatching { track.removeSink(sink) } }
                stream.close()
                processedVideoStream = null
                throw error
            }
            synchronized(receivedVideoSinks) {
                receivedVideoSinks += attachedSinks.map { sink -> track to sink }
            }
            onLog(
                LabLogLevel.SUCCESS,
                "VIDEO STREAM",
                "getProcessedVideoStream 返回 session_id=$sessionId，track=${track.trackId}",
            )
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            onLog(
                LabLogLevel.ERROR,
                "VIDEO STREAM",
                "处理流接收失败：${error.message ?: error::class.java.simpleName}",
            )
            onStatus(RunnerStatus("处理流接收失败", error.message ?: error::class.java.simpleName))
            onProcessedVideoStatus("处理流接收失败", error.message ?: error::class.java.simpleName)
            throw error
        }
    }

    private suspend fun closeLocalMedia() {
        val upload = videoUploadHandle
        val stream = processedVideoStream
        videoUploadHandle = null
        processedVideoStream = null
        detachReceivedVideoSinks()
        upload?.let { runCatching { it.stop() } }
        stream?.let { runCatching { it.close() } }
        onComputeActionAvailability(false)
        onProcessedVideoStatus("视频已停止", "等待下一次处理流会话")
        emitFeatureState()
    }

    private fun requireConsumerSessionId(): String {
        check(config.role == TestRole.A) { "该接口只适用于 consumer（角色 A）" }
        check(processedVideoStream != null) { "处理流尚未建立，无法访问 Sandbox 运行期接口" }
        return checkNotNull(activeComputeSessionId) { "当前没有活动算力会话" }
    }

    private fun computeTargetRequest(requestType: ComputeRequestType): ComputeSessionRequest {
        val sessionId = activeComputeSessionId
        val targetRequestId = createComputeRequest?.requestId
        check(sessionId != null || targetRequestId != null) { "当前没有可查询或取消的算力请求" }
        return computeRequest(
            requestType = requestType,
            computeServiceSessionId = sessionId,
            targetRequestId = if (sessionId == null) targetRequestId else null,
        )
    }

    private fun computeRequest(
        requestType: ComputeRequestType,
        computeServiceSessionId: String? = null,
        targetRequestId: String? = null,
    ) = ComputeSessionRequest(
        messageType = COMPUTE_REQUEST_MESSAGE_TYPE,
        requestType = requestType,
        inputFormat = ComputeInputFormat.STRUCTURED,
        requestId = UUID.randomUUID().toString(),
        computeServiceSessionId = computeServiceSessionId,
        targetRequestId = targetRequestId,
    )

    private suspend fun terminateComputingSession(requestType: ComputeRequestType): String {
        check(requestType == ComputeRequestType.CANCEL || requestType == ComputeRequestType.RELEASE)
        val sessionId = activeComputeSessionId
        val request = when (requestType) {
            ComputeRequestType.CANCEL -> computeTargetRequest(requestType)
            ComputeRequestType.RELEASE -> computeRequest(
                requestType = requestType,
                computeServiceSessionId = checkNotNull(sessionId) { "当前没有可释放的算力会话" },
            )
            else -> error("Unsupported termination request: $requestType")
        }
        val stage = "COMPUTE ${requestType.name}"
        onLog(
            LabLogLevel.INFO,
            stage,
            sessionId?.let { "session_id=$it" }
                ?: "target_request_id=${request.targetRequestId}",
        )
        val status = when (requestType) {
            ComputeRequestType.CANCEL -> sdk.cancelComputingSession(request, timeoutSeconds = 30.0)
            ComputeRequestType.RELEASE -> sdk.releaseComputingSession(request, timeoutSeconds = 30.0)
            else -> error("Unsupported termination request: $requestType")
        }
        lastComputeStatus = status.status
        onLog(
            LabLogLevel.SUCCESS,
            stage,
            computeStatusSummary(status.status, status.cause, status.computeServiceSessionId),
        )
        if (sessionId != null) {
            sdk.awaitComputingSessionClosed(sessionId, timeoutSeconds = 30.0)
            onLog(
                LabLogLevel.SUCCESS,
                stage,
                "已收到 C-05 并清理本地算力配置；现在允许清除 Agent Profile",
            )
        }
        withContext(NonCancellable) { closeLocalMedia() }
        activeComputeSessionId = null
        createComputeRequest = null
        lastComputeStatus = "CLOSED"
        recognitionTargetRevision = null
        lastControlActionId = null
        lastTranscription = null
        onComputeActionAvailability(config.role == TestRole.A && manualMessageSession != null)
        emitFeatureState()
        return "${requestType.name} 已完成，C-05 已确认，本地状态=CLOSED"
    }

    private fun computeStatusSummary(status: String, cause: String, sessionId: String?): String =
        "status=$status，session_id=${sessionId ?: activeComputeSessionId ?: "<none>"}，" +
            "cause=${cause.ifBlank { "<none>" }}"

    private fun detachReceivedVideoSinks() {
        synchronized(receivedVideoSinks) {
            receivedVideoSinks.forEach { (track, sink) -> runCatching { track.removeSink(sink) } }
            receivedVideoSinks.clear()
        }
    }

    private suspend fun waitUntilCancelled(): Nothing {
        while (true) {
            currentCoroutineContext().ensureActive()
            delay(60_000)
        }
    }

    private suspend fun <T> retryableStep(
        stage: String,
        title: String,
        serialized: Boolean = true,
        call: suspend () -> T,
    ): T {
        var attempt = 1
        while (true) {
            currentCoroutineContext().ensureActive()
            ensureResetNotRequested()
            onStatus(RunnerStatus(title, "第 $attempt 次调用中"))
            onLog(LabLogLevel.INFO, stage, "调用开始（attempt=$attempt）")
            try {
                val result = if (serialized) {
                    operationMutex.withLock {
                        ensureResetNotRequested()
                        call()
                    }
                } else {
                    call()
                }
                ensureResetNotRequested()
                return result.also { onLog(LabLogLevel.SUCCESS, stage, "调用完成") }
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                val detail = error.message?.takeIf(String::isNotBlank)
                    ?: error::class.java.simpleName
                onLog(LabLogLevel.ERROR, stage, "$detail；SDK 未关闭")
                onLog(
                    LabLogLevel.WARNING,
                    stage,
                    "写接口超时不代表网侧一定失败；联调前请先核对服务端状态，再重试",
                )
                onStatus(RunnerStatus("$title 失败", detail, canRetry = true))
                retrySignal.receive()
                attempt += 1
            }
        }
    }

    private fun ensureResetNotRequested() {
        if (resetRequested) throw CancellationException("Agent reset requested")
    }

    private fun compact(value: JsonObject): String = redactSensitiveJson(value).toString().take(800)

    private fun redactSensitiveJson(value: JsonElement): JsonElement = when (value) {
        is JsonObject -> JsonObject(
            value.mapValues { (key, nested) ->
                if (key in SENSITIVE_LOG_FIELDS) JsonPrimitive("<redacted>")
                else redactSensitiveJson(nested)
            },
        )
        is JsonArray -> JsonArray(value.map(::redactSensitiveJson))
        else -> value
    }

    private companion object {
        const val COMPUTE_REQUEST_MESSAGE_TYPE = "COMPUTE_SESSION_REQUEST"
        const val COMPUTE_SESSION_MESSAGE_TYPE = "computing_video_session"
        const val COMPUTE_SESSION_ID_FIELD = "compute_service_session_id"
        val SENSITIVE_LOG_FIELDS = emptySet<String>()
    }
}
