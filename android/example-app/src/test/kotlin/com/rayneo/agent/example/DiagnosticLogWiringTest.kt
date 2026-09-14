package com.rayneo.agent.example

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

class DiagnosticLogWiringTest {
    @Test
    fun genericAppExportsDetailedDiagnostics() {
        val activity = File(
            "src/main/kotlin/com/rayneo/agent/example/MainActivity.kt",
        ).readText()
        val exporter = File(
            "src/main/kotlin/com/rayneo/agent/example/DiagnosticLogExporter.kt",
        ).readText()
        val renderer = File(
            "src/main/kotlin/com/rayneo/agent/example/TextureViewEglRenderer.kt",
        ).readText()
        val runner = File(
            "src/main/kotlin/com/rayneo/agent/example/AgentTestRunner.kt",
        ).readText()
        val manifest = File("src/main/AndroidManifest.xml").readText()

        assertTrue(activity.contains("Dump 日志"))
        assertTrue(activity.contains("DiagnosticLogExporter.createDump"))
        assertTrue(exporter.contains("compute_endpoint=<managed by SDK from C-02>"))
        assertTrue(exporter.contains("[VIDEO PREVIEW RENDERER]"))
        assertTrue(renderer.contains("renderer=TextureView/EglRenderer"))
        assertTrue(activity.contains("?.diagnosticSummary()"))
        assertTrue(renderer.contains("first_frame_displayed="))
        assertTrue(exporter.contains("[LOCAL TCP SELF PROBE]"))
        assertTrue(exporter.contains("[SDK AND APP LOGCAT]"))
        assertTrue(exporter.contains("AgentSdkLocalServer:V"))
        assertTrue(exporter.contains("/proc/self/net/tcp"))
        assertTrue(exporter.contains("MediaStore.Downloads.EXTERNAL_CONTENT_URI"))
        assertTrue(exporter.contains("Download/AgentLinkDiagnostics"))
        assertTrue(runner.contains("masque_downlink_packets_over_tun_mtu="))
        assertTrue(runner.contains("masque_downlink_read_buffer_too_small="))
        assertTrue(runner.contains("masque_uplink_datagram_too_large="))
        assertTrue(manifest.contains("androidx.core.content.FileProvider"))
    }

    @Test
    fun rayneoAppExposesFocusedDumpActionAndKeepsFullLogHistory() {
        val activity = File(
            "src/rayneo/kotlin/com/rayneo/agent/example/RayNeoMainActivity.kt",
        ).readText()
        val layout = File("src/rayneo/res/layout/activity_rayneo_main.xml").readText()

        assertTrue(layout.contains("@+id/dump_action"))
        assertTrue(activity.contains("FocusInfo(\n                    dumpAction"))
        assertTrue(activity.contains("MAX_DIAGNOSTIC_LOG_LINES"))
        assertTrue(activity.contains("DiagnosticLogExporter.share"))
    }
}
