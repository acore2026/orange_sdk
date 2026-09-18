package com.rayneo.agent.example

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.net.VpnService
import android.os.Bundle
import android.os.IBinder
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.Toast
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
import org.webrtc.RendererCommon
import org.webrtc.EglBase
import org.webrtc.SurfaceViewRenderer
import org.webrtc.VideoSink
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.CopyOnWriteArraySet

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
            discoveryAsrUrl = intent.getStringExtra("discovery_asr_url"),
        )
    }
    private val logLines = ArrayDeque<String>()
    private val diagnosticLogLines = ArrayDeque<String>()
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
    private var computeAvailable = false
    private var computeStarting = false
    private val voiceRecorder by lazy { VoiceAudioRecorder(this) }
    private var voiceRecordingMode: VoiceMode? = null
    private var pendingVoicePermissionMode: VoiceMode? = null
    private var voiceActionRunning = false
    private var sdkFeatureState: SdkFeatureState? = null
    private val videoRenderers = CopyOnWriteArraySet<SurfaceViewRenderer>()
    @Volatile
    private var videoEglBase: EglBase? = null
    private var videoPreviewHasFrame = false
    private var videoPreviewResolution = ""

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

    private val microphonePermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            val mode = pendingVoicePermissionMode
            pendingVoicePermissionMode = null
            appendLog(
                if (granted) LabLogLevel.SUCCESS else LabLogLevel.ERROR,
                "MICROPHONE",
                if (granted) "麦克风权限已授予" else "麦克风权限被拒绝，无法录音",
            )
            if (granted && mode != null) startVoiceRecording(mode)
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
        initializeVideoPreviews()
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
            asrAction.setOnClickListener { toggleVoiceRecording(VoiceMode.TRANSCRIBE) }
            voiceControlAction.setOnClickListener {
                toggleVoiceRecording(VoiceMode.RUNTIME_INTENT)
            }
            resetAction.setOnClickListener { requestAgentReset() }
            dumpAction.setOnClickListener { dumpLogs() }
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
                    asrAction,
                    eventHandler = { action ->
                        if (action is TempleAction.Click) {
                            toggleVoiceRecording(VoiceMode.TRANSCRIBE)
                        }
                    },
                    focusChangeHandler = { focused -> updateFocus(ActionTarget.ASR, focused) },
                ),
                FocusInfo(
                    voiceControlAction,
                    eventHandler = { action ->
                        if (action is TempleAction.Click) {
                            toggleVoiceRecording(VoiceMode.RUNTIME_INTENT)
                        }
                    },
                    focusChangeHandler = { focused ->
                        updateFocus(ActionTarget.VOICE_CONTROL, focused)
                    },
                ),
                FocusInfo(
                    resetAction,
                    eventHandler = { action ->
                        if (action is TempleAction.Click) requestAgentReset()
                    },
                    focusChangeHandler = { focused -> updateFocus(ActionTarget.RESET, focused) },
                ),
                FocusInfo(
                    dumpAction,
                    eventHandler = { action ->
                        if (action is TempleAction.Click) dumpLogs()
                    },
                    focusChangeHandler = { focused -> updateFocus(ActionTarget.DUMP, focused) },
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
                ActionTarget.ASR -> asrAction
                ActionTarget.VOICE_CONTROL -> voiceControlAction
                ActionTarget.RESET -> resetAction
                ActionTarget.DUMP -> dumpAction
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
                val sdkValue = AgentSdk.create(service)
                val flow = AgentTestRunner(
                    sdk = sdkValue,
                    config = config,
                    onLog = ::appendLog,
                    onStatus = ::setRunnerStatus,
                    onManualMessageSession = ::setManualMessageSession,
                    onResetAvailability = ::setResetAvailable,
                    onComputeActionAvailability = ::setComputeActionAvailable,
                    processedVideoRenderSinks = ::processedVideoRenderSinks,
                    onProcessedVideoStatus = ::setProcessedVideoStatus,
                    onFeatureStateChanged = ::setSdkFeatureState,
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
                computeAvailable -> setPrimaryAction(PrimaryMode.COMPUTE, "申请算力会话")
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
                if (computeAvailable) {
                    setPrimaryAction(PrimaryMode.COMPUTE, "申请算力会话")
                } else {
                    setPrimaryAction(PrimaryMode.SEND, "发送测试消息")
                }
            }
        }
    }

    private fun setComputeActionAvailable(available: Boolean) {
        runOnUiThread {
            computeAvailable = available
            if (available && !computeStarting) {
                setStatus("群组已就绪 · 可申请算力", "单击后由 SDK 等待 consumer C-02 并连接处理流")
                setPrimaryAction(PrimaryMode.COMPUTE, "申请算力会话")
            }
        }
    }

    private fun setSdkFeatureState(state: SdkFeatureState) {
        runOnUiThread {
            sdkFeatureState = state
            refreshVoiceActions()
        }
    }

    private fun refreshVoiceActions() {
        val directReady = runner != null
        val controlReady = sdkFeatureState?.processedVideoState != null
        val busy = voiceActionRunning || voiceRecordingMode != null
        mBindingPair.updateView {
            asrAction.isEnabled = directReady && !busy
            voiceControlAction.isEnabled = controlReady && !busy
            asrAction.alpha = if (asrAction.isEnabled) 1f else 0.45f
            voiceControlAction.alpha = if (voiceControlAction.isEnabled) 1f else 0.45f
            asrAction.text = "语音转文字"
            voiceControlAction.text = "运行期意图"
            when (voiceRecordingMode) {
                VoiceMode.TRANSCRIBE -> {
                    asrAction.isEnabled = true
                    asrAction.alpha = 1f
                    asrAction.text = "停止并转写"
                }
                VoiceMode.RUNTIME_INTENT -> {
                    voiceControlAction.isEnabled = true
                    voiceControlAction.alpha = 1f
                    voiceControlAction.text = "停止并识别"
                }
                null -> Unit
            }
            voiceResult.text = when {
                voiceRecordingMode != null -> intentDisplay() + "\n最近转写结果：正在录音…"
                voiceActionRunning -> intentDisplay() + "\n最近转写结果：正在识别…"
                else -> intentDisplay() + "\n最近转写结果：" +
                    (sdkFeatureState?.lastTranscription?.ifBlank { "<未识别到文字>" } ?: "<暂无>")
            }
        }
    }

    private fun intentDisplay(): String = sdkFeatureState?.let { state ->
        state.recognizedIntent?.let {
            "启动意图：$it · area=${state.recognizedArea ?: "<none>"} · " +
                "skill=${state.discoverySkill ?: "<none>"}"
        }
    } ?: "启动意图：<等待识别>"

    private fun toggleVoiceRecording(mode: VoiceMode) {
        if (voiceRecordingMode == mode && voiceRecorder.isRecording) {
            stopAndSubmitVoice(mode)
            return
        }
        if (voiceRecorder.isRecording || voiceActionRunning || runner == null) return
        if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) !=
            android.content.pm.PackageManager.PERMISSION_GRANTED
        ) {
            pendingVoicePermissionMode = mode
            appendLog(LabLogLevel.INFO, "MICROPHONE", "等待麦克风权限；授权后自动开始录音")
            microphonePermissionLauncher.launch(android.Manifest.permission.RECORD_AUDIO)
            return
        }
        startVoiceRecording(mode)
    }

    private fun startVoiceRecording(mode: VoiceMode) {
        try {
            voiceRecorder.start()
            voiceRecordingMode = mode
            appendLog(
                LabLogLevel.INFO,
                "VOICE RECORD",
                if (mode == VoiceMode.TRANSCRIBE) "录音开始；再次单击上传 ASR 9004"
                else "录音开始；再次单击识别运行期动作意图（不会直接控制设备）",
            )
            setStatus("正在录音", "再次单击当前语音按钮停止并提交")
            refreshVoiceActions()
        } catch (error: Exception) {
            val detail = error.message ?: error::class.java.simpleName
            appendLog(LabLogLevel.ERROR, "VOICE RECORD", "录音启动失败：$detail")
            setStatus("录音启动失败", detail)
        }
    }

    private fun stopAndSubmitVoice(mode: VoiceMode) {
        val activeRunner = runner ?: return
        val recording = try {
            voiceRecorder.stop()
        } catch (error: Exception) {
            voiceRecordingMode = null
            refreshVoiceActions()
            val detail = error.message ?: error::class.java.simpleName
            appendLog(LabLogLevel.ERROR, "VOICE RECORD", "录音结束失败：$detail")
            setStatus("录音失败", detail)
            return
        }
        voiceRecordingMode = null
        voiceActionRunning = true
        refreshVoiceActions()
        setStatus("正在上传语音", if (mode == VoiceMode.TRANSCRIBE) "ASR :9004 转写中" else "Sandbox 语音动作处理中")
        lifecycleScope.launch {
            try {
                val audio = withContext(Dispatchers.IO) { recording.file.readBytes() }
                val detail = when (mode) {
                    VoiceMode.TRANSCRIBE -> activeRunner.transcribeAudio(
                        audio,
                        recording.fileName,
                        recording.contentType,
                    )
                    VoiceMode.RUNTIME_INTENT -> activeRunner.recognizeRuntimeAudioIntent(
                        audio,
                        recording.fileName,
                        recording.contentType,
                    )
                }
                setStatus(
                    if (mode == VoiceMode.TRANSCRIBE) "任务语音识别成功" else "运行期意图识别成功",
                    detail.take(300),
                )
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                val detail = error.message ?: error::class.java.simpleName
                appendLog(LabLogLevel.ERROR, "VOICE", "语音接口失败：$detail")
                setStatus("语音接口失败", detail)
            } finally {
                recording.file.delete()
                voiceActionRunning = false
                refreshVoiceActions()
            }
        }
    }

    private fun initializeVideoPreviews() {
        val events = object : RendererCommon.RendererEvents {
            override fun onFirstFrameRendered() {
                runOnUiThread {
                    videoPreviewHasFrame = true
                    renderProcessedVideoLiveStatus()
                    appendLog(
                        LabLogLevel.SUCCESS,
                        "VIDEO DISPLAY",
                        "SurfaceViewRenderer 已绘制处理流首帧",
                    )
                }
            }

            override fun onFrameResolutionChanged(width: Int, height: Int, rotation: Int) {
                runOnUiThread {
                    videoPreviewResolution = if (rotation % 180 == 0) {
                        "${width}x$height"
                    } else {
                        "${height}x$width"
                    }
                    if (videoPreviewHasFrame) renderProcessedVideoLiveStatus()
                }
            }
        }
        val eglBase = videoEglBase ?: EglBase.create().also { videoEglBase = it }
        mBindingPair.updateView {
            videoRenderer.setBackgroundColor(android.graphics.Color.BLACK)
            videoRenderer.init(eglBase.eglBaseContext, events)
            videoRenderer.setEnableHardwareScaler(false)
            videoRenderer.setMirror(false)
            videoRenderer.setScalingType(RendererCommon.ScalingType.SCALE_ASPECT_FIT)
            videoRenderers += videoRenderer
        }
    }

    private fun processedVideoRenderSinks(): List<VideoSink> = videoRenderers.toList()

    private fun setProcessedVideoStatus(title: String, detail: String) {
        runOnUiThread {
            videoPreviewHasFrame = false
            mBindingPair.updateView {
                videoRenderer.clearImage()
                videoStatus.text = "$title\n$detail"
                videoStatus.setTextColor(android.graphics.Color.rgb(183, 203, 212))
                videoStatus.textSize = 14f
                videoStatus.gravity = Gravity.CENTER
                videoStatus.setBackgroundResource(R.drawable.rayneo_video_status)
                videoStatus.layoutParams = FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    Gravity.CENTER,
                )
            }
        }
    }

    private fun renderProcessedVideoLiveStatus() {
        mBindingPair.updateView {
            videoStatus.text =
                "● LIVE${videoPreviewResolution.takeIf(String::isNotBlank)?.let { "  $it" }.orEmpty()}"
            videoStatus.setTextColor(android.graphics.Color.rgb(94, 234, 212))
            videoStatus.textSize = 13f
            videoStatus.setBackgroundResource(R.drawable.rayneo_video_status_live)
            videoStatus.layoutParams = FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.TOP or Gravity.END,
            ).apply {
                topMargin = dp(8)
                marginEnd = dp(8)
            }
        }
    }

    private fun detachVideoPreviews(): EglBase? {
        videoRenderers.forEach { renderer -> runCatching { renderer.release() } }
        videoRenderers.clear()
        val eglBase = videoEglBase
        videoEglBase = null
        return eglBase
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

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
        voiceRecorder.cancel()
        voiceRecordingMode = null
        pendingVoicePermissionMode = null
        refreshVoiceActions()
        val activeSdk = sdk ?: return
        val activeRunner = runner
        val activeJob = runnerJob
        resetInProgress = true
        resetArmed = false
        setResetAvailable(false)
        setPrimaryAction(PrimaryMode.BUSY, "身份重置中…")
        setStatus("正在重置到状态1", "先关闭算力会话，再清除本地身份状态")
        lifecycleScope.launch {
            try {
                val result = withContext(Dispatchers.IO) {
                    if (activeRunner != null) {
                        activeRunner.stopComputingSession()
                        activeRunner.resetAgent()
                    } else {
                        activeSdk.resetAgent()
                    }
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
            PrimaryMode.COMPUTE -> startComputeVideo()
        }
    }

    private fun startComputeVideo() {
        val activeRunner = runner ?: return
        if (!computeAvailable || computeStarting) return
        computeStarting = true
        setPrimaryAction(PrimaryMode.BUSY, "正在建立视频链路…")
        setStatus("正在申请算力会话", "等待 C-01 响应、C-02 配置和 Sandbox WebRTC Answer")
        lifecycleScope.launch {
            try {
                activeRunner.startVideoOffload()
                computeAvailable = false
                setStatus("处理流已连接", "等待 Sandbox 返回第一帧")
                setPrimaryAction(PrimaryMode.SEND, "发送测试消息")
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                computeAvailable = true
                val detail = error.message ?: error::class.java.simpleName
                appendLog(LabLogLevel.ERROR, "COMPUTE VIDEO", "启动失败：$detail")
                setStatus("视频算力测试失败", detail)
                setPrimaryAction(PrimaryMode.COMPUTE, "重试算力会话")
            } finally {
                computeStarting = false
            }
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
        val time = SimpleDateFormat("HH:mm:ss.SSS", Locale.US).format(Date())
        val raw = "$time  ${level.name.padEnd(7)}  ${stage.padEnd(17)}  $message"
        Log.println(
            when (level) {
                LabLogLevel.INFO, LabLogLevel.SUCCESS -> Log.INFO
                LabLogLevel.WARNING -> Log.WARN
                LabLogLevel.ERROR -> Log.ERROR
            },
            APP_LOG_TAG,
            "$stage $message",
        )
        runOnUiThread {
            diagnosticLogLines.addLast(raw)
            while (diagnosticLogLines.size > MAX_DIAGNOSTIC_LOG_LINES) {
                diagnosticLogLines.removeFirst()
            }
            logLines.addLast("$time  ${level.name.first()}  $stage  $message")
            while (logLines.size > MAX_VISIBLE_LOG_LINES) logLines.removeFirst()
            val rendered = logLines.joinToString("\n")
            mBindingPair.updateView {
                logOutput.text = rendered
                logScroll.post { logScroll.fullScroll(View.FOCUS_DOWN) }
            }
        }
    }

    private fun dumpLogs() {
        appendLog(LabLogLevel.INFO, "DUMP", "开始采集端侧诊断日志")
        val appLogs = diagnosticLogLines.toList()
        val activeRunner = runner
        val activeSdk = sdk
        lifecycleScope.launch {
            try {
                val dump = DiagnosticLogExporter.createDump(
                    context = this@RayNeoMainActivity,
                    config = config,
                    appLogs = appLogs,
                    sdkSummary = activeRunner?.diagnosticSummary()
                        ?: "lifecycle_state=${activeSdk?.agentLifecycleState ?: "<sdk unavailable>"}\n" +
                            "agent_id=${activeSdk?.localProfile?.agentId ?: "<none>"}",
                    localTcpEndpoint = activeRunner?.diagnosticLocalTcpEndpoint(),
                    videoPreviewSummary = videoRenderers.firstOrNull()?.let {
                        "renderer=SurfaceViewRenderer first_frame_displayed=$videoPreviewHasFrame " +
                            "last_frame=$videoPreviewResolution"
                    } ?: "renderer=<unavailable>",
                )
                appendLog(
                    LabLogLevel.SUCCESS,
                    "DUMP",
                    "诊断日志已生成：${dump.displayLocation}",
                )
                DiagnosticLogExporter.share(this@RayNeoMainActivity, dump)
            } catch (error: Exception) {
                appendLog(
                    LabLogLevel.ERROR,
                    "DUMP",
                    "诊断日志生成失败：${error.message ?: error::class.java.simpleName}",
                )
                Toast.makeText(
                    this@RayNeoMainActivity,
                    "诊断日志生成失败",
                    Toast.LENGTH_LONG,
                ).show()
            }
        }
    }

    private fun stopAndFinish() {
        if (isFinishing || stopInProgress) return
        voiceRecorder.cancel()
        voiceRecordingMode = null
        stopInProgress = true
        setPrimaryAction(PrimaryMode.BUSY, "正在关闭…")
        setStatus("正在停止", "先关闭算力会话，再向核心网发送 Agent 去注册请求")
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
        voiceRecorder.cancel()
        voiceRecordingMode = null
        val activeJob = runnerJob
        val activeRunner = runner
        val activeSdk = sdk
        runnerJob = null
        activeJob?.cancelAndJoin()
        if (deregisterIdentity && activeSdk != null) {
            try {
                withContext(Dispatchers.IO) {
                    if (activeRunner != null) {
                        activeRunner.stopComputingSession()
                        activeRunner.deregisterAgentForStop()
                    } else {
                        deregisterIdentityForStop(activeSdk, ::appendLog)
                    }
                }
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                appendLog(
                    LabLogLevel.ERROR,
                    "APP STOP",
                    "算力会话关闭或身份注销失败：" +
                        "${error.message ?: error::class.java.simpleName}；Agent Profile 已保留",
                )
            }
        }
        activeRunner?.close()
        runner = null
        sdkFeatureState = null
        voiceActionRunning = false
        runOnUiThread { refreshVoiceActions() }
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
        voiceRecorder.cancel()
        val activeJob = runnerJob
        val activeSdk = sdk
        runnerJob = null
        sdk = null
        activeJob?.cancel()
        runner?.close()
        runner = null
        // Renderers must release their EGL surfaces on the UI thread, while the shared root EGL
        // context must outlive the adapter's decoder context. Release the root after SDK close.
        val activeEglBase = detachVideoPreviews()
        cleanupScope.launch {
            activeJob?.cancelAndJoin()
            runCatching { activeSdk?.close() }
            runCatching { activeEglBase?.release() }
            cleanupScope.cancel()
        }
        if (serviceBound) runCatching { unbindService(connection) }
        super.onDestroy()
    }

    private enum class PrimaryMode { BUSY, RETRY, SEND, COMPUTE }
    private enum class ActionTarget { PRIMARY, ASR, VOICE_CONTROL, RESET, DUMP, STOP }
    private enum class VoiceMode { TRANSCRIBE, RUNTIME_INTENT }

    private companion object {
        const val MAX_VISIBLE_LOG_LINES = 7
        const val MAX_DIAGNOSTIC_LOG_LINES = 2_000
        const val APP_LOG_TAG = "AgentLinkLab"
    }
}
