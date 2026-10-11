package com.rayneo.demo.controller

import android.app.Activity
import android.app.AlertDialog
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
    private lateinit var body: LinearLayout
    private lateinit var endpoint: EditText
    private lateinit var pin: EditText
    private lateinit var area: EditText
    private lateinit var core: EditText
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
            main.postDelayed(this, 2500)
        }
    }
    private fun tr(zh: String, en: String) = if (english) en else zh
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        english = prefs.getBoolean("english", false)
        buildUi()
    }
    private fun buildUi() {
        controls.clear()
        val scroll = ScrollView(this).apply { setBackgroundColor(Color.rgb(7,19,31)) }
        body = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(22),dp(26),dp(22),dp(28)) }
        scroll.addView(body)
        setContentView(scroll)
        label("PATROL  /  REMOTE", 13, "#55E4E8")
        label(tr("巡检演示控制台", "Patrol Demo Console"), 27)
        label(tr("嘈杂现场 · 一键接替语音", "Preset commands for noisy venues"), 14, "#A4BCCC")
        button(tr("English / 中文", "中文 / English")) {
            saveInputs(); english = !english; prefs.edit().putBoolean("english",english).apply(); buildUi()
        }
        section(tr("连接眼镜", "Connect glasses"))
        endpoint = input(tr("眼镜 IP:8791", "Glasses IP:8791"), prefs.getString("endpoint", "")!!)
        pin = input(tr("8 位配对码（由本机命令读取）", "8-digit pairing code (from setup command)"), prefs.getString("pin", "")!!).apply {
            inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD
        }
        button(tr("连接 / 刷新状态", "Connect / Refresh")) { send("STATUS") }
        status = label(tr("尚未连接", "Not connected"), 15, "#55E4E8")
        result = label(tr("准备就绪后可使用下方按钮", "Connect to enable controls"), 14, "#B5C7D4")
        section(tr("巡逻演示", "Patrol workflow"))
        area = input(tr("巡逻区域", "Patrol area"), prefs.getString("area", "A区域")!!)
        action("PATROL", tr("01  发现机器狗", "01  Find robot dog"))
        action("CONFIRM", tr("02  确认派遣", "02  Confirm dispatch"), true)
        action("CANCEL", tr("取消待确认指令", "Cancel pending command"))
        action("DETER", tr("威吓歹徒 · happy", "Deter suspect · happy"), true)
        action("EXPEL", tr("发起驱逐（待确认）", "Request expel (confirmation required)"))
        action("CONFIRM_EXPEL", tr("确认驱逐", "Confirm expel"), true)
        section(tr("语音与视频", "Voice & video"))
        action("VOICE_START", tr("开始眼镜录音", "Start glasses recording"))
        action("VOICE_STOP", tr("结束录音并识别", "Stop & recognize"))
        action("RTC_RECONNECT", tr("仅重连视频", "Reconnect video only"))
        action("VIDEO_HIDE", tr("隐藏视频", "Hide video"))
        action("VIDEO_SHOW", tr("显示视频", "Show video"))
        section(tr("远端核心网", "Remote core network"))
        core = input(tr("核心网 IP 或域名", "Core IP or hostname"), prefs.getString("core", "")!!)
        button(tr("保存至眼镜（下次启动生效）", "Save to glasses (applies on restart)")) {
            runCatching { DemoRemote.coreHost(core.text.toString()) }.onSuccess { send("CONFIG_CORE") }
                .onFailure { result.text = it.message }
        }
        label(tr("手机通过眼镜复用核心网配置。收到“已接收”后，请等待执行结果；通信超时不会自动重发动作。", "The phone uses the glasses' core configuration. Accepted means queued; wait for completion. Actions are never retried automatically."), 12, "#8297AB")
        updateControls()
    }
    private fun label(text: String, size: Int, color: String = "#E9FCFF"): TextView = TextView(this).apply {
        this.text = text; textSize = size.toFloat(); setTextColor(Color.parseColor(color)); setPadding(0,dp(5),0,dp(5))
        if (size >= 20) typeface = Typeface.DEFAULT_BOLD
        body.addView(this)
    }
    private fun section(title: String) { label(title,18,"#55E4E8").setPadding(0,dp(25),0,dp(8)) }
    private fun input(hint: String, value: String) = EditText(this).apply {
        this.hint=hint; setText(value); setTextColor(Color.WHITE); setHintTextColor(Color.rgb(125,148,166))
        textSize=15f; isSingleLine=true; inputType=InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
        body.addView(this,LinearLayout.LayoutParams(-1,dp(52)))
    }
    private fun button(title: String, run: () -> Unit): Button = Button(this).apply {
        text=title; isAllCaps=false; textSize=15f; setTextColor(Color.parseColor("#E9FCFF"))
        background=GradientDrawable().apply { setColor(Color.rgb(17,42,57)); cornerRadius=dp(10).toFloat(); setStroke(dp(1),Color.rgb(39,97,113)) }
        body.addView(this,LinearLayout.LayoutParams(-1,dp(52)).apply { topMargin=dp(9) })
        setOnClickListener { run() }
    }
    private fun action(command:String, title:String, confirm:Boolean=false) {
        val view=button(title) {
            if (confirm) AlertDialog.Builder(this).setTitle(title)
                .setMessage(tr("此操作会向机器狗下发真实任务或动作，确认继续？", "This sends a real task or action to the robot dog. Continue?"))
                .setNegativeButton(tr("取消","Cancel"),null).setPositiveButton(tr("确认","Confirm")) { _,_->send(command) }.show()
            else send(command)
        }
        controls += command to view
    }
    private fun saveInputs() {
        if (!::core.isInitialized) return
        prefs.edit().putString("endpoint",endpoint.text.toString().trim()).putString("pin",pin.text.toString().trim())
            .putString("area",area.text.toString()).putString("core",core.text.toString()).apply()
    }
    private fun send(command:String) {
        if (inFlight) return
        val host=endpoint.text.toString().trim()
        val code=pin.text.toString().trim()
        val request=JSONObject().put("command",command).put("request_id",UUID.randomUUID().toString())
            .put("language",if(english) "en" else "zh")
        if(command=="PATROL") request.put("area",area.text.toString())
        if(command=="CONFIG_CORE") request.put("host",core.text.toString())
        val wire=try { DemoRemote.endpoint(host); DemoRemote.envelope(code,request.toString()) }
            catch(e:Exception) { result.text=e.message; return }
        saveInputs(); inFlight=true; updateControls()
        io.execute {
            val response=runCatching { DemoRemote.exchange(host,wire) }
            main.post {
                if(isDestroyed) return@post
                inFlight=false
                if(host!=endpoint.text.toString().trim() || code!=pin.text.toString().trim()) { connected=false; updateControls(); return@post }
                response.onSuccess { raw ->
                    if(raw.startsWith("OK {")) {
                        snapshot=JSONObject(raw.removePrefix("OK ")); connected=true; snapshotEndpoint=host
                        status.text=tr("阶段：", "Phase: ")+snapshot.optString("phase")+"\n"+
                            tr("核心网：", "Core: ")+snapshot.optString("core_host")+
                            if(snapshot.optBoolean("stalled")) tr("\n视频暂时中断", "\nVideo stalled") else ""
                        result.text=snapshot.optString("result")
                        if(core.text.isBlank()) core.setText(snapshot.optString("saved_core_host"))
                    } else {
                        result.text=raw
                        if(raw.startsWith("ERR")) { connected=false }
                        else if(command!="STATUS") main.post { send("STATUS") }
                    }
                }.onFailure { connected=false; status.text=tr("连接失败", "Connection failed"); result.text=tr("操作结果未知，请刷新确认，勿重复点击：", "Outcome unknown; refresh before retrying: ")+it.message }
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
