package com.rayneo.agent.example

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

class SdkFeatureCoverageWiringTest {
    @Test
    fun runnerCoversEveryInteractiveSdkControlSurface() {
        val runner = File(
            "src/main/kotlin/com/rayneo/agent/example/AgentTestRunner.kt",
        ).readText()

        listOf(
            "sdk.updateCapabilities(",
            "sdk.getGroupSnapshot(",
            "sdk.queryComputingSession(",
            "sdk.cancelComputingSession(",
            "sdk.releaseComputingSession(",
            "sdk.awaitComputingSessionClosed(",
            "upload.pause()",
            "upload.resume()",
            "sdk.updateRecognitionTarget(",
            "sdk.getRecognitionTarget(",
            "sdk.createControlAction(",
            "sdk.getControlAction(",
        ).forEach { call ->
            assertTrue("Missing example-app coverage for $call", runner.contains(call))
        }
    }

    @Test
    fun genericUiExposesRoleAndStateAwareFeatureActions() {
        val activity = File(
            "src/main/kotlin/com/rayneo/agent/example/MainActivity.kt",
        ).readText()

        listOf(
            "读取群组快照",
            "新增能力",
            "删除能力",
            "查询 QUERY",
            "取消 CANCEL",
            "释放 RELEASE",
            "暂停摄像头上传",
            "写入目标",
            "读取目标",
            "创建动作",
            "查询动作",
        ).forEach { label ->
            assertTrue("Missing feature action: $label", activity.contains(label))
        }
        assertTrue(activity.contains("state?.processedVideoState != null"))
        assertTrue(activity.contains("state?.producerVideoReady == true"))
        assertTrue(activity.contains("activeConfig?.role == TestRole.A"))
    }

    @Test
    fun pendingCreateIsCancelledBeforeIdentityRemoval() {
        val runner = File(
            "src/main/kotlin/com/rayneo/agent/example/AgentTestRunner.kt",
        ).readText()
        val pendingCancel = runner.indexOf("CREATE 尚未返回 session_id")
        val deregistration = runner.indexOf("suspend fun deregisterAgentForStop")

        assertTrue(pendingCancel >= 0)
        assertTrue(deregistration > pendingCancel)
        assertTrue(runner.contains("targetRequestId = createRequestId"))
    }
}
