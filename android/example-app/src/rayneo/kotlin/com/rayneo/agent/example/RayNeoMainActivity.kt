package com.rayneo.agent.example

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.net.VpnService
import android.os.Bundle
import android.os.IBinder
import android.view.View
import androidx.activity.result.contract.ActivityResultContracts
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.ffalcon.mercury.android.sdk.focus.reqFocus
import com.ffalcon.mercury.android.sdk.touch.TempleAction
import com.ffalcon.mercury.android.sdk.ui.activity.BaseMirrorActivity
import com.ffalcon.mercury.android.sdk.ui.util.FixPosFocusTracker
import com.ffalcon.mercury.android.sdk.ui.util.FocusHolder
import com.ffalcon.mercury.android.sdk.ui.util.FocusInfo
import com.rayneo.agent.example.databinding.ActivityRayneoMainBinding
import com.rayneo.agent.sdk.AgentSdk
import com.rayneo.agent.sdk.model.AgentLifecycleState
import com.rayneo.agent.sdk.vpn.AgentVpnService
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * RayNeo X3 Pro launcher for the fixed Agent-A deployment.
 *
 * BaseMirrorActivity inflates [ActivityRayneoMainBinding] twice and places both copies on the
 * logical display. Every visual mutation is therefore applied with mBindingPair.updateView.
 * Business callbacks and touch handlers are bound only once to the left copy.
 */
class RayNeoMainActivity : BaseMirrorActivity<ActivityRayneoMainBinding>() {
    private val config by lazy {
        RayNeoX3ProDeployment.agentAConfig(
            masqueToken = intent.getStringExtra("masque_token"),
        )
    }
    private val logLines = ArrayDeque<String>()
    private val cleanupScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    @Volatile
    private var vpnService: AgentVpnService? = null
    @Volatile
    private var sdk: AgentSdk? = null
    @Volatile
    private var runner: AgentTestRunner? = null
    private var runnerJob: Job? = null
    private var serviceBound = false
    private var flowStarted = false
    private var messageSession: ManualMessageSession? = null
    private var primaryMode = PrimaryMode.BUSY
    private var sendSequence = 0
    private var focusTracker: FixPosFocusTracker? = null
    private var resetAvailable = false
    private var resetArmed = false
    private var resetInProgress = false
    private var stopInProgress = false

