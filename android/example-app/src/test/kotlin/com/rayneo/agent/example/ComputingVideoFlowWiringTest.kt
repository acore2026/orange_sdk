package com.rayneo.agent.example

import java.io.File
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ComputingVideoFlowWiringTest {
    @Test
    fun requesterCreatesTheFormalSessionAndSendsOnlyItsIdToTheProducer() {
        val runner = runnerSource()

        assertTrue(runner.contains("sdk.createComputingSession(request"))
        assertTrue(runner.contains("requestType = ComputeRequestType.CREATE"))
        assertTrue(runner.contains("inputFormat = ComputeInputFormat.STRUCTURED"))
        assertTrue(runner.contains("acnContext = AcnContext("))
        assertTrue(runner.contains("jsonMessage = buildJsonObject { put(COMPUTE_SESSION_ID_FIELD, sessionId) }"))
        assertFalse(runner.contains("video_server_ip"))
        assertFalse(runner.contains("OffloadingSession"))
        assertFalse(runner.contains("createOffloadingSession"))
    }

    @Test
    fun eachRoleWaitsForItsOwnInternalC02BeforeNegotiatingMedia() {
        val runner = runnerSource()

        assertTrue(runner.contains("computeServiceSessionId = sessionId"))
        assertTrue(runner.contains("sdk.getProcessedVideoStream(sessionId"))
        assertTrue(runner.contains("val track = stream.track"))
        assertTrue(runner.contains("if (config.role == TestRole.A) createAndReceiveProcessedVideo()"))
        assertTrue(runner.contains("onProducerSessionReady()"))
    }

    @Test
    fun appsUseTheSdkMediaImplementationAndReleaseBeforeDeregistering() {
        val generic = File(
            "src/main/kotlin/com/rayneo/agent/example/MainActivity.kt",
        ).readText()
        val rayneo = File(
            "src/rayneo/kotlin/com/rayneo/agent/example/RayNeoMainActivity.kt",
        ).readText()
        val runner = runnerSource()

        assertTrue(generic.contains("AgentSdk.create(service)"))
        assertTrue(rayneo.contains("AgentSdk.create(service)"))
        assertFalse(generic.contains("mediaOffloadAdapter"))
        assertFalse(rayneo.contains("mediaOffloadAdapter"))
        assertTrue(runner.contains("sdk.releaseComputingSession("))
        assertTrue(runner.contains("sdk.awaitComputingSessionClosed(sessionId"))
        assertFalse(generic.contains("runCatching { activeRunner.stopComputingSession() }"))
        assertFalse(rayneo.contains("runCatching { activeRunner.stopComputingSession() }"))
        assertTrue(generic.indexOf("activeRunner.stopComputingSession()") <
            generic.indexOf("activeRunner.deregisterAgentForStop()"))
        assertTrue(rayneo.indexOf("activeRunner.stopComputingSession()") <
            rayneo.indexOf("activeRunner.deregisterAgentForStop()"))
    }

    @Test
    fun processedTrackIsAttachedToBothGenericAndRayneoRenderers() {
        val runner = runnerSource()
        val generic = File(
            "src/main/kotlin/com/rayneo/agent/example/MainActivity.kt",
        ).readText()
        val rayneo = File(
            "src/rayneo/kotlin/com/rayneo/agent/example/RayNeoMainActivity.kt",
        ).readText()
        val rayneoLayout = File("src/rayneo/res/layout/activity_rayneo_main.xml").readText()

        assertTrue(runner.contains("val sinks = processedVideoRenderSinks() + diagnosticSink"))
        assertTrue(runner.contains("track.addSink(sink)"))
        assertTrue(generic.contains("TextureViewEglRenderer(this)"))
        assertTrue(rayneo.contains("processedVideoRenderSinks = ::processedVideoRenderSinks"))
        assertTrue(rayneoLayout.contains("@+id/video_renderer"))
    }

    private fun runnerSource(): String = File(
        "src/main/kotlin/com/rayneo/agent/example/AgentTestRunner.kt",
    ).readText()
}
