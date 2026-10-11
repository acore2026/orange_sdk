package com.rayneo.agent.example

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.rayneo.demo.remote.DemoRemote
import java.security.SecureRandom

internal object DemoSettings {
    private fun prefs(context: Context) = context.getSharedPreferences("demo_connection", Context.MODE_PRIVATE)
    fun coreHost(context: Context): String = prefs(context).getString("core_host", RayNeoX3ProDeployment.SERVER_IP)!!
    fun saveCore(context: Context, host: String) {
        check(prefs(context).edit().putString("core_host", DemoRemote.coreHost(host)).commit()) { "Could not persist core host" }
    }
    @Synchronized fun pin(context: Context): String {
        prefs(context).getString("pairing_pin", null)?.let { return it }
        val pin = (10_000_000 + SecureRandom().nextInt(90_000_000)).toString()
        check(prefs(context).edit().putString("pairing_pin", pin).commit())
        return pin
    }
}

/** Shell-only entrypoint; the app manifest requires android.permission.DUMP. */
class DemoConfigReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        try {
            intent.getStringExtra("core_host")?.let { DemoSettings.saveCore(context, it) }
            resultCode = 0
            resultData = "OK core_host=${DemoSettings.coreHost(context)} pairing_pin=${DemoSettings.pin(context)} restart_required=true"
        } catch (error: Exception) {
            resultCode = 1
            resultData = "ERROR ${error.message}"
        }
    }
}