    private val vpnPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            if (result.resultCode == RESULT_OK) {
                appendLog(LabLogLevel.SUCCESS, "VPN", "系统 VPN 权限已授予")
                startSdkFlow()
            } else {
                flowStarted = false
                appendLog(LabLogLevel.ERROR, "VPN", "VPN 权限被拒绝或授权页已关闭")
                setStatus("需要 VPN 权限", "单击“启用 Agent 网络”后，在系统页面确认网络连接请求")
                setPrimaryAction(PrimaryMode.RETRY, "启用 Agent 网络")
            }
        }

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            vpnService = (binder as AgentVpnService.LocalBinder).service
            appendLog(LabLogLevel.SUCCESS, "VPN", "AgentVpnService 已绑定")
            requestVpnOrStart()
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            vpnService = null
            appendLog(LabLogLevel.WARNING, "VPN", "AgentVpnService 连接断开")
            setStatus("VPN 服务已断开", "单击“重新连接”恢复端侧链路")
            setPrimaryAction(PrimaryMode.RETRY, "重新连接")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        renderDeployment()
        installFocusAndTempleActions()
        appendLog(
            LabLogLevel.INFO,
            "BOOT",
            "固定角色=A，Runtime=${config.serverIp}:${config.runtimePort}，MASQUE/UDP=${config.masquePort}",
        )

        // Do not open Android's system VPN activity during launcher creation. On glasses, a
        // system activity can cover both the app and launcher while waiting for input. The first
        // connection is therefore an explicit temple click; subsequent launches continue without
        // a dialog after Android has retained the user's consent.
        setStatus(
            "Agent 网络尚未启用",
            "单击“启用 Agent 网络”；首次使用需确认系统网络连接请求",
        )
        setPrimaryAction(PrimaryMode.RETRY, "启用 Agent 网络")
        appendLog(LabLogLevel.INFO, "APP", "等待用户显式启用 Agent 网络")
    }

    private fun renderDeployment() {
        mBindingPair.updateView {
            deploymentLabel.text = "RAYNEO X3 PRO  ·  AGENT A"
            statusTitle.text = "正在准备端侧链路"
            statusDetail.text =
                "Runtime ${config.serverIp}:${config.runtimePort}  ·  MASQUE/UDP ${config.masquePort}"
            primaryAction.text = "自动启动中…"
        }
    }

    private fun installFocusAndTempleActions() {
        val focusHolder = FocusHolder(true)
        mBindingPair.setLeft {
            primaryAction.setOnClickListener { handlePrimaryAction() }
            resetAction.setOnClickListener { requestAgentReset() }
            stopAction.setOnClickListener { stopAndFinish() }
            focusHolder.addFocusTarget(
                FocusInfo(
                    primaryAction,
                    eventHandler = { action ->
                        if (action is TempleAction.Click) handlePrimaryAction()
                    },
                    focusChangeHandler = { focused -> updateFocus(ActionTarget.PRIMARY, focused) },
                ),
                FocusInfo(
                    resetAction,
                    eventHandler = { action ->
                        if (action is TempleAction.Click) requestAgentReset()
                    },
                    focusChangeHandler = { focused -> updateFocus(ActionTarget.RESET, focused) },
                ),
                FocusInfo(
                    stopAction,
                    eventHandler = { action ->
                        if (action is TempleAction.Click) stopAndFinish()
                    },
                    focusChangeHandler = { focused -> updateFocus(ActionTarget.STOP, focused) },
                ),
            )
            focusHolder.currentFocus(primaryAction)
        }
        focusTracker = FixPosFocusTracker(focusHolder).apply {
            focusObj.reqFocus()
        }

        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.RESUMED) {
                templeActionViewModel.state.collect { action ->
                    if (action is TempleAction.DoubleClick) {
                        stopAndFinish()
                    } else {
                        focusTracker?.handleFocusTargetEvent(action)
                    }
                }
            }
        }
    }

    private fun updateFocus(action: ActionTarget, focused: Boolean) {
        mBindingPair.updateView {
            val target = when (action) {
                ActionTarget.PRIMARY -> primaryAction
                ActionTarget.RESET -> resetAction
                ActionTarget.STOP -> stopAction
            }
            target.setBackgroundResource(
                when {
                    focused -> R.drawable.rayneo_action_focused
                    action == ActionTarget.PRIMARY -> R.drawable.rayneo_action_primary
                    action == ActionTarget.RESET && resetArmed -> R.drawable.rayneo_action_warning
                    else -> R.drawable.rayneo_action_secondary
                },
            )
        }
    }

    private fun beginConnection() {
        if (flowStarted || runnerJob?.isActive == true) return
        flowStarted = true
        setPrimaryAction(PrimaryMode.BUSY, "连接中…")
        setStatus("正在连接 AgentRuntime", "随后自动建立 TUN、MASQUE、身份、发现与建组流程")
        appendLog(LabLogLevel.INFO, "APP", "自动流程启动；准备申请 Android VPN 权限")

        if (serviceBound) {
            requestVpnOrStart()
            return
        }
        serviceBound = bindService(
            Intent(this, AgentVpnService::class.java),
            connection,
            Context.BIND_AUTO_CREATE,
        )
        if (!serviceBound) {
            flowStarted = false
            appendLog(LabLogLevel.ERROR, "VPN", "无法绑定 AgentVpnService")
            setStatus("VPN 服务启动失败", "单击“重新连接”重试")
            setPrimaryAction(PrimaryMode.RETRY, "重新连接")
        }
    }

    private fun requestVpnOrStart() {
        val permissionIntent = VpnService.prepare(this)
        if (permissionIntent == null) {
            startSdkFlow()
        } else {
            appendLog(LabLogLevel.INFO, "VPN", "用户已触发；等待系统 VPN 授权")
            vpnPermissionLauncher.launch(permissionIntent)
        }
    }

    private fun startSdkFlow() {
        if (runnerJob?.isActive == true) return
        val service = vpnService ?: run {
            flowStarted = false
            setStatus("VPN 服务尚未连接", "单击“重新连接”重试")
            setPrimaryAction(PrimaryMode.RETRY, "重新连接")
            return
        }
        // AgentSdk.create may touch the Android keystore and initialize the native library;
        // initialize then performs a synchronous JNI CONNECT-IP handshake. Keep the complete
        // sequence off the main thread so a slow or unreachable MASQUE endpoint cannot freeze
        // the glasses UI.
        runnerJob = lifecycleScope.launch(Dispatchers.IO) {
            try {
                val mediaAdapter = AndroidWebRtcMediaOffloadAdapter(service) { event ->
                    appendLog(LabLogLevel.INFO, "WEBRTC", event)
                }
                val sdkValue = AgentSdk.create(service, mediaOffloadAdapter = mediaAdapter)
                val flow = AgentTestRunner(
                    sdk = sdkValue,
                    config = config,
                    onLog = ::appendLog,
                    onStatus = ::setRunnerStatus,
                    onManualMessageSession = ::setManualMessageSession,
                    onResetAvailability = ::setResetAvailable,
                )
                sdk = sdkValue
                runner = flow
                flow.run()
            } catch (_: CancellationException) {
                appendLog(LabLogLevel.INFO, "APP", "Agent A 流程已停止")
            } catch (error: Exception) {
                flowStarted = false
                val detail = error.message ?: error::class.java.simpleName
                appendLog(LabLogLevel.ERROR, "APP", "流程异常：$detail；SDK 未自动关闭")
                setRunnerStatus(RunnerStatus("流程异常", detail, canRetry = true))
            }
        }
    }

    private fun setRunnerStatus(status: RunnerStatus) {
        runOnUiThread {
            setStatus(status.title, status.detail)
            when {
                messageSession != null -> setPrimaryAction(PrimaryMode.SEND, "发送测试消息")
                status.canRetry -> setPrimaryAction(PrimaryMode.RETRY, "重试当前步骤")
                else -> setPrimaryAction(PrimaryMode.BUSY, "流程执行中…")
            }
        }
    }

    private fun setManualMessageSession(session: ManualMessageSession?) {
        runOnUiThread {
            messageSession = session
            if (session == null) {
                if (primaryMode == PrimaryMode.SEND) {
                    setPrimaryAction(PrimaryMode.BUSY, "等待群组配置…")
                }
            } else {
                setStatus("群组已就绪 · 可发送", "目标 ${session.targetAgentName} · 单击发送预置测试消息")
                setPrimaryAction(PrimaryMode.SEND, "发送测试消息")
            }
        }
    }

    private fun setResetAvailable(available: Boolean) {
        runOnUiThread {
            resetAvailable = available
            if (!available) resetArmed = false
            mBindingPair.updateView {
                resetAction.isEnabled = available && !resetInProgress
                resetAction.alpha = if (resetAction.isEnabled) 1f else 0.45f
                resetAction.text = when {
                    resetInProgress -> "正在重置…"
                    resetArmed -> "再次单击确认"
                    else -> "重置到状态1"
                }
                resetAction.setBackgroundResource(
                    if (resetArmed) {
                        R.drawable.rayneo_action_warning
                    } else {
                        R.drawable.rayneo_action_secondary
                    },
                )
            }
        }
    }

    private fun requestAgentReset() {
        if (!resetAvailable || resetInProgress) return
        if (!resetArmed) {
            resetArmed = true
            setResetAvailable(true)
            setStatus(
                "确认重置 Agent 身份",
                "再次单击 Reset：仅清除本地 Profile / Agent Card，不修改网侧身份",
            )
            appendLog(LabLogLevel.WARNING, "RESET", "等待二次确认；当前身份尚未改变")
            return
        }
        performAgentReset()
    }

    private fun performAgentReset() {
        val activeSdk = sdk ?: return
        val activeRunner = runner
        val activeJob = runnerJob
        resetInProgress = true
        resetArmed = false
        setResetAvailable(false)
        setPrimaryAction(PrimaryMode.BUSY, "身份重置中…")
        setStatus("正在重置到状态1", "停止自动流程并清除本地身份状态")
        lifecycleScope.launch {
            try {
                val result = withContext(Dispatchers.IO) {
                    activeRunner?.resetAgent() ?: activeSdk.resetAgent()
                }
                runnerJob = null
                activeJob?.cancelAndJoin()
                activeRunner?.close()
                runner = null
                messageSession = null
                setManualMessageSession(null)
                check(result.success) { result.message.ifBlank { "本地状态重置失败" } }
                check(activeSdk.agentLifecycleState == AgentLifecycleState.NO_IDENTITY) {
                    "Reset 成功后 SDK 未进入 NO_IDENTITY"
                }
                appendLog(
                    LabLogLevel.SUCCESS,
                    "RESET",
                    "本地身份状态已清除；Agent 已回到状态1；网侧身份未修改",
                )
                closeResources()
                resetInProgress = false
                mBindingPair.updateView {
                    resetAction.text = "已重置为状态1"
                    resetAction.isEnabled = false
                    resetAction.alpha = 0.55f
                }
                setStatus("已回到状态1", "当前未申请数字身份；可重新启用 Agent 网络")
                setPrimaryAction(PrimaryMode.RETRY, "重新启用 Agent 网络")
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                runnerJob = null
                activeJob?.cancelAndJoin()
                activeRunner?.close()
                runner = null
                messageSession = null
                setManualMessageSession(null)
                resetInProgress = false
                flowStarted = true
                val detail = error.message ?: error::class.java.simpleName
                appendLog(LabLogLevel.ERROR, "RESET", "重置失败：$detail；原身份状态未清除")
                setStatus("重置失败", "$detail；可再次尝试 Reset，或停止退出")
                setPrimaryAction(PrimaryMode.BUSY, "重置失败")
                setResetAvailable(true)
            }
        }
    }

    private fun handlePrimaryAction() {
        when (primaryMode) {
            PrimaryMode.BUSY -> Unit
            PrimaryMode.RETRY -> {
                if (runner != null) {
                    setPrimaryAction(PrimaryMode.BUSY, "重试中…")
                    runner?.retryCurrentStep()
                } else {
                    flowStarted = false
                    beginConnection()
                }
            }
            PrimaryMode.SEND -> sendTestMessage()
        }
    }

    private fun sendTestMessage() {
        val activeRunner = runner ?: return
        val session = messageSession ?: return
        if (primaryMode == PrimaryMode.BUSY) return
        sendSequence += 1
        val content = "${config.message} #$sendSequence"
        setPrimaryAction(PrimaryMode.BUSY, "发送中…")
        lifecycleScope.launch {
            try {
                val receipt = activeRunner.sendManualMessage(content)
                setStatus("消息发送成功", "→ ${session.targetAgentName} · ${receipt.messageId.take(8)}")
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                setStatus("消息发送失败", "${error.message ?: error::class.java.simpleName}；可再次发送")
            } finally {
                if (messageSession != null) {
                    setPrimaryAction(PrimaryMode.SEND, "再次发送测试消息")
                }
            }
        }
    }

    private fun setStatus(title: String, detail: String) {
        mBindingPair.updateView {
            statusTitle.text = title
            statusDetail.text = detail
        }
    }

    private fun setPrimaryAction(mode: PrimaryMode, label: String) {
        primaryMode = mode
        mBindingPair.updateView {
            primaryAction.text = label
            primaryAction.alpha = if (mode == PrimaryMode.BUSY) 0.55f else 1f
        }
    }

    private fun appendLog(level: LabLogLevel, stage: String, message: String) {
        runOnUiThread {
            val time = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())
            logLines.addLast("$time  ${level.name.first()}  $stage  $message")
            while (logLines.size > MAX_VISIBLE_LOG_LINES) logLines.removeFirst()
            val rendered = logLines.joinToString("\n")
            mBindingPair.updateView {
                logOutput.text = rendered
                logScroll.post { logScroll.fullScroll(View.FOCUS_DOWN) }
            }
        }
    }

    private fun stopAndFinish() {
        if (isFinishing || stopInProgress) return
        stopInProgress = true
        setPrimaryAction(PrimaryMode.BUSY, "正在关闭…")
        setStatus("正在停止", "先向核心网发送 Agent 去注册请求")
        mBindingPair.updateView {
            stopAction.isEnabled = false
            stopAction.alpha = 0.45f
        }
        lifecycleScope.launch {
            closeResources(deregisterIdentity = true)
            finish()
        }
    }

    private suspend fun closeResources(deregisterIdentity: Boolean = false) {
        val activeJob = runnerJob
        val activeRunner = runner
        val activeSdk = sdk
        runnerJob = null
        activeJob?.cancelAndJoin()
        if (deregisterIdentity && activeSdk != null) {
            try {
                withContext(Dispatchers.IO) {
                    if (activeRunner != null) {
                        activeRunner.deregisterAgentForStop()
                    } else {
                        deregisterIdentityForStop(activeSdk, ::appendLog)
                    }
                }
            } catch (error: CancellationException) {
                throw error
            } catch (_: Exception) {
                // The attempt and failure are logged by deregisterIdentityForStop.
                // A user-requested stop still releases all local resources.
            }
        }
        activeRunner?.close()
        runner = null
        messageSession = null
        withContext(Dispatchers.IO) { runCatching { activeSdk?.close() } }
        sdk = null
        if (serviceBound) {
            runCatching { unbindService(connection) }
            serviceBound = false
            vpnService = null
        }
        flowStarted = false
        resetAvailable = false
        resetArmed = false
    }

    override fun onDestroy() {
        val activeJob = runnerJob
        val activeSdk = sdk
        runnerJob = null
        sdk = null
        activeJob?.cancel()
        runner?.close()
        runner = null
        cleanupScope.launch {
            activeJob?.cancelAndJoin()
            runCatching { activeSdk?.close() }
            cleanupScope.cancel()
        }
        if (serviceBound) runCatching { unbindService(connection) }
        super.onDestroy()
    }

    private enum class PrimaryMode { BUSY, RETRY, SEND }
    private enum class ActionTarget { PRIMARY, RESET, STOP }

    private companion object {
        const val MAX_VISIBLE_LOG_LINES = 7
    }
}
