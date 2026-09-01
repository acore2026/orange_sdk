package com.rayneo.agent.example

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

class AgentResetWiringTest {
    @Test
    fun genericAppRequiresConfirmationAndCallsSdkReset() {
        val source = File(
            "src/main/kotlin/com/rayneo/agent/example/MainActivity.kt",
        ).readText()
        val runner = File(
            "src/main/kotlin/com/rayneo/agent/example/AgentTestRunner.kt",
        ).readText()

        assertTrue(source.contains("重置到状态1"))
        assertTrue(source.contains("再次点击确认"))
        assertTrue(source.contains("不修改网侧身份"))
        assertTrue(source.contains("activeSdk.resetAgent()"))
        assertTrue(source.contains("AgentLifecycleState.NO_IDENTITY"))
        assertTrue(runner.contains("operationMutex.withLock { sdk.resetAgent() }"))
        assertTrue(runner.contains("ensureResetNotRequested()"))
    }

    @Test
    fun rayneoAppExposesAFocusedTwoStepResetAction() {
        val source = File(
            "src/rayneo/kotlin/com/rayneo/agent/example/RayNeoMainActivity.kt",
        ).readText()
        val layout = File("src/rayneo/res/layout/activity_rayneo_main.xml").readText()

        assertTrue(layout.contains("@+id/reset_action"))
        assertTrue(layout.contains("Reset 需单击两次确认"))
        assertTrue(source.contains("FocusInfo(\n                    resetAction"))
        assertTrue(source.contains("不修改网侧身份"))
        assertTrue(source.contains("activeSdk.resetAgent()"))
        assertTrue(source.contains("已回到状态1"))
    }
}
