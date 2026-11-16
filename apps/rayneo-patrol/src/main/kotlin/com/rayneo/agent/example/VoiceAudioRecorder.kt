package com.rayneo.agent.example

import android.content.Context
import android.media.MediaRecorder
import android.os.Build
import java.io.File

internal data class RecordedAudio(
    val file: File,
    val fileName: String,
    val contentType: String,
)

internal class VoiceAudioRecorder(private val context: Context) {
    private var recorder: MediaRecorder? = null
    private var outputFile: File? = null

    val isRecording: Boolean get() = recorder != null

    fun start() {
        check(recorder == null) { "录音已经开始" }
        val file = File.createTempFile("agent-voice-", ".m4a", context.cacheDir)
        val created = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            MediaRecorder(context)
        } else {
            @Suppress("DEPRECATION")
            MediaRecorder()
        }
        try {
            created.setAudioSource(MediaRecorder.AudioSource.MIC)
            created.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            created.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            created.setAudioChannels(1)
            created.setAudioSamplingRate(16_000)
            created.setAudioEncodingBitRate(64_000)
            created.setOutputFile(file.absolutePath)
            created.prepare()
            created.start()
            outputFile = file
            recorder = created
        } catch (error: Throwable) {
            runCatching { created.release() }
            file.delete()
            throw error
        }
    }

    fun stop(): RecordedAudio {
        val active = checkNotNull(recorder) { "录音尚未开始" }
        val file = checkNotNull(outputFile)
        recorder = null
        outputFile = null
        try {
            active.stop()
        } catch (error: RuntimeException) {
            file.delete()
            throw IllegalStateException("录音太短或未采集到有效音频", error)
        } finally {
            active.release()
        }
        if (file.length() <= 0L) {
            file.delete()
            error("录音文件为空")
        }
        return RecordedAudio(file, file.name, "audio/mp4")
    }

    fun cancel() {
        val active = recorder
        val file = outputFile
        recorder = null
        outputFile = null
        if (active != null) {
            runCatching { active.stop() }
            runCatching { active.release() }
        }
        file?.delete()
    }
}
