package com.rayneo.agent.example

import android.annotation.SuppressLint
import android.Manifest
import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.net.VpnService
import android.os.Bundle
import android.os.IBinder
import android.text.InputType
import android.text.SpannableString
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Space
import android.widget.TextView
import android.widget.Toast
import com.rayneo.agent.sdk.AgentSdk
import com.rayneo.agent.sdk.model.AgentLifecycleState
import com.rayneo.agent.sdk.vpn.AgentVpnService
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import org.webrtc.RendererCommon
import org.webrtc.EglBase
import org.webrtc.VideoSink
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@SuppressLint("SetTextI18n")
class MainActivity : Activity() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val fields = mutableMapOf<String, EditText>()
    private val logLines = ArrayDeque<CharSequence>()
    private val diagnosticLogLines = ArrayDeque<String>()

    private var selectedRole = TestRole.A
    private var activeConfig: TestConfig? = null
    private var vpnService: AgentVpnService? = null
    private var sdk: AgentSdk? = null
    private var runner: AgentTestRunner? = null
    private var runnerJob: Job? = null
    private var serviceBound = false
    private var aOnlyContainer: View? = null
    private var roleAButton: TextView? = null
    private var roleBButton: TextView? = null
    private var startButton: TextView? = null
    private var logStatusTitle: TextView? = null
    private var logStatusDetail: TextView? = null
    private var retryButton: TextView? = null
    private var resetButton: TextView? = null
    private var stopButton: TextView? = null
    private var logOutput: TextView? = null
    private var logScroll: ScrollView? = null
    private var controlScroll: ScrollView? = null
    private var manualMessagePanel: View? = null
    private var manualRouteLabel: TextView? = null
    private var manualMessageInput: EditText? = null
    private var manualSendButton: TextView? = null
    private var manualMessageSession: ManualMessageSession? = null
    private var manualMessageSending = false
    private var computeVideoPanel: View? = null
    private var computeVideoButton: TextView? = null
    private var computeVideoAvailable = false
    private var computeVideoStarting = false
    private var pendingVideoStart = false
    private var sdkFeaturePanel: View? = null
    private var sdkFeatureStatus: TextView? = null
    private var groupSnapshotButton: TextView? = null
    private var capabilityAddButton: TextView? = null
    private var capabilityRemoveButton: TextView? = null
    private var computeQueryButton: TextView? = null
    private var computeCancelButton: TextView? = null
    private var computeReleaseButton: TextView? = null
    private var videoUploadToggleButton: TextView? = null
    private var recognitionUpdateButton: TextView? = null
    private var recognitionGetButton: TextView? = null
    private var asrTranscribeButton: TextView? = null
    private var voiceControlButton: TextView? = null
    private var voiceTranscriptionResult: TextView? = null
    private var capabilitySkillInput: EditText? = null
    private var capabilityVcInput: EditText? = null
    private var recognitionTargetInput: EditText? = null
    private var sdkFeatureState: SdkFeatureState? = null
    private var sdkFeatureActionRunning = false
    private val voiceRecorder by lazy { VoiceAudioRecorder(this) }
    private var voiceRecordingMode: VoiceMode? = null
    private var pendingVoicePermissionMode: VoiceMode? = null
    private var videoPreviewRenderer: VideoSink? = null
    private var videoPreviewEglBase: EglBase? = null
    private var videoPreviewStatus: TextView? = null
    private var videoPreviewHasFrame = false
    private var videoPreviewResolution = ""
    private var videoPreviewStatistics = "正在统计…"
    private var videoStatisticsJob: Job? = null
    private var lastSdkDiagnosticSummary: String? = null
    private var lastDiagnosticLocalTcpEndpoint: String? = null
    private var lastVideoPreviewSummary: String? = null
    private var resetAvailable = false
    private var resetArmed = false
    private var resetInProgress = false

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            vpnService = (binder as AgentVpnService.LocalBinder).service
            appendLog(LabLogLevel.INFO, "VPN", "AgentVpnService 已绑定")
            requestVpnOrStart()
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            vpnService = null
            appendLog(LabLogLevel.WARNING, "VPN", "AgentVpnService 连接断开")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.statusBarColor = Palette.INK
        window.navigationBarColor = Palette.INK
        showConfigScreen()
    }

    private fun showConfigScreen() {
        voiceRecorder.cancel()
        voiceRecordingMode = null
        pendingVoicePermissionMode = null
        releaseProcessedVideoPreview()
        fields.clear()
        manualMessagePanel = null
        manualRouteLabel = null
        manualMessageInput = null
        manualSendButton = null
        manualMessageSession = null
        computeVideoPanel = null
        computeVideoButton = null
        computeVideoAvailable = false
        computeVideoStarting = false
        pendingVideoStart = false
        sdkFeaturePanel = null
        sdkFeatureStatus = null
        groupSnapshotButton = null
        capabilityAddButton = null
        capabilityRemoveButton = null
        computeQueryButton = null
        computeCancelButton = null
        computeReleaseButton = null
        videoUploadToggleButton = null
        recognitionUpdateButton = null
        recognitionGetButton = null
        asrTranscribeButton = null
        voiceControlButton = null
        voiceTranscriptionResult = null
        capabilitySkillInput = null
        capabilityVcInput = null
        recognitionTargetInput = null
        sdkFeatureState = null
        sdkFeatureActionRunning = false
        controlScroll = null
        logScroll = null
        resetButton = null
        resetAvailable = false
        resetArmed = false
        resetInProgress = false
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Palette.CANVAS)
        }
        val scroll = ScrollView(this).apply {
            isFillViewport = true
            addView(LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(22), dp(26), dp(22), dp(34))

                addView(eyebrow("AGENT LINK LAB  /  ANDROID"))
                addView(title("A/B 端到端联调"))
                addView(body("填写端侧与服务器参数。应用完成身份、能力、发现和建组后，可验证双向消息及正式异步算力视频流程。"))
                addView(roleSelector())

                addView(section("01  测试角色"))
                addView(body("A 按能力发现 B 并发起建组；B 自动接受邀请。群组配置生效后双方均可手动发送。"))

                addView(section("02  服务器与隧道"))
                addView(field("server_ip", "服务器 IP / 域名", "由部署方提供"))
                addView(twoColumns(
                    field("runtime_port", "Runtime HTTP 端口", "8088", numeric = true),
                    field("masque_port", "MASQUE QUIC 端口", "8443", numeric = true),
                ))
                addView(field("masque_path", "CONNECT-IP 路径", "/.well-known/masque/ip"))
                addView(field("masque_token", "MASQUE Token（可选，不保存）", "Bearer token", password = true))
                addView(body("算力 Sandbox 地址、端口和媒体路径由网侧 C-02 下发，并由 SDK 内部使用。"))

                addView(section("03  Agent 服务端口"))
                addView(body("MASQUE 外层源地址由 Android 系统路由自动选择，无需填写本机 Wi-Fi 或 VLAN 地址。"))
                addView(twoColumns(
                    field("tcp_port", "A2A TCP 端口", "4001", numeric = true),
                    field("udp_port", "A2A UDP 端口", "28443", numeric = true),
                ))

                addView(section("04  Agent Profile"))
                addView(field("owner", "Owner", "测试终端归属标识"))
                addView(field("agent_name", "Agent 名称", "Agent-A"))
                addView(field("capability", "算力 capability_id", "dog-vision"))

                addView(section("05  建组与双向消息"))
                addView(body("A 端填写建组参数；双方日志页会在群组就绪后显示消息发送区。"))
                aOnlyContainer = LinearLayout(this@MainActivity).apply {
                    orientation = LinearLayout.VERTICAL
                    addView(field(
                        "discovery_asr_url",
                        "Discovery ASR URL",
                        "http://server:9004/api/v1/transcribe",
                    ))
                    addView(field("dnn", "DNN", "internet"))
                    addView(field("group_name", "群组名称", "android-ab-test-group"))
                }.also(::addView)
                addView(field("message", "手动消息预填内容", selectedRole.defaultMessage))

                addView(actionButton("启动角色 A", filled = true) { startTest() }.also {
                    startButton = it
                    val params = LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        dp(54),
                    )
                    params.topMargin = dp(26)
                    it.layoutParams = params
                })
                addView(body("启动后进入日志页。VPN 授权只由 Android 系统弹窗申请；Agent TUN IP 由 GET /v1/ue/info 返回。"))
            })
        }
        root.addView(scroll, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT,
        ))
        setContentView(root)
        restoreFormValues()
        updateRole(selectedRole, updatePorts = false)
    }

    private fun roleSelector(): View {
        val container = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            background = rounded(Palette.SURFACE, 14f, Palette.LINE)
            setPadding(dp(4), dp(4), dp(4), dp(4))
        }
        roleAButton = roleChoice("A  发起方") { updateRole(TestRole.A) }
        roleBButton = roleChoice("B  能力方") { updateRole(TestRole.B) }
        container.addView(roleAButton, LinearLayout.LayoutParams(0, dp(48), 1f))
        container.addView(roleBButton, LinearLayout.LayoutParams(0, dp(48), 1f))
        val params = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        )
        params.topMargin = dp(22)
        container.layoutParams = params
        return container
    }

    private fun updateRole(role: TestRole, updatePorts: Boolean = true) {
        val previous = selectedRole
        selectedRole = role
        roleAButton?.apply {
            background = rounded(if (role == TestRole.A) Palette.ACCENT else Color.TRANSPARENT, 10f)
            setTextColor(if (role == TestRole.A) Color.WHITE else Palette.MUTED)
        }
        roleBButton?.apply {
            background = rounded(if (role == TestRole.B) Palette.ACCENT else Color.TRANSPARENT, 10f)
            setTextColor(if (role == TestRole.B) Color.WHITE else Palette.MUTED)
        }
        aOnlyContainer?.visibility = if (role == TestRole.A) View.VISIBLE else View.GONE
        startButton?.text = "启动角色 ${role.name}"
        fields["agent_name"]?.let { value ->
            if (value.text.isBlank() || value.text.toString() == "Agent-${previous.name}") {
                value.setText("Agent-${role.name}")
            }
        }
        fields["owner"]?.let { value ->
            val oldDefault = "android-test-owner-${previous.name.lowercase()}"
            if (value.text.isBlank() || value.text.toString() == oldDefault) {
                value.setText("android-test-owner-${role.name.lowercase()}")
            }
        }
        fields["message"]?.let { value ->
            if (value.text.isBlank() || value.text.toString() == previous.defaultMessage) {
                value.setText(role.defaultMessage)
            }
        }
        if (updatePorts) {
            val runtime = fields["runtime_port"]
            if (runtime?.text.isNullOrBlank() || runtime?.text.toString() == previous.defaultRuntimePort.toString()) {
                runtime?.setText(role.defaultRuntimePort.toString())
            }
            val masque = fields["masque_port"]
            if (masque?.text.isNullOrBlank() || masque?.text.toString() == previous.defaultMasquePort.toString()) {
                masque?.setText(role.defaultMasquePort.toString())
            }
        }
    }

    private fun startTest() {
        val config = readConfig()
        val errors = config.validate()
        if (errors.isNotEmpty()) {
            Toast.makeText(this, errors.first(), Toast.LENGTH_LONG).show()
            return
        }
        persistFormValues(config)
        activeConfig = config
        lastSdkDiagnosticSummary = null
        lastDiagnosticLocalTcpEndpoint = null
        lastVideoPreviewSummary = null
        showLogScreen(config)
        appendLog(LabLogLevel.INFO, "APP", "配置校验完成；准备请求 VPN 权限")
        bindVpnService()
    }

    private fun bindVpnService() {
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
            appendLog(LabLogLevel.ERROR, "VPN", "无法绑定 AgentVpnService")
            setRunnerStatus(RunnerStatus("VPN 服务启动失败", "点击重试", canRetry = true))
        }
    }

    private fun requestVpnOrStart() {
        val permission = VpnService.prepare(this)
        if (permission != null) {
            appendLog(LabLogLevel.INFO, "VPN", "等待系统 VPN 授权")
            startActivityForResult(permission, VPN_PERMISSION_REQUEST)
        } else {
            startSdk()
        }
    }

    @Deprecated("Uses the platform VPN permission callback without additional UI dependencies")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != VPN_PERMISSION_REQUEST) return
        if (resultCode == RESULT_OK) {
            appendLog(LabLogLevel.SUCCESS, "VPN", "系统 VPN 权限已授予")
            startSdk()
        } else {
            appendLog(LabLogLevel.ERROR, "VPN", "用户拒绝了 VPN 权限")
            setRunnerStatus(RunnerStatus("需要 VPN 权限", "授权后才能创建 Agent TUN", true))
        }
    }

    private fun startSdk() {
        if (runnerJob?.isActive == true) return
        val service = vpnService ?: run {
            setRunnerStatus(RunnerStatus("VPN 服务尚未连接", "点击重试", true))
            return
        }
        val config = activeConfig ?: return
        val value = AgentSdk.create(service)
        val flow = AgentTestRunner(
            value,
            config,
            ::appendLog,
            ::setRunnerStatus,
            ::setManualMessageSession,
            ::setResetAvailable,
            ::setComputeActionAvailable,
            ::requestProducerVideoStart,
            processedVideoRenderSinks = ::processedVideoRenderSinks,
            onProcessedVideoStatus = ::setProcessedVideoStatus,
            onFeatureStateChanged = ::setSdkFeatureState,
        )
        sdk = value
        runner = flow
        runnerJob = scope.launch {
            try {
                flow.run()
            } catch (_: CancellationException) {
                appendLog(LabLogLevel.INFO, "APP", "测试任务已停止")
            } catch (error: Exception) {
                appendLog(
                    LabLogLevel.ERROR,
                    "APP",
                    "流程异常：${error.message ?: error::class.java.simpleName}；SDK 保持至手动停止",
                )
                setRunnerStatus(RunnerStatus("流程异常", "可停止后返回配置重启", false))
            }
        }
    }

    private fun showLogScreen(config: TestConfig) {
        logLines.clear()
        diagnosticLogLines.clear()
        manualMessageSession = null
        manualMessageSending = false
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(22), dp(18), dp(18))
            setBackgroundColor(Palette.INK)
        }
        root.addView(LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            addView(LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL
                addView(TextView(this@MainActivity).apply {
                    text = "●  ${config.role.name} / CONNECT-IP"
                    setTextColor(Palette.LINK)
                    textSize = 12f
                    typeface = Typeface.DEFAULT_BOLD
                    letterSpacing = .08f
                })
                addView(TextView(this@MainActivity).apply {
                    text = "联调日志"
                    setTextColor(Color.WHITE)
                    textSize = 28f
                    typeface = Typeface.DEFAULT_BOLD
                })
            }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.HORIZONTAL
                addView(actionButton("复制日志", filled = false) { copyLogs() }.apply {
                    setTextColor(Palette.LINK)
                }, LinearLayout.LayoutParams(dp(92), dp(42)))
                addView(Space(this@MainActivity), LinearLayout.LayoutParams(dp(8), 1))
                addView(actionButton("Dump 日志", filled = false) { dumpLogs() }.apply {
                    setTextColor(Palette.WARNING)
                }, LinearLayout.LayoutParams(dp(108), dp(42)))
            })
        })

        val controls = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
        }
        controls.addView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(14), dp(16), dp(14))
            background = rounded(Palette.INK_SURFACE, 14f, Palette.INK_LINE)
            logStatusTitle = TextView(this@MainActivity).apply {
                text = "正在启动"
                setTextColor(Color.WHITE)
                textSize = 17f
                typeface = Typeface.DEFAULT_BOLD
            }.also(::addView)
            logStatusDetail = TextView(this@MainActivity).apply {
                text = "角色 ${config.role.name} · ${config.serverIp}:${config.runtimePort}"
                setTextColor(Palette.INK_MUTED)
                textSize = 13f
                setPadding(0, dp(5), 0, 0)
            }.also(::addView)
        }, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(18) })

        if (config.role == TestRole.A) {
            controls.addView(processedVideoPreview(), LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(12) })
        }

        controls.addView(manualMessageComposer(config), LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(12) })

        controls.addView(computeVideoControls(), LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(12) })

        controls.addView(sdkFeatureControls(config), LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(12) })

        controlScroll = ScrollView(this).apply {
            isFillViewport = true
            overScrollMode = View.OVER_SCROLL_IF_CONTENT_SCROLLS
            addView(controls, ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ))
        }.also {
            root.addView(it, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                0,
                1.15f,
            ))
        }

        logOutput = TextView(this).apply {
            setTextColor(Palette.LOG_TEXT)
            textSize = 12f
            typeface = Typeface.MONOSPACE
            setLineSpacing(0f, 1.22f)
            setPadding(dp(2), dp(14), dp(2), dp(20))
            setTextIsSelectable(true)
        }
        logScroll = ScrollView(this).apply {
            isFillViewport = true
            addView(logOutput, ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ))
        }
        root.addView(logScroll, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            0,
            .85f,
        ).apply { topMargin = dp(8) })

        root.addView(LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            retryButton = actionButton("重试当前接口", filled = true) {
                disarmReset()
                if (runner != null) runner?.retryCurrentStep() else requestVpnOrStart()
            }.apply { visibility = View.GONE }.also {
                addView(it, LinearLayout.LayoutParams(0, dp(50), 1f))
            }
            addView(Space(this@MainActivity), LinearLayout.LayoutParams(dp(10), 1))
            resetButton = actionButton("重置到状态1", filled = false) {
                requestAgentReset()
            }.apply {
                setTextColor(Palette.WARNING)
                isEnabled = false
                alpha = .45f
            }.also {
                addView(it, LinearLayout.LayoutParams(0, dp(50), 1f))
            }
            addView(Space(this@MainActivity), LinearLayout.LayoutParams(dp(10), 1))
            stopButton = actionButton("停止", filled = false) { stopOrReturn() }.also {
                addView(it, LinearLayout.LayoutParams(0, dp(50), 1f))
            }
        })
        setContentView(root)
    }

    private fun manualMessageComposer(config: TestConfig): View = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(16), dp(14), dp(16), dp(16))
        background = rounded(Palette.INK_SURFACE, 14f, Palette.INK_LINE)
        visibility = View.GONE
        manualMessagePanel = this

        addView(TextView(this@MainActivity).apply {
            text = "MANUAL A2A  /  GROUP ROUTE"
            setTextColor(Palette.LINK)
            textSize = 11f
            typeface = Typeface.DEFAULT_BOLD
            letterSpacing = .08f
        })
        manualRouteLabel = TextView(this@MainActivity).apply {
            text = "${config.role.name} → 等待群组配置"
            setTextColor(Color.WHITE)
            textSize = 17f
            typeface = Typeface.DEFAULT_BOLD
            setPadding(0, dp(5), 0, dp(3))
        }.also(::addView)
        addView(TextView(this@MainActivity).apply {
            text = "消息经 SDK 的群组缓存解析对端 Agent ID、IP 和端口。"
            setTextColor(Palette.INK_MUTED)
            textSize = 12f
        })
        manualMessageInput = EditText(this@MainActivity).apply {
            setText(config.message)
            hint = "输入要发送的文本"
            setHintTextColor(Palette.INK_HINT)
            setTextColor(Color.WHITE)
            textSize = 14f
            minLines = 2
            maxLines = 4
            gravity = Gravity.TOP or Gravity.START
            setPadding(dp(12), dp(10), dp(12), dp(10))
            background = rounded(Palette.INK_INPUT, 10f, Palette.INK_LINE)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE
        }.also {
            addView(it, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(12) })
        }
        manualSendButton = actionButton("发送消息", filled = true) {
            sendManualMessage()
        }.apply {
            isEnabled = false
            alpha = .45f
        }.also {
            addView(it, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(48),
            ).apply { topMargin = dp(10) })
        }
    }

    private fun computeVideoControls(): View = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(16), dp(14), dp(16), dp(16))
        background = rounded(Palette.INK_SURFACE, 14f, Palette.INK_LINE)
        visibility = View.GONE
        computeVideoPanel = this
        addView(TextView(this@MainActivity).apply {
            text = "COMPUTE VIDEO  /  ASYNC SESSION"
            setTextColor(Palette.LINK)
            textSize = 11f
            typeface = Typeface.DEFAULT_BOLD
            letterSpacing = .08f
        })
        addView(TextView(this@MainActivity).apply {
            text = if (activeConfig?.role == TestRole.A) {
                "A 创建正式算力会话并接收处理流；Sandbox 端点由 SDK 等待 C-02 后使用。"
            } else {
                "收到 A 的会话 ID 后自动申请摄像头权限，并等待 producer C-02 开始上传。"
            }
            setTextColor(Palette.INK_MUTED)
            textSize = 12f
            setPadding(0, dp(6), 0, 0)
        })
        computeVideoButton = actionButton("开始视频算力测试", filled = true) {
            startComputeVideo()
        }.apply {
            isEnabled = false
            alpha = .45f
        }.also {
            addView(it, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(48),
            ).apply { topMargin = dp(10) })
        }
    }

    private fun sdkFeatureControls(config: TestConfig): View = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(16), dp(14), dp(16), dp(16))
        background = rounded(Palette.INK_SURFACE, 14f, Palette.INK_LINE)
        sdkFeaturePanel = this

        addView(TextView(this@MainActivity).apply {
            text = "SDK FEATURE LAB  /  COMPLETE COVERAGE"
            setTextColor(Palette.LINK)
            textSize = 11f
            typeface = Typeface.DEFAULT_BOLD
            letterSpacing = .08f
        })
        sdkFeatureStatus = TextView(this@MainActivity).apply {
            text = "等待 Agent Card 与群组配置"
            setTextColor(Palette.INK_MUTED)
            textSize = 12f
            setPadding(0, dp(6), 0, dp(10))
        }.also(::addView)

        addView(featureCaption("语音识别 · pruned_sandbox :9004"))
        asrTranscribeButton = actionButton("开始录音 · 仅转文字", filled = false) {
            toggleVoiceRecording(VoiceMode.TRANSCRIBE)
        }
        voiceControlButton = actionButton("开始录音 · 运行期意图", filled = false) {
            toggleVoiceRecording(VoiceMode.RUNTIME_INTENT)
        }
        addView(featureButtonRow(
            checkNotNull(asrTranscribeButton),
            checkNotNull(voiceControlButton),
        ))
        voiceTranscriptionResult = TextView(this@MainActivity).apply {
            text = "最近转写结果：<暂无>"
            setTextColor(Color.WHITE)
            textSize = 14f
            maxLines = 6
            setTextIsSelectable(true)
            setPadding(dp(11), dp(10), dp(11), dp(10))
            background = rounded(Palette.INK_INPUT, 9f, Palette.INK_LINE)
        }.also {
            addView(it, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(7) })
        }

        groupSnapshotButton = actionButton("读取群组快照", filled = false) {
            runSdkFeatureAction("群组快照") { inspectGroupSnapshot() }
        }
        addView(featureButtonRow(checkNotNull(groupSnapshotButton)))

        addView(featureCaption("Agent Card 能力更新"))
        capabilitySkillInput = featureInput(
            value = config.capability,
            hintText = "技能名称",
        ).also(::addView)
        capabilityVcInput = featureInput(
            value = "",
            hintText = "新增时粘贴对应 VC JSON；删除时可留空",
            multiline = true,
        ).also {
            addView(it, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(7) })
        }
        capabilityAddButton = actionButton("新增能力", filled = false) {
            val skill = capabilitySkillInput?.text?.toString().orEmpty()
            val rawCredential = capabilityVcInput?.text?.toString()?.trim().orEmpty()
            runSdkFeatureAction("能力新增") {
                val credential = rawCredential.takeIf(String::isNotEmpty)
                    ?.let { Json.parseToJsonElement(it).jsonObject }
                val result = updatePublishedCapability(skill, add = true, credential = credential)
                "success=${result.success}，${result.message.ifBlank { "Agent Card 已更新" }}"
            }
        }
        capabilityRemoveButton = actionButton("删除能力", filled = false) {
            val skill = capabilitySkillInput?.text?.toString().orEmpty()
            runSdkFeatureAction("能力删除") {
                val result = updatePublishedCapability(skill, add = false)
                "success=${result.success}，${result.message.ifBlank { "Agent Card 已更新" }}"
            }
        }
        addView(featureButtonRow(
            checkNotNull(capabilityAddButton),
            checkNotNull(capabilityRemoveButton),
        ))

        addView(featureCaption("算力会话生命周期"))
        computeQueryButton = actionButton("查询 QUERY", filled = false) {
            runSdkFeatureAction("算力查询") { queryActiveComputingSession() }
        }
        computeCancelButton = actionButton("取消 CANCEL", filled = false) {
            runSdkFeatureAction("算力取消") { cancelActiveComputingSession() }
        }
        computeReleaseButton = actionButton("释放 RELEASE", filled = false) {
            runSdkFeatureAction("算力释放") { releaseActiveComputingSession() }
        }
        addView(featureButtonRow(
            checkNotNull(computeQueryButton),
            checkNotNull(computeCancelButton),
            checkNotNull(computeReleaseButton),
        ))

        if (config.role == TestRole.B) {
            addView(featureCaption("Producer 视频控制"))
            videoUploadToggleButton = actionButton("暂停摄像头上传", filled = false) {
                runSdkFeatureAction("视频上传控制") { toggleVideoUpload() }
            }
            addView(featureButtonRow(checkNotNull(videoUploadToggleButton)))
        } else {
            addView(featureCaption("持续识别目标"))
            recognitionTargetInput = featureInput(
                value = "寻找画面中的红色物体",
                hintText = "识别目标",
            ).also(::addView)
            recognitionUpdateButton = actionButton("写入目标", filled = false) {
                val target = recognitionTargetInput?.text?.toString().orEmpty()
                runSdkFeatureAction("识别目标写入") { updateRecognitionTarget(target) }
            }
            recognitionGetButton = actionButton("读取目标", filled = false) {
                runSdkFeatureAction("识别目标读取") { getRecognitionTarget() }
            }
            addView(featureButtonRow(
                checkNotNull(recognitionUpdateButton),
                checkNotNull(recognitionGetButton),
            ))

        }
        refreshSdkFeatureControls()
    }

    private fun featureCaption(value: String) = TextView(this).apply {
        text = value
        setTextColor(Color.WHITE)
        textSize = 13f
        typeface = Typeface.DEFAULT_BOLD
        setPadding(0, dp(13), 0, dp(7))
    }

    private fun featureInput(value: String, hintText: String, multiline: Boolean = false) =
        EditText(this).apply {
            setText(value)
            hint = hintText
            setHintTextColor(Palette.INK_HINT)
            setTextColor(Color.WHITE)
            textSize = 13f
            minLines = if (multiline) 2 else 1
            maxLines = if (multiline) 5 else 2
            setPadding(dp(11), dp(9), dp(11), dp(9))
            background = rounded(Palette.INK_INPUT, 9f, Palette.INK_LINE)
            inputType = InputType.TYPE_CLASS_TEXT or
                (if (multiline) InputType.TYPE_TEXT_FLAG_MULTI_LINE else 0)
        }

    private fun featureButtonRow(vararg buttons: TextView) = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        buttons.forEachIndexed { index, button ->
            if (index > 0) addView(Space(this@MainActivity), LinearLayout.LayoutParams(dp(7), 1))
            addView(button, LinearLayout.LayoutParams(0, dp(44), 1f))
        }
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(7) }
    }

    private fun setSdkFeatureState(state: SdkFeatureState) {
        runOnUiThread {
            sdkFeatureState = state
            refreshSdkFeatureControls()
        }
    }

    private fun refreshSdkFeatureControls() {
        val state = sdkFeatureState
        val featureBusy = sdkFeatureActionRunning || voiceRecordingMode != null
        fun TextView?.available(value: Boolean) {
            this ?: return
            isEnabled = value && !featureBusy
            alpha = if (isEnabled) 1f else .42f
        }
        val cardReady = state?.cardPublished == true
        val groupReady = state?.groupReady == true
        val sessionReady = state?.computeSessionReady == true
        val consumerRuntimeReady = activeConfig?.role == TestRole.A &&
            state?.processedVideoState != null
        groupSnapshotButton.available(groupReady)
        capabilityAddButton.available(cardReady)
        capabilityRemoveButton.available(cardReady)
        computeQueryButton.available(sessionReady && activeConfig?.role == TestRole.A)
        computeCancelButton.available(sessionReady && activeConfig?.role == TestRole.A)
        computeReleaseButton.available(sessionReady && activeConfig?.role == TestRole.A)
        videoUploadToggleButton.available(state?.producerVideoReady == true)
        recognitionUpdateButton.available(consumerRuntimeReady)
        recognitionGetButton.available(consumerRuntimeReady)
        asrTranscribeButton.available(cardReady)
        voiceControlButton.available(consumerRuntimeReady)
        when (voiceRecordingMode) {
            VoiceMode.TRANSCRIBE -> asrTranscribeButton?.apply {
                isEnabled = true
                alpha = 1f
                text = "停止并提交 · 仅转文字"
            }
            VoiceMode.RUNTIME_INTENT -> voiceControlButton?.apply {
                isEnabled = true
                alpha = 1f
                text = "停止并提交 · 运行期意图"
            }
            null -> {
                asrTranscribeButton?.text = "开始录音 · 仅转文字"
                voiceControlButton?.text = "开始录音 · 运行期意图"
            }
        }
        videoUploadToggleButton?.text = if (state?.videoUploadState == "PAUSED") {
            "恢复摄像头上传"
        } else {
            "暂停摄像头上传"
        }
        sdkFeatureStatus?.text = when {
            sdkFeatureActionRunning -> "语音上传或接口调用中，其他功能按钮暂时锁定"
            voiceRecordingMode != null -> "正在录音；再次点击当前语音按钮停止并提交"
            state == null -> "等待 SDK 初始化"
            else -> "Card=${if (cardReady) "READY" else "WAIT"} · " +
                "Group=${if (groupReady) "READY" else "WAIT"} · " +
                "Compute=${state.computeStatus ?: if (sessionReady) "READY" else "WAIT"} · " +
                "Media=${state.videoUploadState ?: state.processedVideoState ?: "WAIT"}"
        }
        voiceTranscriptionResult?.text = when {
            voiceRecordingMode != null -> intentDisplay(state) + "\n最近转写结果：正在录音…"
            sdkFeatureActionRunning -> intentDisplay(state) + "\n最近转写结果：正在识别…"
            else -> intentDisplay(state) + "\n最近转写结果：" +
                (state?.lastTranscription?.ifBlank { "<未识别到文字>" } ?: "<暂无>")
        }
    }

    private fun intentDisplay(state: SdkFeatureState?): String =
        if (state?.recognizedIntent == null) {
            "启动意图：<等待识别>"
        } else {
            "启动意图：${state.recognizedIntent} · area=${state.recognizedArea ?: "<none>"} · " +
                "skill=${state.discoverySkill ?: "<none>"}"
        }

    private fun toggleVoiceRecording(mode: VoiceMode) {
        if (voiceRecordingMode == mode && voiceRecorder.isRecording) {
            stopAndSubmitVoice(mode)
            return
        }
        if (voiceRecorder.isRecording || sdkFeatureActionRunning) return
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            pendingVoicePermissionMode = mode
            appendLog(LabLogLevel.INFO, "MICROPHONE", "等待麦克风权限；授权后自动开始录音")
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), AUDIO_PERMISSION_REQUEST)
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
                if (mode == VoiceMode.TRANSCRIBE) "开始录音，停止后上传 ASR 9004"
            else "开始录音，停止后识别运行期动作意图（不会直接控制设备）",
            )
            refreshSdkFeatureControls()
        } catch (error: Exception) {
            val detail = error.message ?: error::class.java.simpleName
            appendLog(LabLogLevel.ERROR, "VOICE RECORD", "录音启动失败：$detail")
            setRunnerStatus(RunnerStatus("录音启动失败", detail))
        }
    }

    private fun stopAndSubmitVoice(mode: VoiceMode) {
        val activeRunner = runner ?: return
        val recording = try {
            voiceRecorder.stop()
        } catch (error: Exception) {
            voiceRecordingMode = null
            refreshSdkFeatureControls()
            val detail = error.message ?: error::class.java.simpleName
            appendLog(LabLogLevel.ERROR, "VOICE RECORD", "录音结束失败：$detail")
            setRunnerStatus(RunnerStatus("录音失败", detail))
            return
        }
        voiceRecordingMode = null
        sdkFeatureActionRunning = true
        refreshSdkFeatureControls()
        scope.launch {
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
                setRunnerStatus(
                    RunnerStatus(
                        if (mode == VoiceMode.TRANSCRIBE) "任务语音识别成功" else "运行期意图识别成功",
                        detail.take(300),
                    ),
                )
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                val detail = error.message ?: error::class.java.simpleName
                appendLog(LabLogLevel.ERROR, "VOICE", "语音接口失败：$detail")
                setRunnerStatus(RunnerStatus("语音接口失败", detail))
            } finally {
                recording.file.delete()
                sdkFeatureActionRunning = false
                setSdkFeatureState(activeRunner.featureState())
            }
        }
    }

    private fun runSdkFeatureAction(
        title: String,
        action: suspend AgentTestRunner.() -> String,
    ) {
        val activeRunner = runner ?: run {
            Toast.makeText(this, "SDK 流程尚未启动", Toast.LENGTH_SHORT).show()
            return
        }
        if (sdkFeatureActionRunning) return
        sdkFeatureActionRunning = true
        refreshSdkFeatureControls()
        scope.launch {
            try {
                val detail = activeRunner.action()
                setRunnerStatus(RunnerStatus("$title 成功", detail.take(240)))
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                val detail = error.message ?: error::class.java.simpleName
                appendLog(LabLogLevel.ERROR, "FEATURE LAB", "$title 失败：$detail")
                setRunnerStatus(RunnerStatus("$title 失败", detail))
            } finally {
                sdkFeatureActionRunning = false
                setSdkFeatureState(activeRunner.featureState())
            }
        }
    }

    private fun processedVideoPreview(): View {
        releaseProcessedVideoPreview()
        videoPreviewHasFrame = false
        videoPreviewResolution = ""
        val rendererEvents = object : RendererCommon.RendererEvents {
            override fun onFirstFrameRendered() {
                runOnUiThread {
                    videoPreviewHasFrame = true
                    startVideoStatisticsUpdates()
                    renderProcessedVideoLiveStatus()
                    appendLog(
                        LabLogLevel.SUCCESS,
                        "VIDEO DISPLAY",
                        "${videoPreviewRendererName()} 已绘制处理流首帧",
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
        val eglBase = EglBase.create().also { videoPreviewEglBase = it }
        val renderer: View = TextureViewEglRenderer(this).apply {
            init(eglBase.eglBaseContext, rendererEvents)
            setMirror(false)
            setScalingType(RendererCommon.ScalingType.SCALE_ASPECT_FIT)
        }.also { videoPreviewRenderer = it }
        val status = TextView(this).apply {
            text = "等待处理后视频\n收到 B 的邀请后自动播放"
            setTextColor(Palette.INK_MUTED)
            textSize = 13f
            gravity = Gravity.CENTER
            setPadding(dp(14), dp(9), dp(14), dp(9))
            background = rounded(Color.rgb(13, 27, 36), 9f, Palette.INK_LINE)
        }.also { videoPreviewStatus = it }

        val stage = FrameLayout(this).apply {
            setBackgroundColor(Color.BLACK)
            addView(renderer, FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            ))
            addView(status, centeredPreviewStatusParams())
        }
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(11), dp(12), dp(12))
            background = rounded(Palette.INK_SURFACE, 14f, Palette.INK_LINE)
            addView(TextView(this@MainActivity).apply {
                text = "PROCESSED VIDEO  /  N6 COMPUTE"
                setTextColor(Palette.LINK)
                textSize = 11f
                typeface = Typeface.DEFAULT_BOLD
                letterSpacing = .08f
            })
            addView(stage, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(200),
            ).apply { topMargin = dp(9) })
        }
    }

    private fun processedVideoRenderSinks(): List<VideoSink> =
        listOfNotNull(videoPreviewRenderer)

    private fun setProcessedVideoStatus(title: String, detail: String) {
        runOnUiThread {
            videoStatisticsJob?.cancel()
            videoStatisticsJob = null
            videoPreviewHasFrame = false
            (videoPreviewRenderer as? TextureViewEglRenderer)?.apply {
                if (title == "处理流已连接") resetStatistics()
                clearImage()
            }
            videoPreviewStatistics = "正在统计…"
            videoPreviewStatus?.apply {
                text = "$title\n$detail"
                setTextColor(Palette.INK_MUTED)
                textSize = 13f
                gravity = Gravity.CENTER
                background = rounded(Color.rgb(13, 27, 36), 9f, Palette.INK_LINE)
                layoutParams = centeredPreviewStatusParams()
                visibility = View.VISIBLE
            }
        }
    }

    private fun renderProcessedVideoLiveStatus() {
        videoPreviewStatus?.apply {
            text = buildString {
                append("● LIVE")
                videoPreviewResolution.takeIf(String::isNotBlank)?.let { append("  ", it) }
                append('\n', videoPreviewStatistics)
            }
            setTextColor(Palette.LINK)
            textSize = 11f
            background = rounded(Color.argb(205, 8, 17, 25), 8f, Palette.INK_LINE)
            layoutParams = FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.TOP or Gravity.END,
            ).apply {
                topMargin = dp(9)
                marginEnd = dp(9)
            }
        }
    }

    private fun startVideoStatisticsUpdates() {
        videoStatisticsJob?.cancel()
        val renderer = videoPreviewRenderer as? TextureViewEglRenderer ?: return
        videoStatisticsJob = scope.launch {
            var previous = renderer.statisticsSnapshot()
            while (isActive && videoPreviewRenderer === renderer) {
                delay(1_000)
                val current = renderer.statisticsSnapshot()
                val elapsedSeconds =
                    (current.capturedAtNanos - previous.capturedAtNanos).coerceAtLeast(1L) /
                        1_000_000_000.0
                val receiveFps = (current.framesReceived - previous.framesReceived) / elapsedSeconds
                val renderFps = (current.framesRendered - previous.framesRendered) / elapsedSeconds
                val lastFrameAge = current.lastFrameAgeMillis?.let { " · 最新 ${it}ms" }.orEmpty()
                videoPreviewStatistics = String.format(
                    Locale.US,
                    "接收 %.1f fps · 显示 %.1f fps\n卡顿 %d · 最大间隔 %dms%s",
                    receiveFps,
                    renderFps,
                    current.stutterCount,
                    current.maximumFrameGapMillis,
                    lastFrameAge,
                )
                if (videoPreviewHasFrame) renderProcessedVideoLiveStatus()
                previous = current
            }
        }
    }

    private fun centeredPreviewStatusParams() = FrameLayout.LayoutParams(
        ViewGroup.LayoutParams.WRAP_CONTENT,
        ViewGroup.LayoutParams.WRAP_CONTENT,
        Gravity.CENTER,
    )

    private fun detachProcessedVideoRenderer(): EglBase? {
        (videoPreviewRenderer as? TextureViewEglRenderer)?.let { renderer ->
            lastVideoPreviewSummary = renderer.diagnosticSummary()
            runCatching { renderer.release() }
        }
        videoPreviewRenderer = null
        val eglBase = videoPreviewEglBase
        videoPreviewEglBase = null
        return eglBase
    }

    private fun releaseProcessedVideoPreview() {
        videoStatisticsJob?.cancel()
        videoStatisticsJob = null
        detachProcessedVideoRenderer()?.let { eglBase -> runCatching { eglBase.release() } }
        videoPreviewStatus = null
        videoPreviewHasFrame = false
        videoPreviewResolution = ""
        videoPreviewStatistics = "正在统计…"
    }

    private fun videoPreviewRendererName(): String = when (videoPreviewRenderer) {
        is TextureViewEglRenderer -> "TextureView/EglRenderer"
        else -> "<unavailable>"
    }

    private fun setRunnerStatus(status: RunnerStatus) {
        runOnUiThread {
            logStatusTitle?.text = status.title
            logStatusDetail?.text = status.detail
            retryButton?.visibility = if (status.canRetry) View.VISIBLE else View.GONE
        }
    }

    private fun setManualMessageSession(session: ManualMessageSession?) {
        runOnUiThread {
            manualMessageSession = session
            manualMessagePanel?.visibility = if (session == null) View.GONE else View.VISIBLE
            if (session != null) {
                manualRouteLabel?.text =
                    "${activeConfig?.role?.name ?: "Agent"} → ${session.targetAgentName}"
                manualMessageInput?.hint = "发送给 ${session.targetAgentName}"
            }
            updateManualSendButton()
        }
    }

    private fun setComputeActionAvailable(available: Boolean) {
        runOnUiThread {
            computeVideoAvailable = available
            val reveal = available && computeVideoPanel?.visibility != View.VISIBLE
            computeVideoPanel?.visibility = if (available || computeVideoStarting) View.VISIBLE else View.GONE
            updateComputeVideoButton()
            if (reveal) {
                controlScroll?.post { controlScroll?.fullScroll(View.FOCUS_DOWN) }
            }
        }
    }

    private fun startComputeVideo() {
        val flow = runner ?: return
        if (!computeVideoAvailable || computeVideoStarting) return
        if (activeConfig?.role == TestRole.B &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            pendingVideoStart = true
            appendLog(LabLogLevel.INFO, "CAMERA", "等待摄像头权限；授权后自动继续")
            requestPermissions(arrayOf(Manifest.permission.CAMERA), CAMERA_PERMISSION_REQUEST)
            return
        }
        pendingVideoStart = false
        computeVideoStarting = true
        updateComputeVideoButton()
        scope.launch {
            try {
                flow.startVideoOffload()
                computeVideoAvailable = false
                setRunnerStatus(
                    if (activeConfig?.role == TestRole.A) {
                        RunnerStatus("处理流已连接", "A 已完成算力申请和 consumer WebRTC 协商")
                    } else {
                        RunnerStatus("视频上传已启动", "B 已完成 producer WebRTC 协商")
                    },
                )
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                computeVideoAvailable = true
                appendLog(
                    LabLogLevel.ERROR,
                    "COMPUTE VIDEO",
                    "启动失败：${error.message ?: error::class.java.simpleName}",
                )
                setRunnerStatus(RunnerStatus("视频算力测试失败", error.message ?: error::class.java.simpleName))
            } finally {
                computeVideoStarting = false
                updateComputeVideoButton()
            }
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == AUDIO_PERMISSION_REQUEST) {
            val granted = grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED
            val mode = pendingVoicePermissionMode
            pendingVoicePermissionMode = null
            appendLog(
                if (granted) LabLogLevel.SUCCESS else LabLogLevel.ERROR,
                "MICROPHONE",
                if (granted) "麦克风权限已授予" else "麦克风权限被拒绝，无法录音",
            )
            if (granted && mode != null) startVoiceRecording(mode)
            return
        }
        if (requestCode != CAMERA_PERMISSION_REQUEST) return
        val granted = grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED
        appendLog(
            if (granted) LabLogLevel.SUCCESS else LabLogLevel.ERROR,
            "CAMERA",
            if (granted) "摄像头权限已授予" else "摄像头权限被拒绝，无法启动视频上传",
        )
        if (granted && pendingVideoStart) startComputeVideo()
        pendingVideoStart = false
    }

    private fun updateComputeVideoButton() {
        computeVideoButton?.apply {
            val ready = computeVideoAvailable && !computeVideoStarting
            isEnabled = ready
            alpha = if (ready) 1f else .45f
            text = when {
                computeVideoStarting -> "正在启动视频链路…"
                !computeVideoAvailable -> "视频算力链路已启动"
                activeConfig?.role == TestRole.A -> "申请算力会话并接收视频"
                else -> "开始上传视频"
            }
        }
    }

    private fun requestProducerVideoStart() {
        runOnUiThread {
            if (activeConfig?.role != TestRole.B) return@runOnUiThread
            computeVideoAvailable = true
            computeVideoPanel?.visibility = View.VISIBLE
            updateComputeVideoButton()
            controlScroll?.post { controlScroll?.fullScroll(View.FOCUS_DOWN) }
            startComputeVideo()
        }
    }

    private fun setResetAvailable(available: Boolean) {
        runOnUiThread {
            resetAvailable = available
            if (!available) resetArmed = false
            resetButton?.apply {
                isEnabled = available && !resetInProgress
                alpha = if (isEnabled) 1f else .45f
                text = when {
                    resetInProgress -> "正在重置…"
                    resetArmed -> "再次点击确认"
                    else -> "重置到状态1"
                }
            }
        }
    }

    private fun disarmReset() {
        if (!resetArmed || resetInProgress) return
        resetArmed = false
        setResetAvailable(resetAvailable)
    }

    private fun requestAgentReset() {
        if (!resetAvailable || resetInProgress) return
        if (!resetArmed) {
            resetArmed = true
            setResetAvailable(true)
            setRunnerStatus(
                RunnerStatus(
                    "确认重置 Agent 身份",
                    "再次点击 Reset 将仅清除本地 Profile 与 Agent Card，不修改网侧身份",
                ),
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
        setRunnerStatus(RunnerStatus("正在重置到状态1", "先关闭算力会话，再清除本地身份状态"))
        scope.launch {
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
                withContext(Dispatchers.IO) { activeSdk.close() }
                if (sdk === activeSdk) sdk = null
                if (serviceBound) {
                    runCatching { unbindService(connection) }
                    serviceBound = false
                    vpnService = null
                }
                resetInProgress = false
                resetAvailable = false
                resetButton?.apply {
                    text = "已重置为状态1"
                    isEnabled = false
                    alpha = .55f
                }
                setRunnerStatus(RunnerStatus("已回到状态1", "未申请数字身份；返回配置后可重新启动"))
                stopButton?.apply {
                    text = "返回配置"
                    isEnabled = true
                }
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                runnerJob = null
                activeJob?.cancelAndJoin()
                activeRunner?.close()
                runner = null
                setManualMessageSession(null)
                resetInProgress = false
                appendLog(
                    LabLogLevel.ERROR,
                    "RESET",
                    "重置失败：${error.message ?: error::class.java.simpleName}；原身份状态未清除",
                )
                setRunnerStatus(
                    RunnerStatus(
                        "重置失败",
                        "${error.message ?: error::class.java.simpleName}；可再次尝试 Reset 或停止",
                    ),
                )
                setResetAvailable(true)
            }
        }
    }

    private fun sendManualMessage() {
        val flow = runner ?: return
        val session = manualMessageSession ?: run {
            Toast.makeText(this, "群组尚未就绪", Toast.LENGTH_SHORT).show()
            return
        }
        val content = manualMessageInput?.text?.toString()?.trim().orEmpty()
        if (content.isEmpty()) {
            manualMessageInput?.error = "请输入消息内容"
            return
        }
        if (manualMessageSending) return
        manualMessageSending = true
        updateManualSendButton()
        scope.launch {
            try {
                val receipt = flow.sendManualMessage(content)
                manualMessageInput?.text?.clear()
                setRunnerStatus(
                    RunnerStatus(
                        "消息发送成功",
                        "→ ${session.targetAgentName} · ${receipt.messageId.take(8)}",
                    ),
                )
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                setRunnerStatus(
                    RunnerStatus(
                        "消息发送失败",
                        "${error.message ?: error::class.java.simpleName}；SDK 未关闭，可继续发送",
                    ),
                )
            } finally {
                manualMessageSending = false
                updateManualSendButton()
            }
        }
    }

    private fun updateManualSendButton() {
        manualSendButton?.apply {
            val ready = manualMessageSession != null && !manualMessageSending
            isEnabled = ready
            alpha = if (ready) 1f else .45f
            text = if (manualMessageSending) "发送中…" else "发送消息"
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
            val line = SpannableString(raw).apply {
                val color = when (level) {
                    LabLogLevel.INFO -> Palette.LOG_TEXT
                    LabLogLevel.SUCCESS -> Palette.SUCCESS
                    LabLogLevel.WARNING -> Palette.WARNING
                    LabLogLevel.ERROR -> Palette.ERROR
                }
                setSpan(ForegroundColorSpan(color), 14, minOf(21, length), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            }
            diagnosticLogLines.addLast(raw)
            while (diagnosticLogLines.size > MAX_DIAGNOSTIC_LOG_LINES) {
                diagnosticLogLines.removeFirst()
            }
            logLines.addLast(line)
            while (logLines.size > MAX_LOG_LINES) logLines.removeFirst()
            logOutput?.text = logLines.joinToString("\n")
            logScroll?.post { logScroll?.fullScroll(View.FOCUS_DOWN) }
        }
    }

    private fun copyLogs() {
        val text = diagnosticLogLines.joinToString("\n")
        val clipboard = getSystemService(CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(ClipData.newPlainText("Agent Link Lab logs", text))
        Toast.makeText(this, "日志已复制", Toast.LENGTH_SHORT).show()
    }

    private fun dumpLogs() {
        val config = activeConfig ?: run {
            Toast.makeText(this, "当前没有运行配置", Toast.LENGTH_SHORT).show()
            return
        }
        appendLog(LabLogLevel.INFO, "DUMP", "开始采集端侧诊断日志")
        val appLogs = diagnosticLogLines.toList()
        val activeRunner = runner
        val activeSdk = sdk
        scope.launch {
            try {
                val dump = DiagnosticLogExporter.createDump(
                    context = this@MainActivity,
                    config = config,
                    appLogs = appLogs,
                    sdkSummary = activeRunner?.diagnosticSummary()
                        ?: lastSdkDiagnosticSummary
                        ?: "lifecycle_state=${activeSdk?.agentLifecycleState ?: "<sdk unavailable>"}\n" +
                            "agent_id=${activeSdk?.localProfile?.agentId ?: "<none>"}",
                    localTcpEndpoint = activeRunner?.diagnosticLocalTcpEndpoint()
                        ?: lastDiagnosticLocalTcpEndpoint,
                    videoPreviewSummary =
                        (videoPreviewRenderer as? TextureViewEglRenderer)?.diagnosticSummary()
                            ?: lastVideoPreviewSummary
                            ?: "renderer=<unavailable>",
                )
                appendLog(
                    LabLogLevel.SUCCESS,
                    "DUMP",
                    "诊断日志已生成：${dump.displayLocation}",
                )
                DiagnosticLogExporter.share(this@MainActivity, dump)
            } catch (error: Exception) {
                appendLog(
                    LabLogLevel.ERROR,
                    "DUMP",
                    "诊断日志生成失败：${error.message ?: error::class.java.simpleName}",
                )
                Toast.makeText(
                    this@MainActivity,
                    "诊断日志生成失败",
                    Toast.LENGTH_LONG,
                ).show()
            }
        }
    }

    private fun stopOrReturn() {
        voiceRecorder.cancel()
        voiceRecordingMode = null
        pendingVoicePermissionMode = null
        disarmReset()
        if (runnerJob == null && sdk == null) {
            showConfigScreen()
            return
        }
        stopButton?.isEnabled = false
        setRunnerStatus(RunnerStatus("正在停止", "先关闭算力会话，再向核心网发送 Agent 去注册请求"))
        val activeJob = runnerJob
        val activeRunner = runner
        val activeSdk = sdk
        runnerJob = null
        scope.launch {
            activeJob?.cancelAndJoin()
            lastSdkDiagnosticSummary = activeRunner?.diagnosticSummary()
                ?: activeSdk?.let {
                    "lifecycle_state=${it.agentLifecycleState}\n" +
                        "agent_id=${it.localProfile?.agentId ?: "<none>"}"
                }
            lastDiagnosticLocalTcpEndpoint = activeRunner?.diagnosticLocalTcpEndpoint()
            if (activeSdk != null) {
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
            setManualMessageSession(null)
            // The renderer can still retain a decoded texture after its Track sinks are removed.
            // Flush/release it before disposing the decoder's shared EGL child context.
            val previewEglBase = detachProcessedVideoRenderer()
            withContext(Dispatchers.IO) { activeSdk?.close() }
            previewEglBase?.let { eglBase -> runCatching { eglBase.release() } }
            sdk = null
            if (serviceBound) {
                runCatching { unbindService(connection) }
                serviceBound = false
                vpnService = null
            }
            appendLog(LabLogLevel.INFO, "APP", "去注册流程结束；SDK、MASQUE、TUN 与本地服务已关闭")
            setRunnerStatus(RunnerStatus("已停止 · 日志已保留", "可先 Dump 日志，再返回配置页"))
            stopButton?.apply {
                text = "返回配置"
                isEnabled = true
            }
        }
    }

    private fun readConfig(): TestConfig {
        val serverIp = value("server_ip")
        val discoveryAsrUrl = value("discovery_asr_url").takeUnless {
            it.isBlank() || it == "http://:9004/api/v1/transcribe"
        } ?: "http://$serverIp:9004/api/v1/transcribe"
        return TestConfig(
            role = selectedRole,
            serverIp = serverIp,
            runtimePort = intValue("runtime_port"),
            masquePort = intValue("masque_port"),
            masquePath = value("masque_path"),
            localTcpPort = intValue("tcp_port"),
            localUdpPort = intValue("udp_port"),
            masqueToken = value("masque_token").ifBlank { null },
            discoveryAsrUrl = discoveryAsrUrl,
            owner = value("owner"),
            agentName = value("agent_name"),
            capability = value("capability"),
            dnn = value("dnn"),
            groupName = value("group_name"),
            message = value("message"),
        )
    }

    private fun restoreFormValues() {
        val preferences = getSharedPreferences(PREFERENCES, MODE_PRIVATE)
        val roleText = intent.getStringExtra("role") ?: preferences.getString("role", "A")
        selectedRole = runCatching { TestRole.valueOf(roleText!!.uppercase()) }.getOrDefault(TestRole.A)
        val legacyMasque = intent.getStringExtra("masque_url")?.let(Uri::parse)
        setValue("server_ip", intent.getStringExtra("server_ip")
            ?: intent.getStringExtra("runtime_ip")
            ?: preferences.getString("server_ip", ""))
        setValue("runtime_port", intentInt("runtime_port", preferences.getInt("runtime_port", selectedRole.defaultRuntimePort)))
        setValue("masque_port", intentInt("masque_port", legacyMasque?.port?.takeIf { it > 0 }
            ?: preferences.getInt("masque_port", selectedRole.defaultMasquePort)))
        setValue("masque_path", intent.getStringExtra("masque_path")
            ?: legacyMasque?.path?.takeIf(String::isNotBlank)
            ?: preferences.getString("masque_path", "/.well-known/masque/ip"))
        setValue("tcp_port", intentInt("tcp_port", preferences.getInt("tcp_port", 4001)))
        setValue("udp_port", intentInt("udp_port", preferences.getInt("udp_port", 28443)))
        setValue("owner", intent.getStringExtra("owner")
            ?: preferences.getString("owner", "android-test-owner-${selectedRole.name.lowercase()}"))
        setValue("agent_name", intent.getStringExtra("agent_name")
            ?: preferences.getString("agent_name", "Agent-${selectedRole.name}"))
        val storedCapability = preferences.getString("capability", "dog-vision")
        setValue(
            "capability",
            intent.getStringExtra("capability")
                ?: storedCapability?.let { if (it == "text") "dog-vision" else it },
        )
        setValue("dnn", intent.getStringExtra("dnn") ?: preferences.getString("dnn", "internet"))
        val configuredServer = fields["server_ip"]?.text?.toString().orEmpty()
        setValue(
            "discovery_asr_url",
            intent.getStringExtra("discovery_asr_url")
                ?: preferences.getString("discovery_asr_url", null)
                ?: configuredServer.takeIf(String::isNotBlank)
                    ?.let { "http://$it:9004/api/v1/transcribe" }
                ?: "",
        )
        setValue("group_name", intent.getStringExtra("group_name")
            ?: preferences.getString("group_name", "android-ab-test-group"))
        setValue("message", intent.getStringExtra("message")
            ?: preferences.getString("message", selectedRole.defaultMessage))
        setValue("masque_token", intent.getStringExtra("masque_token") ?: "")
    }

    private fun persistFormValues(config: TestConfig) {
        getSharedPreferences(PREFERENCES, MODE_PRIVATE).edit()
            .putString("role", config.role.name)
            .putString("server_ip", config.serverIp)
            .putInt("runtime_port", config.runtimePort)
            .putInt("masque_port", config.masquePort)
            .putString("masque_path", config.masquePath)
            .putInt("tcp_port", config.localTcpPort)
            .putInt("udp_port", config.localUdpPort)
            .putString("owner", config.owner)
            .putString("agent_name", config.agentName)
            .putString("capability", config.capability)
            .putString("discovery_asr_url", config.discoveryAsrUrl)
            .putString("dnn", config.dnn)
            .putString("group_name", config.groupName)
            .putString("message", config.message)
            .apply()
    }

    private fun field(
        key: String,
        label: String,
        hint: String,
        numeric: Boolean = false,
        password: Boolean = false,
    ): View = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        val params = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        )
        params.topMargin = dp(12)
        layoutParams = params
        addView(TextView(this@MainActivity).apply {
            text = label
            setTextColor(Palette.TEXT)
            textSize = 12f
            typeface = Typeface.DEFAULT_BOLD
        })
        addView(EditText(this@MainActivity).apply {
            this.hint = hint
            setHintTextColor(Palette.HINT)
            setTextColor(Palette.TEXT)
            textSize = 15f
            setSingleLine(true)
            setPadding(dp(13), 0, dp(13), 0)
            background = rounded(Palette.SURFACE, 10f, Palette.LINE)
            inputType = when {
                numeric -> InputType.TYPE_CLASS_NUMBER
                password -> InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
                else -> InputType.TYPE_CLASS_TEXT
            }
            fields[key] = this
        }, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            dp(48),
        ).apply { topMargin = dp(6) })
    }

    private fun twoColumns(first: View, second: View): View = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        addView(first, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        addView(Space(this@MainActivity), LinearLayout.LayoutParams(dp(10), 1))
        addView(second, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
    }

    private fun eyebrow(text: String) = TextView(this).apply {
        this.text = text
        setTextColor(Palette.ACCENT)
        textSize = 11f
        typeface = Typeface.DEFAULT_BOLD
        letterSpacing = .12f
    }

    private fun title(text: String) = TextView(this).apply {
        this.text = text
        setTextColor(Palette.TEXT)
        textSize = 32f
        typeface = Typeface.DEFAULT_BOLD
        setPadding(0, dp(7), 0, dp(7))
    }

    private fun body(text: String) = TextView(this).apply {
        this.text = text
        setTextColor(Palette.MUTED)
        textSize = 13f
        setLineSpacing(0f, 1.25f)
        setPadding(0, dp(6), 0, dp(4))
    }

    private fun section(text: String) = TextView(this).apply {
        this.text = text
        setTextColor(Palette.ACCENT)
        textSize = 12f
        typeface = Typeface.DEFAULT_BOLD
        letterSpacing = .06f
        setPadding(0, dp(24), 0, 0)
    }

    private fun roleChoice(text: String, onClick: () -> Unit) = TextView(this).apply {
        this.text = text
        gravity = Gravity.CENTER
        textSize = 14f
        typeface = Typeface.DEFAULT_BOLD
        isClickable = true
        setOnClickListener { onClick() }
    }

    private fun actionButton(text: String, filled: Boolean, onClick: () -> Unit) = TextView(this).apply {
        this.text = text
        gravity = Gravity.CENTER
        textSize = 14f
        typeface = Typeface.DEFAULT_BOLD
        setTextColor(if (filled) Color.WHITE else Palette.INK_MUTED)
        background = if (filled) {
            rounded(Palette.ACCENT, 12f)
        } else {
            rounded(Color.TRANSPARENT, 12f, Palette.INK_LINE)
        }
        isClickable = true
        setOnClickListener { onClick() }
    }

    private fun rounded(fill: Int, radius: Float, stroke: Int? = null) = GradientDrawable().apply {
        shape = GradientDrawable.RECTANGLE
        color = android.content.res.ColorStateList.valueOf(fill)
        cornerRadius = dp(radius.toInt()).toFloat()
        stroke?.let { setStroke(dp(1), it) }
    }

    private fun value(key: String): String = fields.getValue(key).text.toString().trim()
    private fun intValue(key: String): Int = value(key).toIntOrNull() ?: 0
    private fun setValue(key: String, value: Any?) = fields[key]?.setText(value?.toString().orEmpty()) ?: Unit
    private fun intentInt(name: String, default: Int): Int =
        if (intent.hasExtra(name)) intent.getIntExtra(name, default) else default
    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    override fun onDestroy() {
        voiceRecorder.cancel()
        runnerJob?.cancel()
        runner?.close()
        val previewEglBase = detachProcessedVideoRenderer()
        runBlocking(Dispatchers.IO) { runCatching { sdk?.close() } }
        previewEglBase?.let { eglBase -> runCatching { eglBase.release() } }
        releaseProcessedVideoPreview()
        if (serviceBound) runCatching { unbindService(connection) }
        scope.cancel()
        super.onDestroy()
    }

    private object Palette {
        val CANVAS = Color.rgb(243, 246, 248)
        val SURFACE = Color.WHITE
        val TEXT = Color.rgb(17, 32, 44)
        val MUTED = Color.rgb(84, 103, 116)
        val HINT = Color.rgb(146, 159, 168)
        val LINE = Color.rgb(214, 223, 228)
        val ACCENT = Color.rgb(0, 112, 128)
        val INK = Color.rgb(8, 17, 25)
        val INK_SURFACE = Color.rgb(16, 30, 41)
        val INK_INPUT = Color.rgb(9, 22, 31)
        val INK_LINE = Color.rgb(43, 62, 74)
        val INK_MUTED = Color.rgb(158, 176, 187)
        val INK_HINT = Color.rgb(105, 126, 139)
        val LINK = Color.rgb(67, 210, 202)
        val LOG_TEXT = Color.rgb(191, 207, 216)
        val SUCCESS = Color.rgb(88, 217, 146)
        val WARNING = Color.rgb(246, 184, 80)
        val ERROR = Color.rgb(255, 113, 113)
    }

    private companion object {
        const val VPN_PERMISSION_REQUEST = 1001
        const val CAMERA_PERMISSION_REQUEST = 1002
        const val AUDIO_PERMISSION_REQUEST = 1003
        const val PREFERENCES = "agent-link-lab"
        const val MAX_LOG_LINES = 300
        const val MAX_DIAGNOSTIC_LOG_LINES = 2_000
        const val APP_LOG_TAG = "AgentLinkLab"
    }

    private enum class VoiceMode { TRANSCRIBE, RUNTIME_INTENT }
}
