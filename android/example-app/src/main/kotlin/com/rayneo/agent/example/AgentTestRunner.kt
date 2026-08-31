package com.rayneo.agent.example

import com.rayneo.agent.sdk.AgentSdk
import com.rayneo.agent.sdk.model.AgentLifecycleState
import com.rayneo.agent.sdk.model.AgentProfile
import com.rayneo.agent.sdk.model.GroupConfigSnapshot
import com.rayneo.agent.sdk.model.MessageReceipt
import com.rayneo.agent.sdk.model.NetworkMessageAction
import com.rayneo.agent.sdk.model.NetworkMessageType
import com.rayneo.agent.sdk.transport.GroupMessageListener
import com.rayneo.agent.sdk.transport.NetworkMessageListener
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

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

class AgentTestRunner(
    private val sdk: AgentSdk,
    private val config: TestConfig,
    private val onLog: (LabLogLevel, String, String) -> Unit,
    private val onStatus: (RunnerStatus) -> Unit,
    private val onManualMessageSession: (ManualMessageSession?) -> Unit,
) {
    private val retrySignal = Channel<Unit>(Channel.CONFLATED)
    private val sendMutex = Mutex()
    private var networkListener: AutoCloseable? = null
    private var groupListener: AutoCloseable? = null
    @Volatile
    private var localAgentId: String? = null
    @Volatile
    private var manualMessageSession: ManualMessageSession? = null

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

        val initResult = retryableStep("INIT", "建立端侧链路") {
            sdk.initialize(
                agentRuntimeIp = config.serverIp,
                agentRuntimePort = config.runtimePort,
                localTcpPort = config.localTcpPort,
                localUdpPort = config.localUdpPort,
                masqueServerUrl = config.masqueServerUrl,
                masqueAuthorization = config.masqueToken?.let { "Bearer $it" },
            )
        }
        onLog(
            LabLogLevel.SUCCESS,
            "INIT",
            "系统选择MASQUE出口=${initResult.masqueOuterSourceIp}，" +
                "Agent TUN=${initResult.agentTunCidr}，A2A=${initResult.agentTcpEndpoint}",
        )

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
                        put("version", "0.2.0")
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
        runCatching { networkListener?.close() }
        runCatching { groupListener?.close() }
        manualMessageSession = null
        onManualMessageSession(null)
        retrySignal.close()
    }

    suspend fun sendManualMessage(content: String): MessageReceipt = sendMutex.withLock {
        val normalized = content.trim()
        require(normalized.isNotEmpty()) { "消息内容不能为空" }
        val session = checkNotNull(manualMessageSession) { "群组尚未就绪，不能发送消息" }
        onLog(
            LabLogLevel.INFO,
            "A2A SEND",
            "to=${session.targetAgentName}(${session.targetAgentId})，group_id=${session.groupId}，" +
                "content=${normalized.take(300)}",
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
                onStatus(RunnerStatus("已收到 A2A 消息", "链路验证成功，SDK 继续运行"))
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

        retryableStep("GROUP CONFIG", "等待双方群组配置") {
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

    private suspend fun waitUntilCancelled(): Nothing {
        while (true) {
            currentCoroutineContext().ensureActive()
            delay(60_000)
        }
    }

    private suspend fun <T> retryableStep(
        stage: String,
        title: String,
        call: suspend () -> T,
    ): T {
        var attempt = 1
        while (true) {
            currentCoroutineContext().ensureActive()
            onStatus(RunnerStatus(title, "第 $attempt 次调用中"))
            onLog(LabLogLevel.INFO, stage, "调用开始（attempt=$attempt）")
            try {
                return call().also {
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

    private fun compact(value: JsonObject): String = value.toString().take(800)
}
