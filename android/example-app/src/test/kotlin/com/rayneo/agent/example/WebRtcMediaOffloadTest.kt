package com.rayneo.agent.example

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.webrtc.MediaCodecHighProfileVideoEncoderFactory
import java.io.File
import java.nio.ByteBuffer

class WebRtcMediaOffloadTest {
    @Test
    fun highProfileSpsParserAcceptsAnnexBAndAvcc() {
        val annexB = ByteBuffer.wrap(
            byteArrayOf(
                0, 0, 0, 1, 0x67, 0x64, 0x00, 0x1f,
                0, 0, 0, 1, 0x68, 0x01,
            ),
        )
        val avcc = ByteBuffer.wrap(
            byteArrayOf(0, 0, 0, 5, 0x67, 0x64, 0x0c, 0x1f, 0x01),
        )

        assertEquals(
            "64001f",
            MediaCodecHighProfileVideoEncoderFactory.findSpsProfileLevelId(annexB),
        )
        assertEquals(
            "640c1f",
            MediaCodecHighProfileVideoEncoderFactory.findSpsProfileLevelId(avcc),
        )
    }

    @Test
    fun highProfileSpsParserRejectsDataWithoutSps() {
        val deltaFrame = ByteBuffer.wrap(byteArrayOf(0, 0, 0, 1, 0x41, 0x01, 0x02))

        assertEquals(
            null,
            MediaCodecHighProfileVideoEncoderFactory.findSpsProfileLevelId(deltaFrame),
        )
    }

    @Test
    fun consumerOfferPrefersH264AndItsRtxWithoutRemovingFallbacks() {
        val offer = listOf(
            "v=0",
            "m=video 9 UDP/TLS/RTP/SAVPF 96 97 98 99 100 101",
            "a=rtpmap:96 VP8/90000",
            "a=rtpmap:97 rtx/90000",
            "a=fmtp:97 apt=96",
            "a=rtpmap:98 VP9/90000",
            "a=rtpmap:99 rtx/90000",
            "a=fmtp:99 apt=98",
            "a=rtpmap:100 H264/90000",
            "a=rtpmap:101 rtx/90000",
            "a=fmtp:101 apt=100",
            "",
        ).joinToString("\r\n")

        val preferred = preferH264InOfferSdp(offer)

        assertTrue(preferred.contains("m=video 9 UDP/TLS/RTP/SAVPF 100 101 96 97 98 99"))
        assertTrue(preferred.contains("a=rtpmap:96 VP8/90000"))
        assertTrue(preferred.endsWith("\r\n"))
    }

    @Test
    fun producerAppliesServerOfferBeforeBindingTheCameraTrack() {
        val source = File(
            "src/main/kotlin/com/rayneo/agent/example/AndroidWebRtcMediaOffloadAdapter.kt",
        ).readText()
        val applyOffer = source.indexOf("pc.awaitSetRemote(remoteOffer)")
        val findOfferedTransceiver = source.indexOf("pc.transceivers.filter")
        val bindTrack = source.indexOf("transceiver.sender.setTrack(localTrack, false)")
        val createAnswer = source.indexOf("val answer = pc.awaitCreateAnswer()")

        assertTrue(applyOffer >= 0)
        assertTrue(applyOffer < findOfferedTransceiver)
        assertTrue(findOfferedTransceiver < bindTrack)
        assertTrue(bindTrack < createAnswer)
        assertTrue(!source.contains("pc.addTransceiver(\n            localTrack"))
        assertTrue(source.contains("MediaCodecHighProfileVideoEncoderFactory"))
        assertTrue(source.contains("highProfileEncoderFactory.isHighProfileAvailable"))
    }

    @Test
    fun sendingVideoAnswerRequiresAnActiveDirectionAndMid() {
        val answer = """
            v=0
            m=video 9 UDP/TLS/RTP/SAVPF 96
            a=mid:video-source
            a=sendonly
        """.trimIndent()

        assertEquals("video-source", requireSendingVideoAnswer(answer))

        assertThrows(IllegalStateException::class.java) {
            requireSendingVideoAnswer(answer.replace("a=sendonly", "a=inactive"))
        }
        assertThrows(IllegalStateException::class.java) {
            requireSendingVideoAnswer(answer.replace("m=video 9", "m=video 0"))
        }
    }

    @Test
    fun producerRequiresH264HighProfileOnTheSourceLeg() {
        val highAnswer = """
            v=0
            m=video 9 UDP/TLS/RTP/SAVPF 104 105
            a=mid:video-source
            a=sendonly
            a=rtpmap:104 H264/90000
            a=fmtp:104 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=64001f
            a=rtpmap:105 rtx/90000
            a=fmtp:105 apt=104
        """.trimIndent()

        assertEquals("64001f", requireH264HighVideoCodec(highAnswer, "test answer"))
        assertEquals("video-source", requireH264HighSendingVideoAnswer(highAnswer))

        val baselineAnswer = highAnswer.replace("64001f", "42e01f")
        assertThrows(IllegalStateException::class.java) {
            requireH264HighSendingVideoAnswer(baselineAnswer)
        }
        val vp8Answer = highAnswer
            .replace("H264/90000", "VP8/90000")
            .replace(
                "a=fmtp:104 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=64001f\n",
                "",
            )
        assertThrows(IllegalStateException::class.java) {
            requireH264HighSendingVideoAnswer(vp8Answer)
        }
    }

