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
            "sdk.transcribeAudio(",
            "sdk.recognizeIntent(",
            "sdk.createAudioControlAction(",
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
            "开始录音 · 仅转文字",
            "开始录音 · 转写并执行",
            "最近转写结果",
            "启动意图",
        ).forEach { label ->
            assertTrue("Missing feature action: $label", activity.contains(label))
        }
        assertTrue(activity.contains("state?.processedVideoState != null"))
        assertTrue(activity.contains("state?.producerVideoReady == true"))
        assertTrue(activity.contains("activeConfig?.role == TestRole.A"))
    }

    @Test
    fun bothAppsExposeStandaloneAsrAndBoundVoiceActions() {
        val manifest = File("src/main/AndroidManifest.xml").readText()
        val recorder = File(
            "src/main/kotlin/com/rayneo/agent/example/VoiceAudioRecorder.kt",
        ).readText()
        val rayneo = File(
            "src/rayneo/kotlin/com/rayneo/agent/example/RayNeoMainActivity.kt",
        ).readText()
        val rayneoLayout = File("src/rayneo/res/layout/activity_rayneo_main.xml").readText()

        assertTrue(manifest.contains("android.permission.RECORD_AUDIO"))
        assertTrue(recorder.contains("MediaRecorder.AudioSource.MIC"))
        assertTrue(rayneo.contains("VoiceMode.TRANSCRIBE"))
        assertTrue(rayneo.contains("VoiceMode.CONTROL_ACTION"))
        assertTrue(rayneoLayout.contains("@+id/asr_action"))
        assertTrue(rayneoLayout.contains("@+id/voice_control_action"))
        assertTrue(rayneoLayout.contains("@+id/voice_result"))
        assertTrue(rayneo.contains("sdkFeatureState?.lastTranscription"))
        assertTrue(rayneo.contains("recognizedIntent"))
    }

    @Test
    fun agentADiscoversOnlyAfterSecurityPatrolIntentIsMappedToSkill() {
        val runner = File(
            "src/main/kotlin/com/rayneo/agent/example/AgentTestRunner.kt",
        ).readText()

        val intentCall = runner.indexOf("sdk.recognizeIntent(")
        val intentGuard = runner.indexOf("result.intent == SECURITY_PATROL_INTENT")
        val discovery = runner.indexOf("sdk.discoverAgents(")
        assertTrue(intentCall >= 0)
        assertTrue(intentGuard > intentCall)
        assertTrue(discovery > intentGuard)
        assertTrue(runner.contains("val requiredSkill = checkNotNull(recognition.executor).trim()"))
        assertTrue(runner.contains("requiredSkills = listOf(requiredSkill)"))
        assertTrue(runner.contains("PATROL_UTTERANCE = \"派机器狗巡逻A区域\""))
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
