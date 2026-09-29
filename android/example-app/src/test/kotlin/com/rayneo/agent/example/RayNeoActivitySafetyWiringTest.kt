package com.rayneo.agent.example

import java.io.File
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RayNeoActivitySafetyWiringTest {
    @Test
    fun rayneoLauncherStartsConnectionAndKeepsVpnConsentAsFallback() {
        val source = rayneoActivitySource()

        assertTrue(source.contains("window.decorView.post { beginConnection() }"))
        assertTrue(source.contains("vpnPermissionLauncher.launch(permissionIntent)"))
        assertFalse(source.contains("等待用户显式启用 Agent 网络"))
        assertFalse(source.contains("startActivityForResult(permissionIntent"))
    }

    @Test
    fun rayneoConnectAndDestroyPathsNeverBlockTheMainThread() {
        val source = rayneoActivitySource()

        assertTrue(source.contains("lifecycleScope.launch(Dispatchers.IO)"))
        assertTrue(source.contains("cleanupScope.launch"))
        assertTrue(source.contains("withContext(Dispatchers.IO)"))
        assertTrue(source.contains("activeRunner.resetAgent()"))
        assertTrue(source.contains("activeRunner.stopComputingSession()"))
        assertFalse(source.contains("runBlocking"))
    }

    @Test
    fun windowsInstallerPreAuthorizesVpnByDefault() {
        val script = File("../install-rayneo-windows.ps1").readText()

        assertTrue(script.contains("[switch]\$RequireVpnConsent"))
        assertTrue(script.contains("ACTIVATE_VPN allow"))
        assertTrue(script.contains("if (-not \$RequireVpnConsent)"))
        assertTrue(script.contains("ACTIVATE_VPN:\\s*allow"))
    }

    @Test
    fun rayneoHudUsesOneVoiceFocusWithFullScreenVideoAndTwoPrompts() {
        val activity = rayneoActivitySource()
        val layout = File("src/rayneo/res/layout/activity_rayneo_main.xml").readText()

        assertTrue(layout.contains("@+id/video_preview_panel"))
        assertTrue(layout.contains("@+id/status_title"))
        assertTrue(layout.contains("@+id/patrol_hud"))
        assertTrue(layout.contains("@+id/patrol_command_action"))
        assertTrue(layout.contains("@+id/voice_result"))
        assertTrue(activity.contains("focusHolder.currentFocus(patrolCommandAction)"))
        assertTrue(activity.contains("VoiceMode.CONFIRMATION"))
        assertTrue(activity.contains("parsePatrolConfirmation(spoken)"))
        assertFalse(activity.contains("focusHolder.currentFocus(primaryAction)"))
    }

    private fun rayneoActivitySource(): String = File(
        "src/rayneo/kotlin/com/rayneo/agent/example/RayNeoMainActivity.kt",
    ).readText()
}
