package com.rayneo.agent.example

import android.annotation.SuppressLint
import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
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
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Space
import android.widget.TextView
import android.widget.Toast
import com.rayneo.agent.sdk.AgentSdk
import com.rayneo.agent.sdk.vpn.AgentVpnService
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@SuppressLint("SetTextI18n")
class MainActivity : Activity() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val fields = mutableMapOf<String, EditText>()
    private val logLines = ArrayDeque<CharSequence>()

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
    private var stopButton: TextView? = null
    private var logOutput: TextView? = null
    private var logScroll: ScrollView? = null

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
        fields.clear()
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
                addView(body("填写端侧与服务器参数。应用会按 Linux 示例完成身份、能力、发现、建组和 MASQUE A2A 验证。"))
                addView(roleSelector())

                addView(section("01  测试角色"))
                addView(body("A 按能力发现并向 B 发消息；B 发布能力并自动接受邀请。"))

                addView(section("02  服务器与隧道"))
                addView(field("server_ip", "服务器 IP / 域名", "由部署方提供"))
                addView(twoColumns(
                    field("runtime_port", "Runtime HTTP 端口", "8088", numeric = true),
                    field("masque_port", "MASQUE QUIC 端口", "8443", numeric = true),
                ))
                addView(field("masque_path", "CONNECT-IP 路径", "/.well-known/masque/ip"))
                addView(field("masque_token", "MASQUE Token（可选，不保存）", "Bearer token", password = true))

                addView(section("03  端侧网络"))
                addView(field("local_vlan_ip", "本机 Wi-Fi / VLAN IP", "当前设备可达服务器的地址"))
                addView(twoColumns(
                    field("tcp_port", "A2A TCP 端口", "4001", numeric = true),
                    field("udp_port", "A2A UDP 端口", "28443", numeric = true),
                ))

                addView(section("04  Agent Profile"))
                addView(field("owner", "Owner", "测试终端归属标识"))
                addView(field("agent_name", "Agent 名称", "Agent-A"))
                addView(field("capability", "B 发布 / A 发现的能力", "text"))

                aOnlyContainer = LinearLayout(this@MainActivity).apply {
                    orientation = LinearLayout.VERTICAL
                    addView(section("05  A 端任务"))
                    addView(field("dnn", "DNN", "internet"))
                    addView(field("group_name", "群组名称", "android-ab-test-group"))
                    addView(field("message", "发送给 B 的消息", "hello Agent B from Android A"))
                }.also(::addView)

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
        val flow = AgentTestRunner(value, config, ::appendLog, ::setRunnerStatus)
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
            addView(actionButton("复制日志", filled = false) { copyLogs() }.apply {
                setTextColor(Palette.LINK)
            }, LinearLayout.LayoutParams(dp(96), dp(42)))
        })

        root.addView(LinearLayout(this).apply {
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
            1f,
        ))

        root.addView(LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            retryButton = actionButton("重试当前接口", filled = true) {
                if (runner != null) runner?.retryCurrentStep() else requestVpnOrStart()
            }.apply { visibility = View.GONE }.also {
                addView(it, LinearLayout.LayoutParams(0, dp(50), 1f))
            }
            addView(Space(this@MainActivity), LinearLayout.LayoutParams(dp(10), 1))
            stopButton = actionButton("停止", filled = false) { stopOrReturn() }.also {
                addView(it, LinearLayout.LayoutParams(0, dp(50), 1f))
            }
        })
        setContentView(root)
    }

    private fun setRunnerStatus(status: RunnerStatus) {
        runOnUiThread {
            logStatusTitle?.text = status.title
            logStatusDetail?.text = status.detail
            retryButton?.visibility = if (status.canRetry) View.VISIBLE else View.GONE
        }
    }

    private fun appendLog(level: LabLogLevel, stage: String, message: String) {
        runOnUiThread {
            val time = SimpleDateFormat("HH:mm:ss.SSS", Locale.US).format(Date())
            val raw = "$time  ${level.name.padEnd(7)}  ${stage.padEnd(17)}  $message"
            val line = SpannableString(raw).apply {
                val color = when (level) {
                    LabLogLevel.INFO -> Palette.LOG_TEXT
                    LabLogLevel.SUCCESS -> Palette.SUCCESS
                    LabLogLevel.WARNING -> Palette.WARNING
                    LabLogLevel.ERROR -> Palette.ERROR
                }
                setSpan(ForegroundColorSpan(color), 14, minOf(21, length), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            }
            logLines.addLast(line)
            while (logLines.size > MAX_LOG_LINES) logLines.removeFirst()
            logOutput?.text = logLines.joinToString("\n")
            logScroll?.post { logScroll?.fullScroll(View.FOCUS_DOWN) }
        }
    }

    private fun copyLogs() {
        val text = logLines.joinToString("\n")
        val clipboard = getSystemService(CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(ClipData.newPlainText("Agent Link Lab logs", text))
        Toast.makeText(this, "日志已复制", Toast.LENGTH_SHORT).show()
    }

    private fun stopOrReturn() {
        if (runnerJob == null && sdk == null) {
            showConfigScreen()
            return
        }
        stopButton?.isEnabled = false
        scope.launch {
            runnerJob?.cancel()
            runnerJob = null
            runner?.close()
            runner = null
            withContext(Dispatchers.IO) { sdk?.close() }
            sdk = null
            if (serviceBound) {
                runCatching { unbindService(connection) }
                serviceBound = false
                vpnService = null
            }
            appendLog(LabLogLevel.INFO, "APP", "SDK、MASQUE、TUN 与本地服务已关闭")
            setRunnerStatus(RunnerStatus("已停止", "可以返回配置页修改参数"))
            stopButton?.apply {
                text = "返回配置"
                isEnabled = true
            }
        }
    }

    private fun readConfig(): TestConfig = TestConfig(
        role = selectedRole,
        serverIp = value("server_ip"),
        runtimePort = intValue("runtime_port"),
        masquePort = intValue("masque_port"),
        masquePath = value("masque_path"),
        localVlanIp = value("local_vlan_ip"),
        localTcpPort = intValue("tcp_port"),
        localUdpPort = intValue("udp_port"),
        masqueToken = value("masque_token").ifBlank { null },
        owner = value("owner"),
        agentName = value("agent_name"),
        capability = value("capability"),
        dnn = value("dnn"),
        groupName = value("group_name"),
        message = value("message"),
    )

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
        setValue("local_vlan_ip", intent.getStringExtra("local_vlan_ip")
            ?: preferences.getString("local_vlan_ip", ""))
        setValue("tcp_port", intentInt("tcp_port", preferences.getInt("tcp_port", 4001)))
        setValue("udp_port", intentInt("udp_port", preferences.getInt("udp_port", 28443)))
        setValue("owner", intent.getStringExtra("owner")
            ?: preferences.getString("owner", "android-test-owner-${selectedRole.name.lowercase()}"))
        setValue("agent_name", intent.getStringExtra("agent_name")
            ?: preferences.getString("agent_name", "Agent-${selectedRole.name}"))
        setValue("capability", intent.getStringExtra("capability")
            ?: preferences.getString("capability", "text"))
        setValue("dnn", intent.getStringExtra("dnn") ?: preferences.getString("dnn", "internet"))
        setValue("group_name", intent.getStringExtra("group_name")
            ?: preferences.getString("group_name", "android-ab-test-group"))
        setValue("message", intent.getStringExtra("message")
            ?: preferences.getString("message", "hello Agent B from Android A"))
        setValue("masque_token", intent.getStringExtra("masque_token") ?: "")
    }

    private fun persistFormValues(config: TestConfig) {
        getSharedPreferences(PREFERENCES, MODE_PRIVATE).edit()
            .putString("role", config.role.name)
            .putString("server_ip", config.serverIp)
            .putInt("runtime_port", config.runtimePort)
            .putInt("masque_port", config.masquePort)
            .putString("masque_path", config.masquePath)
            .putString("local_vlan_ip", config.localVlanIp)
            .putInt("tcp_port", config.localTcpPort)
            .putInt("udp_port", config.localUdpPort)
            .putString("owner", config.owner)
            .putString("agent_name", config.agentName)
            .putString("capability", config.capability)
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
        runnerJob?.cancel()
        runner?.close()
        runBlocking(Dispatchers.IO) { runCatching { sdk?.close() } }
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
        val INK_LINE = Color.rgb(43, 62, 74)
        val INK_MUTED = Color.rgb(158, 176, 187)
        val LINK = Color.rgb(67, 210, 202)
        val LOG_TEXT = Color.rgb(191, 207, 216)
        val SUCCESS = Color.rgb(88, 217, 146)
        val WARNING = Color.rgb(246, 184, 80)
        val ERROR = Color.rgb(255, 113, 113)
    }

    private companion object {
        const val VPN_PERMISSION_REQUEST = 1001
        const val PREFERENCES = "agent-link-lab"
        const val MAX_LOG_LINES = 300
    }
}
