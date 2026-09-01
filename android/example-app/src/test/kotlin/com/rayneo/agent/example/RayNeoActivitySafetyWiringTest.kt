package com.rayneo.agent.example

import java.io.File
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RayNeoActivitySafetyWiringTest {
    @Test
    fun rayneoLauncherRequiresExplicitUserActionBeforeVpnConsent() {
        val source = rayneoActivitySource()

        assertTrue(source.contains("等待用户显式启用 Agent 网络"))
        assertTrue(source.contains("vpnPermissionLauncher.launch(permissionIntent)"))
        assertFalse(source.contains("root.post(::beginConnection)"))
        assertFalse(source.contains("startActivityForResult(permissionIntent"))
    }

    @Test
    fun rayneoConnectAndDestroyPathsNeverBlockTheMainThread() {
        val source = rayneoActivitySource()

        assertTrue(source.contains("lifecycleScope.launch(Dispatchers.IO)"))
        assertTrue(source.contains("cleanupScope.launch"))
        assertFalse(source.contains("runBlocking"))
    }

    @Test
    fun windowsInstallerKeepsAdbVpnAuthorizationOptional() {
        val script = File("../install-rayneo-windows.ps1").readText()

        assertTrue(script.contains("[switch]\$PreAuthorizeVpn"))
        assertTrue(script.contains("ACTIVATE_VPN allow"))
        assertTrue(script.contains("if (\$PreAuthorizeVpn)"))
    }

    private fun rayneoActivitySource(): String = File(
        "src/rayneo/kotlin/com/rayneo/agent/example/RayNeoMainActivity.kt",
    ).readText()
}
