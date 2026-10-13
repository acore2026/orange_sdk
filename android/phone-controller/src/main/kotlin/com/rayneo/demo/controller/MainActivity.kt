package com.rayneo.demo.controller

import android.app.Activity
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.View
import android.view.WindowManager
import android.widget.*
import com.rayneo.demo.remote.DemoRemote
import org.json.JSONObject
import java.util.UUID
import java.util.concurrent.Executors

/** A phone remote for the real glasses workflow; it owns no robot/core identity. */
class MainActivity : Activity() {
    private val prefs by lazy { getSharedPreferences("controller", MODE_PRIVATE) }
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private var settingsPage = false
    private lateinit var body: LinearLayout
    private lateinit var endpoint: EditText
    private lateinit var dogEndpoint: EditText
    private lateinit var dogResult: TextView
    private lateinit var glassesConnection: TextView
    private lateinit var dogConnection: TextView
    private lateinit var glassesIdentityResult: TextView
    private lateinit var glassesCoreResult: TextView
    private lateinit var dogCoreResult: TextView
    private lateinit var sandboxResult: TextView
    private var glassesIdentityId = ""
    private var dogConnectedHost = ""
    private var dogAdminBusy = false
    private var dogRequestId = ""
    private var dogRequestHost = ""
    private var dogInFlight = false
    private var dogPolling = false
    private var dogPollDeadline = 0L
    private lateinit var area: EditText
    private lateinit var core: EditText
    private lateinit var sandbox: EditText
    private lateinit var status: TextView
    private lateinit var result: TextView
    private val controls = mutableListOf<Pair<String, Button>>()
    private var english = false
    private var connected = false
    private var foreground = false
    private var inFlight = false
    private var snapshot = JSONObject()
    private var snapshotEndpoint = ""
    private val poll = object : Runnable {
        override fun run() {
            if (!foreground) return
            if (connected && !inFlight) send("STATUS")
            if (dogPolling && !dogInFlight) {
                if (android.os.SystemClock.elapsedRealtime() < dogPollDeadline) exchangeDog(false)
                else {
                    dogPolling = false
                    dogResult.text = tr("等待结果超时，请查询处理结果", "Result wait timed out; refresh operation result")
                }
            }
            main.postDelayed(this, 2500)
        }
    }
    private fun tr(zh: String, en: String) = if (english) en else zh
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        english = prefs.getBoolean("english", false)
        settingsPage = prefs.getString("endpoint", "").isNullOrBlank()
        dogRequestId = prefs.getString("dog_request_id", "").orEmpty()
        dogRequestHost = prefs.getString("dog_request_host", "").orEmpty()
        prefs.edit().remove("pin").remove("dog_pin").remove("dog_request_pin").apply()
        buildUi()
    }
    private fun buildUi() {
        controls.clear()
        val screen = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.rgb(7, 19, 31))
        }
        setContentView(screen)
        val pageHost = FrameLayout(this)
        screen.addView(pageHost, LinearLayout.LayoutParams(-1, 0, 1f))
        fun page(): Pair<ScrollView, LinearLayout> {
            val content = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(14), dp(10), dp(14), dp(10))
            }
            val scroll = ScrollView(this).apply { isFillViewport = true; addView(content) }
            pageHost.addView(scroll, FrameLayout.LayoutParams(-1, -1))
            return scroll to content
        }
        val (settingsScroll, settings) = page()
        val (workflowScroll, workflow) = page()
        body = settings
        label(tr("设备配置", "Device configuration"), 22)
        section(tr("设备连接", "Device connections"))
        endpoint = input(tr("眼镜 IP（默认 8791）", "Glasses IP (default 8791)"), prefs.getString("endpoint", "")!!)
        button(tr("连接眼镜", "Connect glasses")) { send("STATUS") }
        glassesConnection = label(tr("眼镜未连接", "Glasses not connected"), 13, "#B5C7D4")
        dogEndpoint = input(tr("机器狗 IP（默认 8081）", "Dog IP (default 8081)"), prefs.getString("dog_endpoint", "")!!)
        button(tr("连接机器狗", "Connect dog")) { dogAdmin(false) }
        dogConnection = label(tr("机器狗未连接", "Dog not connected"), 13, "#B5C7D4")

        section(tr("设备操作", "Device operations"))
        row {
            button(tr("去注册眼镜身份", "Deregister glasses")) { send("DEREGISTER_GLASSES") }
            button(tr("去注册机器狗身份", "Deregister dog")) { deregisterDog() }
        }
        glassesIdentityResult = label(tr("眼镜身份：尚无操作", "Glasses identity: no operation"), 13, "#B5C7D4")
        dogResult = label(tr("机器狗身份：尚无操作", "Dog identity: no operation"), 13, "#B5C7D4")
        button(tr("查询机器狗身份处理结果", "Refresh dog identity result")) {
            if (dogRequestId.isBlank()) dogResult.text = tr("尚无身份操作", "No identity operation")
            else exchangeDog(false)
        }
        core = input(tr("新的核心网 IP 或域名", "New core IP or hostname"), prefs.getString("core", "")!!)
        row {
            button(tr("更新眼镜核心网", "Update glasses core")) { send("CONFIG_CORE") }
            button(tr("更新机器狗核心网", "Update dog core")) { dogAdmin(true) }
        }
        glassesCoreResult = label(tr("眼镜核心网：尚无操作", "Glasses core: no operation"), 13, "#B5C7D4")
        dogCoreResult = label(tr("机器狗核心网：尚无操作", "Dog core: no operation"), 13, "#B5C7D4")
        label(tr("核心网地址保存成功后，需重启对应设备的 Agent / App 生效。", "Restart the device Agent / app after saving a core address."), 12, "#8297AB")
        sandbox = input(tr("Sandbox IP 或域名", "Sandbox IP or hostname"), prefs.getString("sandbox", "192.168.2.150")!!)
        button(tr("更新眼镜 Sandbox", "Update glasses Sandbox")) { send("CONFIG_SANDBOX") }
        sandboxResult = label(tr("Sandbox：尚无操作", "Sandbox: no operation"), 13, "#B5C7D4")
        button("English / 中文") {
            saveInputs(); english = !english; prefs.edit().putBoolean("english", english).apply(); buildUi()
        }

        body = workflow
        label(tr("遥控", "Remote control"), 22)
        area = input(tr("巡逻区域", "Patrol area"), prefs.getString("area", "A区域")!!)
        section(tr("巡逻", "Patrol"))
        row {
            action("PATROL", tr("发现机器狗", "Find dog"))
            action("CONFIRM", tr("确认派遣", "Dispatch"))
            action("CANCEL", tr("取消", "Cancel"))
        }
        section(tr("机器狗动作", "Robot actions"))
        row {
            action("FORWARD", tr("前进", "Forward"))
            action("BACK", tr("后退", "Back"))
            action("STOP", tr("停止", "Stop"))
        }
        row {
            action("LEFT", tr("左转", "Left"))
            action("RIGHT", tr("右转", "Right"))
            action("DETER", tr("威吓 · Content", "Deter · Content"))
        }
        row {
            action("EXPEL", tr("发起驱逐", "Request expel"))
            action("CONFIRM_EXPEL", tr("确认驱逐", "Confirm expel"))
        }
        section(tr("语音与视频", "Voice & video"))
        row {
            action("VOICE_START", tr("开始录音", "Record"))
            action("VOICE_STOP", tr("结束识别", "Recognize"))
            action("RTC_RECONNECT", tr("重连视频", "Reconnect"))
        }
        row {
            action("VIDEO_SHOW", tr("显示视频", "Show video"))
            action("VIDEO_HIDE", tr("隐藏视频", "Hide video"))
            button(tr("刷新状态", "Refresh")) { send("STATUS") }
        }
        // Fixed footer stays outside both independently scrolling pages.
        body = screen
        status = label(tr("尚未连接", "Not connected"), 14, "#55E4E8").apply { maxLines = 2 }
        result = label(tr("请在配置页填写设备地址", "Set device addresses on Configuration"), 13, "#B5C7D4").apply { maxLines = 3 }
        val bottomNavigation = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(dp(12), dp(8), dp(12), dp(8))
            setBackgroundColor(Color.rgb(10, 28, 42))
        }
        screen.addView(bottomNavigation, LinearLayout.LayoutParams(-1, dp(56)))
        fun tab(text: String) = Button(this).apply {
            this.text = text; isAllCaps = false
            bottomNavigation.addView(this, LinearLayout.LayoutParams(0, -1, 1f).apply { marginStart = dp(4); marginEnd = dp(4) })
        }
        val configurationTab = tab(tr("配置", "Configuration"))
        val navigationTab = tab(tr("遥控", "Remote"))
        fun selectPage(configuration: Boolean) {
            settingsPage = configuration
            settingsScroll.visibility = if (configuration) View.VISIBLE else View.GONE
            workflowScroll.visibility = if (configuration) View.GONE else View.VISIBLE
            listOf(configurationTab to configuration, navigationTab to !configuration).forEach { (tab, selected) ->
                tab.isSelected = selected
                tab.setTextColor(Color.parseColor(if (selected) "#55E4E8" else "#A4BCCC"))
                tab.background = GradientDrawable().apply {
                    setColor(Color.parseColor(if (selected) "#143A48" else "#0A1C2A"))
                    cornerRadius = dp(10).toFloat()
                    setStroke(dp(1), Color.parseColor(if (selected) "#55E4E8" else "#25495A"))
                }
            }
        }
        configurationTab.setOnClickListener { saveInputs(); selectPage(true) }
        navigationTab.setOnClickListener { saveInputs(); selectPage(false) }
        selectPage(settingsPage)
        updateControls()
    }
    private fun label(text: String, size: Int, color: String = "#E9FCFF"): TextView = TextView(this).apply {
        this.text = text; textSize = size.toFloat(); setTextColor(Color.parseColor(color)); setPadding(0,dp(5),0,dp(5))
        if (size >= 20) typeface = Typeface.DEFAULT_BOLD
        body.addView(this)
    }
    private fun section(title: String) { label(title,15,"#55E4E8").setPadding(0,dp(8),0,dp(3)) }
    private fun input(hint: String, value: String) = EditText(this).apply {
        this.hint=hint; setText(value); setTextColor(Color.WHITE); setHintTextColor(Color.rgb(125,148,166))
        textSize=15f; isSingleLine=true; inputType=InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
        body.addView(this,LinearLayout.LayoutParams(-1,dp(52)))
    }
    private fun button(title: String, run: () -> Unit): Button = Button(this).apply {
        text=title; isAllCaps=false; textSize=13f; setTextColor(Color.parseColor("#E9FCFF"))
        background=GradientDrawable().apply { setColor(Color.rgb(17,42,57)); cornerRadius=dp(10).toFloat(); setStroke(dp(1),Color.rgb(39,97,113)) }
        minimumWidth=0; minWidth=0; minimumHeight=0; minHeight=0
        setPadding(dp(4), 0, dp(4), 0)
        val params = if (body.orientation == LinearLayout.HORIZONTAL)
            LinearLayout.LayoutParams(0, dp(46), 1f).apply { marginEnd=dp(4); topMargin=dp(4) }
        else LinearLayout.LayoutParams(-1, dp(46)).apply { topMargin=dp(4) }
        body.addView(this, params)
        setOnClickListener { run() }
    }
    private fun row(block: () -> Unit) {
        val parent = body
        body = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        parent.addView(body, LinearLayout.LayoutParams(-1, -2))
        block()
        body = parent
    }
    private fun action(command: String, title: String) {
        controls += command to button(title) { send(command) }
    }
    private fun dogAdmin(configure: Boolean) {
        if (dogAdminBusy) return
        val host = dogEndpoint.text.toString().trim()
        val coreHost = if (configure) runCatching { DemoRemote.coreHost(core.text.toString()) }
            .getOrElse { dogCoreResult.text = it.message; return } else null
        if (configure && dogConnectedHost != host) {
            dogCoreResult.text = tr("请先连接此机器狗", "Connect this dog first"); return
        }
        saveInputs()
        dogAdminBusy = true
        val target = if (configure) dogCoreResult else dogConnection
        target.text = tr("处理中…", "Processing…")
        io.execute {
            val response = runCatching { JSONObject(DemoRemote.dogAdmin(host, coreHost)) }
            main.post {
                if (isDestroyed) return@post
                dogAdminBusy = false
                val currentTarget = if (configure) dogCoreResult else dogConnection
                response.onSuccess {
                    if (configure) {
                        currentTarget.text = tr("机器狗核心网已保存：", "Dog core saved: ") + it.optString("saved_core_host") +
                            tr(" · 重启后生效", " · applies on restart")
                    } else if (host == dogEndpoint.text.toString().trim()) {
                        dogConnectedHost = host
                        currentTarget.text = tr("机器狗连接成功：", "Dog connected: ") + host +
                            tr(" · 当前核心网 ", " · active core ") + it.optString("core_host")
                    }
                }.onFailure {
                    if (!configure) dogConnectedHost = ""
                    currentTarget.text = tr("处理失败：", "Failed: ") + it.message
                }
            }
        }
    }
    private fun saveInputs() {
        if (!::core.isInitialized) return
        prefs.edit().putString("endpoint",endpoint.text.toString().trim())
            .putString("area",area.text.toString()).putString("core",core.text.toString())
            .putString("sandbox", sandbox.text.toString().trim())
            .putString("dog_endpoint", dogEndpoint.text.toString().trim())
            .apply()
    }
    private fun deregisterDog() {
        if (dogInFlight || dogPolling) return
        val host = dogEndpoint.text.toString().trim()
        try {
            DemoRemote.endpoint(host)
        } catch (e: Exception) { dogResult.text = e.message; return }
        if (dogConnectedHost != host) { dogResult.text = tr("请先连接此机器狗", "Connect this dog first"); return }
        saveInputs()
        dogResult.text = tr("正在提交机器狗身份去注册…", "Submitting dog deregistration…")
        dogRequestHost = host
        dogRequestId = UUID.randomUUID().toString()
        prefs.edit().putString("dog_request_id", dogRequestId).putString("dog_request_host", dogRequestHost)
            .apply()
        dogPolling = true
        dogPollDeadline = android.os.SystemClock.elapsedRealtime() + 200_000
        exchangeDog(true)
    }
    private fun exchangeDog(submit: Boolean) {
        if (dogInFlight || dogRequestId.isBlank()) return
        dogInFlight = true
        io.execute {
            val response = runCatching { JSONObject(DemoRemote.dogIdentity(dogRequestHost, dogRequestId, submit)) }
            main.post {
                if (isDestroyed) return@post
                dogInFlight = false
                response.onSuccess { data ->
                    val state = data.optString("state")
                    if (state == "RUNNING" && !dogPolling) dogPollDeadline = android.os.SystemClock.elapsedRealtime() + 200_000
                    dogPolling = state == "RUNNING"
                    dogResult.text = identityMessage(state, data.optString("message"))
                }.onFailure {
                    dogPolling = false
                    dogResult.text = tr("结果未知，请查询处理结果：", "Outcome unknown; refresh operation result: ") + it.message
                }
            }
        }
    }
    private fun identityMessage(state: String, detail: String): String = when (state) {
        "RUNNING" -> tr("正在处理身份…", "Processing identity…")
        "NO_IDENTITY" -> tr("去注册成功，设备当前无数字身份", "Deregistered; device has no digital identity")
        "REREGISTERED" -> tr("去注册失败，已清除本地身份并重新注册成功", "Deregistration failed; local identity cleared and re-registration succeeded")
        "FAILED" -> tr("身份恢复失败：", "Identity recovery failed: ") + detail
        else -> detail
    }
    private fun operationResult(command: String): TextView = when (command) {
        "DEREGISTER_GLASSES" -> glassesIdentityResult
        "CONFIG_CORE" -> glassesCoreResult
        "CONFIG_SANDBOX" -> sandboxResult
        else -> result
    }
    private fun send(command:String) {
        if (inFlight) return
        val host=endpoint.text.toString().trim()
        val request=JSONObject().put("command",command).put("request_id",UUID.randomUUID().toString())
            .put("language",if(english) "en" else "zh")
        if(command=="PATROL") request.put("area",area.text.toString())
        if(command=="CONFIG_CORE") request.put("host",core.text.toString().trim())
        if(command=="CONFIG_SANDBOX") request.put("host",sandbox.text.toString())
        val wire=try { DemoRemote.endpoint(host); DemoRemote.envelope(request.toString()) }
            catch(e:Exception) { operationResult(command).text=e.message; return }
        if (command in setOf("CONFIG_CORE", "CONFIG_SANDBOX")) {
            try { DemoRemote.coreHost(request.getString("host")) }
            catch (e: Exception) { operationResult(command).text=e.message; return }
        }
        if (command != "STATUS" && (!connected || snapshotEndpoint != host)) {
            operationResult(command).text = tr("请先连接眼镜", "Connect glasses first"); return
        }
        if (command == "DEREGISTER_GLASSES") glassesIdentityId = request.getString("request_id")
        if (command != "STATUS") operationResult(command).text = tr("处理中…", "Processing…")
        else glassesConnection.text = tr("正在连接眼镜…", "Connecting glasses…")
        saveInputs(); inFlight=true; updateControls()
        io.execute {
            val response=runCatching { DemoRemote.exchange(host,wire) }
            main.post {
                if(isDestroyed) return@post
                inFlight=false
                if(host!=endpoint.text.toString().trim()) { connected=false; updateControls(); return@post }
                response.onSuccess { raw ->
                    if(raw.startsWith("OK {")) {
                        snapshot=JSONObject(raw.removePrefix("OK ")); connected=true; snapshotEndpoint=host
                        glassesConnection.text=tr("眼镜连接成功：", "Glasses connected: ")+host
                        if (glassesIdentityId.isNotBlank() && snapshot.optString("identity_request_id") == glassesIdentityId)
                            glassesIdentityResult.text=identityMessage(snapshot.optString("identity_state"), snapshot.optString("identity_result"))
                        status.text=tr("阶段：", "Phase: ")+snapshot.optString("phase")+"\n"+
                            tr("核心网：", "Core: ")+snapshot.optString("core_host")+
                            if(snapshot.optBoolean("stalled")) tr("\n视频暂时中断", "\nVideo stalled") else ""
                        result.text = snapshot.optString("result")
                        if(core.text.isBlank()) core.setText(snapshot.optString("saved_core_host"))
                        if(sandbox.text.isBlank()) sandbox.setText(snapshot.optString("saved_sandbox_host"))
                    } else {
                        operationResult(command).text=when {
                            raw.startsWith("OK") && command=="CONFIG_CORE" -> tr("眼镜核心网已保存：", "Glasses core saved: ")+request.optString("host")+tr(" · 重启生效", " · restart to apply")
                            raw.startsWith("OK") && command=="CONFIG_SANDBOX" -> tr("Sandbox 已保存，重启眼镜生效", "Sandbox saved; restart glasses")
                            raw.startsWith("OK") && command=="DEREGISTER_GLASSES" -> tr("已接收，正在去注册…", "Accepted; deregistering…")
                            else -> raw
                        }
                        if(raw.startsWith("ERR")) { connected=false }
                        else if(command!="STATUS") main.post { send("STATUS") }
                    }
                }.onFailure {
                    connected=false
                    glassesConnection.text=tr("眼镜连接失败", "Glasses connection failed")
                    status.text=tr("连接失败", "Connection failed")
                    operationResult(command).text=tr("结果未知，请刷新查询：", "Outcome unknown; refresh: ")+it.message
                }
                updateControls()
            }
        }
    }
    private fun updateControls() {
        val ready=connected && !inFlight && snapshotEndpoint==endpoint.text.toString().trim()
        val idle=ready && !snapshot.optBoolean("busy") && !snapshot.optBoolean("recording")
        val phase=snapshot.optString("phase")
        controls.forEach { (command,view) ->
            view.isEnabled=when(command) {
                "DEREGISTER_GLASSES" -> !inFlight
                "PATROL" -> idle && snapshot.optBoolean("ready") && !snapshot.optBoolean("connected") && phase in setOf("IDLE","LISTENING","COMPLETED","ABORTED")
                "CONFIRM" -> idle && phase=="DISPATCHING"
                "CANCEL" -> idle && (phase=="DISPATCHING" || snapshot.optBoolean("pending_expel"))
                "CONFIRM_EXPEL" -> idle && snapshot.optBoolean("pending_expel")
                "VOICE_START" -> idle && snapshot.optBoolean("ready")
                "VOICE_STOP" -> ready && snapshot.optBoolean("recording")
                "VIDEO_SHOW","VIDEO_HIDE" -> idle
                else -> idle && snapshot.optBoolean("connected")
            }
            view.alpha=if(view.isEnabled) 1f else 0.4f
        }
    }
    private fun dp(value:Int)=(value*resources.displayMetrics.density).toInt()
    override fun onResume() { super.onResume(); foreground=true; main.post(poll) }
    override fun onPause() { foreground=false; main.removeCallbacks(poll); saveInputs(); super.onPause() }
    override fun onDestroy() { main.removeCallbacksAndMessages(null); io.shutdownNow(); super.onDestroy() }
}
