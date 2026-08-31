package com.rayneo.agent.example

import com.rayneo.agent.sdk.AgentSdk
import com.rayneo.agent.sdk.model.AgentProfile
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

class AgentTestRunner(
    private val sdk: AgentSdk,
    private val config: TestConfig,
    private val onLog: (LabLogLevel, String, String) -> Unit,
    private val onStatus: (RunnerStatus) -> Unit,
) {
    private val retrySignal = Channel<Unit>(Channel.CONFLATED)
    private var networkListener: AutoCloseable? = null
    private var groupListener: AutoCloseable? = null

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

        val profile = retryableStep("H-ID", "申请 Agent 数字身份") {
            sdk.applyIdentity(
                owner = config.owner,
                name = config.agentName,
                description = "Android ${config.role.name} MASQUE integration test",
                metadata = buildJsonObject {
                    put("region", "CN")
                    put("os", "Android")
                    put("version", "0.1.0")
                },
            )
        }
        onLog(LabLogLevel.SUCCESS, "H-ID", "agent_id=${profile.agentId}")

        val networkAbility = retryableStep("H-NETWORK-ABILITY", "获取运营商网络能力") {
            sdk.getNetworkAbility(profile.agentId)
        }
        onLog(
            LabLogLevel.SUCCESS,
            "H-NETWORK-ABILITY",
            "network_abilities=${networkAbility.abilities.ifEmpty { listOf("<未声明>") }}",
        )

        retryableStep("H-PROFILE", "发布 Agent Card") {
            sdk.registerCapabilities(
                agentId = profile.agentId,
                priority = 1,
                credentials = listOf(networkAbility.abilityVc),
                capabilities = if (config.role == TestRole.B) {
                    listOf(config.capability)
                } else {
                    emptyList()
                },
                agentName = profile.agentName,
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

        if (config.role == TestRole.A) {
            runAgentA(profile)
        } else {
            runAgentB()
        }
    }

    fun close() {
        runCatching { networkListener?.close() }
        runCatching { groupListener?.close() }
        retrySignal.close()
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
                        onLog(
                            LabLogLevel.SUCCESS,
                            "DOWNLINK",
                            "群组配置已缓存，group_id=${payload["group_id"]?.jsonPrimitive?.content}",
                        )
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

        val receipt = retryableStep("H-A2A", "向 Agent B 发送测试消息") {
            sdk.sendMessage(
                groupId = group.groupId,
                targetAgentId = discovered.agentId,
                jsonMessage = buildJsonObject {
                    put("type", "text")
                    put("content", config.message)
                },
                messageType = "text",
                taskId = "android-ab-test",
                timeoutSeconds = 10.0,
            ).also { check(it.delivered) { "Agent B 未返回 status=OK" } }
        }
        onLog(LabLogLevel.SUCCESS, "H-A2A", "message_id=${receipt.messageId}，投递成功")
        onStatus(RunnerStatus("A 端流程通过", "消息已送达 B；SDK 保持运行"))
        waitUntilCancelled()
    }

    private suspend fun runAgentB() {
        onStatus(RunnerStatus("Agent B 已就绪", "请启动 Agent A；邀请将自动接受"))
        onLog(LabLogLevel.INFO, "READY", "等待建组邀请、群组配置和 A2A 消息")
        waitUntilCancelled()
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