    @Test
    fun consumerUsesTheCommittedUnifiedPlanReceiverAndCollectsInboundRtp() {
        val source = File(
            "src/main/kotlin/com/rayneo/agent/example/AndroidWebRtcMediaOffloadAdapter.kt",
        ).readText()
        val applyAnswer = source.indexOf(
            "pc.awaitSetRemote(response.requireSdp(\"sdp_answer\"",
        )
        val selectReceiver = source.indexOf("val receivingTransceivers = pc.transceivers.filter")
        val attachDiagnosticSink = source.indexOf("remoteTrack.addSink(diagnosticSink)")
        val returnTrack = source.indexOf("return AndroidVideoTrack(remoteTrack)")

        assertTrue(applyAnswer >= 0)
        assertTrue(applyAnswer < selectReceiver)
        assertTrue(selectReceiver < attachDiagnosticSink)
        assertTrue(attachDiagnosticSink < returnTrack)
        assertTrue(!source.contains("val trackReady = CompletableDeferred"))
        assertTrue(source.contains("stat.type == \"inbound-rtp\""))
        assertTrue(source.contains("packetsReceived"))
        assertTrue(source.contains("framesDecoded"))
        assertTrue(source.contains("framesDropped"))
    }

    @Test
    fun processedTrackIsAttachedToVisibleRenderersBeforeTheLoggingSink() {
        val runner = File(
            "src/main/kotlin/com/rayneo/agent/example/AgentTestRunner.kt",
        ).readText()
        val genericActivity = File(
            "src/main/kotlin/com/rayneo/agent/example/MainActivity.kt",
        ).readText()
        val rayneoActivity = File(
            "src/rayneo/kotlin/com/rayneo/agent/example/RayNeoMainActivity.kt",
        ).readText()
        val rayneoLayout = File("src/rayneo/res/layout/activity_rayneo_main.xml").readText()
        val adapter = File(
            "src/main/kotlin/com/rayneo/agent/example/AndroidWebRtcMediaOffloadAdapter.kt",
        ).readText()
        val textureRenderer = File(
            "src/main/kotlin/com/rayneo/agent/example/TextureViewEglRenderer.kt",
        ).readText()

        assertTrue(runner.contains("addAll(processedVideoRenderSinks())"))
        assertTrue(runner.contains("track.addSink(renderSink)"))
        assertTrue(runner.contains("track.removeSink(attached)"))
        assertTrue(genericActivity.contains("TextureViewEglRenderer(this)"))
        assertTrue(genericActivity.contains("init(eglBase.eglBaseContext, rendererEvents)"))
        assertTrue(genericActivity.contains("sharedEglContext = videoPreviewEglBase?.eglBaseContext"))
        assertTrue(!genericActivity.contains("DecodedFrameVideoRenderer"))
        assertTrue(textureRenderer.contains(": TextureView(context, attrs)"))
        assertTrue(textureRenderer.contains("EglRenderer("))
        assertTrue(textureRenderer.contains("eglRenderer.onFrame(frame)"))
        assertTrue(!textureRenderer.contains("toI420()"))
        assertTrue(!textureRenderer.contains("android.graphics.Bitmap"))
        assertTrue(!textureRenderer.contains("BitmapFactory"))
        assertTrue(adapter.contains("private val egl = EglBase.create(sharedEglContext)"))
        assertTrue(genericActivity.contains("RendererCommon.ScalingType.SCALE_ASPECT_FIT"))
        assertTrue(rayneoActivity.contains("processedVideoRenderSinks = ::processedVideoRenderSinks"))
        assertTrue(rayneoActivity.contains("sharedEglContext = videoEglBase?.eglBaseContext"))
        assertTrue(rayneoLayout.contains("<org.webrtc.SurfaceViewRenderer"))
        assertTrue(rayneoLayout.contains("@+id/video_renderer"))
        assertTrue(rayneoActivity.contains("videoRenderer.init(eglBase.eglBaseContext, events)"))
        assertTrue(rayneoActivity.contains("CopyOnWriteArraySet<SurfaceViewRenderer>()"))
    }

    @Test
    fun huaweiUsesMediaCodecByteBufferOutputAndStopReleasesTexturesBeforeDecoder() {
        val adapter = File(
            "src/main/kotlin/com/rayneo/agent/example/AndroidWebRtcMediaOffloadAdapter.kt",
        ).readText()
        val activity = File(
            "src/main/kotlin/com/rayneo/agent/example/MainActivity.kt",
        ).readText()
        val stopFlow = activity.substringAfter("private fun stopOrReturn()")
            .substringBefore("private fun readConfig()")

        assertTrue(adapter.contains("Build.MANUFACTURER.equals(\"HUAWEI\""))
        assertTrue(adapter.contains("if (usesByteBufferDecoderOutput) null else egl.eglBaseContext"))
        assertTrue(adapter.contains("DefaultVideoDecoderFactory(decoderEglContext)"))
        assertTrue(adapter.contains("绕过 OMX.hisi DynamicANWBuffer/Surface 输出故障"))

        val removeTrackSinks = stopFlow.indexOf("activeRunner?.close()")
        val releaseRenderer = stopFlow.indexOf("detachProcessedVideoRenderer()")
        val closeDecoder = stopFlow.indexOf("activeSdk?.close()")
        val releaseRootEgl = stopFlow.indexOf("eglBase.release()")
        assertTrue(removeTrackSinks >= 0)
        assertTrue(removeTrackSinks < releaseRenderer)
        assertTrue(releaseRenderer < closeDecoder)
        assertTrue(closeDecoder < releaseRootEgl)
        assertTrue(!stopFlow.contains("finish()"))
        assertTrue(activity.contains("lastSdkDiagnosticSummary"))
        assertTrue(activity.contains("lastVideoPreviewSummary"))
        assertTrue(activity.contains("已停止 · 日志已保留"))
    }
}
