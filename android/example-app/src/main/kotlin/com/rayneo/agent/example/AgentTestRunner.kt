package com.rayneo.agent.example

import com.rayneo.agent.sdk.AgentSdk
import com.rayneo.agent.sdk.model.AgentLifecycleState
import com.rayneo.agent.sdk.model.AgentProfile
import com.rayneo.agent.sdk.model.GroupConfigSnapshot
import com.rayneo.agent.sdk.model.MessageReceipt
import com.rayneo.agent.sdk.model.NetworkMessageAction
import com.rayneo.agent.sdk.model.NetworkMessageType
import com.rayneo.agent.sdk.model.OffloadingSession
import com.rayneo.agent.sdk.model.OperationResult
import com.rayneo.agent.sdk.model.ProcessedVideoEndpoint
import com.rayneo.agent.sdk.model.SandboxSpec
import com.rayneo.agent.sdk.model.SdkInitResult
import com.rayneo.agent.sdk.transport.GroupMessageListener
import com.rayneo.agent.sdk.transport.NetworkMessageListener
import com.rayneo.agent.sdk.transport.VideoTrack
import com.rayneo.agent.sdk.transport.VideoUploadHandle
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.webrtc.VideoSink
import java.time.Instant

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
    private val onVideoUploadAvailability: (Boolean) -> Unit = {},
    private val processedVideoRenderSinks: () -> List<VideoSink> = { emptyList() },
    private val onProcessedVideoStatus: (String, String) -> Unit = { _, _ -> },
) {
    private val mediaScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val retrySignal = Channel<Unit>(Channel.CONFLATED)
    private val sendMutex = Mutex()
    private val operationMutex = Mutex()
    private var networkListener: AutoCloseable? = null
    private var groupListener: AutoCloseable? = null
    @Volatile
    private var localAgentId: String? = null
    @Volatile
    private var initResult: SdkInitResult? = null
    @Volatile
    private var manualMessageSession: ManualMessageSession? = null
    @Volatile
    private var resetRequested = false
    @Volatile
    private var videoUploadHandle: VideoUploadHandle? = null
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
                computeControlIp = config.computeControlIp,
                computeControlPort = config.computeControlPort,
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
                        put("version", "0.2.14")
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
            ) {
                sdk.getNetworkAbility(activeProfile.agentId)
            }
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
            onLog(
                LabLogLevel.SUCCESS,
                "H-PROFILE",
                "Agent Card 已发布，跳过重复 registerCapabilities",
            )
        }

        if (config.role == TestRole.A) {
            runAgentA(activeProfile)
        } else {
            runAgentB()
        }
    }

    fun close() {
        onResetAvailability(false)
        onVideoUploadAvailability(false)
        runCatching { networkListener?.close() }
        runCatching { groupListener?.close() }
        receivedVideoSinks.forEach { (track, sink) -> runCatching { track.removeSink(sink) } }
        receivedVideoSinks.clear()
        mediaScope.cancel()
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

    suspend fun deregisterAgentForStop(): OperationResult? =
        operationMutex.withLock { deregisterIdentityForStop(sdk, onLog) }

    internal fun diagnosticLocalTcpEndpoint(): String? = initResult?.agentTcpEndpoint

    internal fun diagnosticSummary(): String {
        val initialized = initResult
        val messaging = manualMessageSession
        val upload = videoUploadHandle
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
            append("video_upload_state=${upload?.state ?: "<none>"}")
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
                    onLog(
                        LabLogLevel.SUCCESS,
                        "A2A SEND",
                        "message_id=${receipt.messageId}，投递成功",
                    )
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

    suspend fun startVideoOffload(): VideoUploadHandle = operationMutex.withLock {
        ensureResetNotRequested()
        check(config.role == TestRole.B) { "只有 Agent B 可以启动视频上传" }
        videoUploadHandle?.let { return@withLock it }
        val route = checkNotNull(manualMessageSession) { "群组尚未就绪，不能启动视频算力测试" }
        onVideoUploadAvailability(false)
        onLog(
            LabLogLevel.INFO,
            "COMPUTE CREATE",
            "调用 createOffloadingSession，sandbox=2 vCPU/4096 MiB，Mock=${config.computeControlIp}:${config.computeControlPort}",
        )
        try {
            val session = sdk.createOffloadingSession(
                workloadType = "video_relay",
                sandboxSpec = SandboxSpec(vcpus = 2, memoryMb = 4096),
                timeoutSeconds = 30.0,
            )
            onLog(
                LabLogLevel.SUCCESS,
                "COMPUTE CREATE",
                "session_id=${session.sessionId}，video_server=${session.producer?.videoServerIp}",
            )
            onLog(
                LabLogLevel.INFO,
                "VIDEO UPLOAD",
                "应用通过 sendMessage 向 ${route.targetAgentName} 发送 session 信息，然后启动 B→Server 上传",
            )
            val delivery = sdk.sendMessage(
                route.groupId,
                route.targetAgentId,
                buildProcessedVideoSessionMessage(session),
                messageType = "processed_video_session",
                taskId = "offloading:${session.sessionId}",
                timeoutSeconds = 10.0,
            )
            check(delivery.delivered) { "处理流 session 信息未送达 ${route.targetAgentName}" }
            sdk.startVideoUpload(
                session = session,
                cameraId = "0",
                width = 640,
                height = 480,
                fps = 30,
                bitrateKbps = 2400,
            ).also { handle ->
                videoUploadHandle = handle
                onLog(
                    LabLogLevel.SUCCESS,
                    "VIDEO UPLOAD",
                    "服务端已拉到首帧；${route.targetAgentName} 的预建下行保持复用；track=${handle.trackId}",
                )
                onStatus(RunnerStatus("视频算力链路已启动", "B → Mock Video Server → ${route.targetAgentName}"))
            }
        } catch (error: Throwable) {
            onVideoUploadAvailability(true)
            throw error
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
                        onLog(
                            LabLogLevel.SUCCESS,
                            "DOWNLINK",
                            "群组配置已缓存，group_id=$groupId",
                        )
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
                if (payload["type"]?.jsonPrimitive?.content == "processed_video_session") {
                    onProcessedVideoStatus("正在连接处理流", "应用已收到 session 信息，正在完成 WebRTC 协商")
                    onLog(
                        LabLogLevel.INFO,
                        "VIDEO SESSION",
                        "应用解析 P2P session 信息并调用 getProcessedVideoStream",
                    )
                    mediaScope.launch { receiveProcessedVideo(payload) }
                } else {
                    onStatus(RunnerStatus("已收到 A2A 消息", "链路验证成功，SDK 继续运行"))
                }
            },
        )
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
                while (sdk.getGroupSnapshot(group.groupId) == null) {
                    delay(500)
                }
            }
        }
        onLog(LabLogLevel.SUCCESS, "GROUP CONFIG", "成员路由已进入 SDK 缓存")
        activateManualMessaging(group.groupId)
        waitUntilCancelled()
    }

    private suspend fun runAgentB() {
        onStatus(RunnerStatus("Agent B 已就绪", "请启动 Agent A；邀请将自动接受"))
        onLog(LabLogLevel.INFO, "READY", "等待建组邀请、群组配置和 A2A 消息")
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
        onVideoUploadAvailability(config.role == TestRole.B && videoUploadHandle == null)
        onLog(
            LabLogLevel.SUCCESS,
            "A2A READY",
            "${config.role.name} → ${session.targetAgentName}，手动发送已启用",
        )
        onStatus(
            RunnerStatus(
                "群组已就绪 · 可双向发送",
                "目标 ${session.targetAgentName} · ${session.groupId}",
            ),
        )
    }

    private suspend fun receiveProcessedVideo(
        invitation: JsonObject,
    ) {
        try {
            val track = operationMutex.withLock {
                val session = parseProcessedVideoSession(invitation)
                onLog(
                    LabLogLevel.SUCCESS,
                    "VIDEO SESSION",
                    "session_id=${session.sessionId}，video_server=${session.processedStream?.videoServerIp}",
                )
                sdk.getProcessedVideoStream(session, timeoutSeconds = 20.0)
            }
            var frames = 0L
            val sink = VideoSink { frame ->
                frames += 1
                if (frames == 1L || frames % 120L == 0L) {
                    onLog(
                        LabLogLevel.SUCCESS,
                        "VIDEO FRAME",
                        "processed track=${track.trackId}，frames=$frames，${frame.buffer.width}x${frame.buffer.height}",
                    )
                    if (frames == 1L) {
                        onStatus(RunnerStatus("已收到处理后视频首帧", "Mock Video Server WebRTC 下行正常"))
                    }
                }
            }
            onProcessedVideoStatus("处理流已连接", "等待 Video Server 的第一帧")
            val sinks = buildList {
                addAll(processedVideoRenderSinks())
                add(sink)
            }
            val attachedSinks = mutableListOf<VideoSink>()
            try {
                sinks.forEach { renderSink ->
                    track.addSink(renderSink)
                    attachedSinks += renderSink
                }
            } catch (error: Throwable) {
                attachedSinks.forEach { attached -> runCatching { track.removeSink(attached) } }
                throw error
            }
            synchronized(receivedVideoSinks) {
                receivedVideoSinks += attachedSinks.map { renderSink -> track to renderSink }
            }
            onLog(LabLogLevel.SUCCESS, "VIDEO STREAM", "getProcessedVideoStream 返回 track=${track.trackId}")
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            onLog(
                LabLogLevel.ERROR,
                "VIDEO STREAM",
                "处理流接收失败：${error.message ?: error::class.java.simpleName}",
            )
            onStatus(RunnerStatus("处理流接收失败", error.message ?: error::class.java.simpleName))
            onProcessedVideoStatus(
                "处理流接收失败",
                error.message ?: error::class.java.simpleName,
            )
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
                return result.also {
                    onLog(LabLogLevel.SUCCESS, stage, "调用完成")
                }
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

    private fun buildProcessedVideoSessionMessage(
        session: OffloadingSession,
    ): JsonObject {
        val endpoint = checkNotNull(session.processedStream) {
            "createOffloadingSession 未返回 processed_stream"
        }
        return buildJsonObject {
            put("type", "processed_video_session")
            put("version", "1.0")
            put("session_id", session.sessionId)
            put("state", session.state)
            session.expiresAt?.let { put("expires_at", it.toString()) }
            put("processed_stream", buildJsonObject {
                put("video_server_ip", endpoint.videoServerIp)
                put("offer_url", endpoint.offerUrl)
                put("protocol", endpoint.protocol)
                put("signaling", endpoint.signaling)
            })
        }
    }

    private fun parseProcessedVideoSession(
        payload: JsonObject,
    ): OffloadingSession {
        fun required(field: String): String =
            payload[field]?.jsonPrimitive?.contentOrNull?.takeIf { it.isNotBlank() }
                ?: error("session 信息缺少 $field")
        check(required("type") == "processed_video_session") { "不支持的 session 消息类型" }
        check(required("version") == "1.0") { "不支持的 session 消息版本" }
        val endpointJson = payload["processed_stream"]?.jsonObject
            ?: error("session 信息缺少 processed_stream")
        fun endpointField(field: String): String =
            endpointJson[field]?.jsonPrimitive?.contentOrNull?.takeIf { it.isNotBlank() }
                ?: error("processed_stream 缺少 $field")
        val expiresAt = payload["expires_at"]?.jsonPrimitive?.contentOrNull?.let(Instant::parse)
        check(expiresAt == null || expiresAt.isAfter(Instant.now())) { "session 已过期" }
        return OffloadingSession(
            sessionId = required("session_id"),
            state = payload["state"]?.jsonPrimitive?.contentOrNull ?: "ALLOCATED",
            expiresAt = expiresAt,
            processedStream = ProcessedVideoEndpoint(
                videoServerIp = endpointField("video_server_ip"),
                offerUrl = endpointField("offer_url"),
                protocol = endpointJson["protocol"]?.jsonPrimitive?.contentOrNull ?: "webrtc",
                signaling = endpointJson["signaling"]?.jsonPrimitive?.contentOrNull ?: "non-trickle",
            ),
        )
    }

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
        val SENSITIVE_LOG_FIELDS = emptySet<String>()
    }
}
