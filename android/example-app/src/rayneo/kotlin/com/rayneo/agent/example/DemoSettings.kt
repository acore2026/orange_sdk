package com.rayneo.agent.example

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.rayneo.demo.remote.DemoRemote

internal object DemoSettings {
    private fun prefs(context: Context) = context.getSharedPreferences("demo_connection", Context.MODE_PRIVATE)
    fun coreHost(context: Context): String = prefs(context).getString("core_host", RayNeoX3ProDeployment.SERVER_IP)!!
    fun sandboxHost(context: Context): String = prefs(context).getString("sandbox_host", RayNeoX3ProDeployment.SANDBOX_HOST)!!
    fun saveSandbox(context: Context, host: String) {
        check(prefs(context).edit().putString("sandbox_host", DemoRemote.coreHost(host)).commit()) { "Could not persist sandbox host" }
    }
    fun saveCore(context: Context, host: String) {
        check(prefs(context).edit().putString("core_host", DemoRemote.coreHost(host)).commit()) { "Could not persist core host" }
    }

}

/** Shell-only entrypoint; the app manifest requires android.permission.DUMP. */
class DemoConfigReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        try {
            intent.getStringExtra("core_host")?.let { DemoSettings.saveCore(context, it) }
            intent.getStringExtra("sandbox_host")?.let { DemoSettings.saveSandbox(context, it) }
            resultCode = 0
            resultData = "OK core_host=${DemoSettings.coreHost(context)} sandbox_host=${DemoSettings.sandboxHost(context)} restart_required=true"
        } catch (error: Exception) {
            resultCode = 1
            resultData = "ERROR ${error.message}"
        }
    }
}
