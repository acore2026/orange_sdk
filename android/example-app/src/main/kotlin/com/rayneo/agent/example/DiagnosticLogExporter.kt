package com.rayneo.agent.example

import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.Process
import android.os.SystemClock
import android.provider.MediaStore
import android.widget.Toast
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.Socket
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter
import java.util.Collections
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

internal data class DiagnosticDump(
    val file: File,
    val content: String,
    val publicUri: Uri?,
    val displayLocation: String,
)

internal object DiagnosticLogExporter {
    suspend fun createDump(
        context: Context,
        config: TestConfig,
        appLogs: List<String>,
        sdkSummary: String,
        localTcpEndpoint: String?,
        videoPreviewSummary: String = "<not applicable>",
    ): DiagnosticDump = withContext(Dispatchers.IO) {
        val timestamp = OffsetDateTime.now().format(DateTimeFormatter.ISO_OFFSET_DATE_TIME)
        val content = buildString {
            appendLine("AGENT LINK LAB DIAGNOSTIC DUMP")
            appendLine("generated_at=$timestamp")
            appendLine("process_id=${Process.myPid()}")
            appendLine("process_uptime_ms=${SystemClock.elapsedRealtime()}")
            appendLine()
            appendLine("[APP]")
            appendLine(packageSummary(context))
            appendLine("role=${config.role.name}")
            appendLine("runtime=http://${config.serverIp}:${config.runtimePort}")
            appendLine("masque=https://${config.serverIp}:${config.masquePort}${config.masquePath}")
            appendLine("masque_token=${if (config.masqueToken.isNullOrBlank()) "<none>" else "<redacted>"}")
            appendLine("local_tcp_port=${config.localTcpPort}")
            appendLine("local_udp_port=${config.localUdpPort}")
            appendLine("discovery_asr_endpoint=${config.discoveryAsrUrl}")
            appendLine("compute_endpoint=<managed by SDK from C-02>")
            appendLine()
            appendLine("[DEVICE]")
            appendLine("manufacturer=${Build.MANUFACTURER}")
            appendLine("brand=${Build.BRAND}")
            appendLine("model=${Build.MODEL}")
            appendLine("device=${Build.DEVICE}")
            appendLine("product=${Build.PRODUCT}")
            appendLine("android_sdk=${Build.VERSION.SDK_INT}")
            appendLine("android_release=${Build.VERSION.RELEASE}")
            appendLine("fingerprint=${Build.FINGERPRINT}")
            appendLine()
            appendLine("[SDK STATE]")
            appendLine(sdkSummary.ifBlank { "<unavailable>" })
            appendLine()
            appendLine("[VIDEO PREVIEW RENDERER]")
            appendLine(videoPreviewSummary)
            appendLine()
            appendLine("[LOCAL TCP SELF PROBE]")
            appendLine(probeLocalTcp(localTcpEndpoint))
            appendLine()
            appendLine("[NETWORK INTERFACES]")
            appendLine(networkInterfaces())
            appendLine()
            appendLine("[ANDROID CONNECTIVITY]")
            appendLine(connectivity(context))
            appendLine()
            appendLine("[PROC SELF NETWORK]")
            PROC_NETWORK_FILES.forEach { path ->
                appendLine("--- $path ---")
                appendLine(readProcFile(path))
            }
            appendLine()
            appendLine("[APP FLOW LOG]")
            appendLine(appLogs.joinToString("\n").ifBlank { "<empty>" })
            appendLine()
            appendLine("[SDK AND APP LOGCAT]")
            appendLine(processLogcat(LOGCAT_RELEVANT_FILTERS, LOGCAT_RELEVANT_LINE_LIMIT))
            appendLine()
            appendLine("[PROCESS LOGCAT TAIL]")
            appendLine(processLogcat(emptyList(), LOGCAT_PROCESS_LINE_LIMIT))
        }
        val directory = context.getExternalFilesDir("diagnostics")
            ?: File(context.filesDir, "diagnostics")
        check(directory.exists() || directory.mkdirs()) {
            "无法创建诊断日志目录 ${directory.absolutePath}"
        }
        val fileTimestamp = OffsetDateTime.now()
            .format(DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss-SSS"))
        val file = File(directory, "agent-link-diagnostic-$fileTimestamp.txt")
        file.writeText(content)
        val publicUri = publishToDownloads(context, file.name, content)
        DiagnosticDump(
            file = file,
            content = content,
            publicUri = publicUri,
            displayLocation = if (publicUri != null) {
                "Download/AgentLinkDiagnostics/${file.name}"
            } else {
                file.absolutePath
            },
        )
    }

    fun share(activity: Activity, dump: DiagnosticDump) {
        val uri = dump.publicUri ?: FileProvider.getUriForFile(
                activity,
                "${activity.packageName}.fileprovider",
                dump.file,
            )
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_SUBJECT, dump.file.name)
            putExtra(Intent.EXTRA_TEXT, "Agent Link 端侧诊断日志：${dump.file.name}")
            putExtra(Intent.EXTRA_STREAM, uri)
            clipData = ClipData.newRawUri(dump.file.name, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        val hasShareTarget = activity.packageManager
            .queryIntentActivities(intent, 0)
            .isNotEmpty()
        val launched = hasShareTarget && runCatching {
            activity.startActivity(Intent.createChooser(intent, "发送端侧诊断日志"))
        }.isSuccess
        if (!launched) {
            val clipboard = activity.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            clipboard.setPrimaryClip(
                ClipData.newPlainText("Agent Link diagnostic dump", dump.displayLocation),
            )
            Toast.makeText(
                activity,
                "没有分享应用；日志已保存到 ${dump.displayLocation}",
                Toast.LENGTH_LONG,
            ).show()
        }
    }

    private fun publishToDownloads(
        context: Context,
        fileName: String,
        content: String,
    ): Uri? {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return null
        val resolver = context.contentResolver
        val values = ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, fileName)
            put(MediaStore.Downloads.MIME_TYPE, "text/plain")
            put(
                MediaStore.Downloads.RELATIVE_PATH,
                "${Environment.DIRECTORY_DOWNLOADS}/AgentLinkDiagnostics",
            )
            put(MediaStore.Downloads.IS_PENDING, 1)
        }
        val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            ?: return null
        return try {
            resolver.openOutputStream(uri, "w")?.use { output ->
                output.write(content.encodeToByteArray())
            } ?: error("无法打开公共 Download 诊断文件")
            resolver.update(
                uri,
                ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) },
                null,
                null,
            )
            uri
        } catch (error: Exception) {
            runCatching { resolver.delete(uri, null, null) }
            null
        }
    }

    private fun packageSummary(context: Context): String = runCatching {
        val info = context.packageManager.getPackageInfo(context.packageName, 0)
        val code = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            info.longVersionCode
        } else {
            @Suppress("DEPRECATION")
            info.versionCode.toLong()
        }
        "package=${context.packageName}\nversion_name=${info.versionName}\nversion_code=$code"
    }.getOrElse { "package=${context.packageName}\nversion_error=${errorSummary(it)}" }

    private fun probeLocalTcp(endpoint: String?): String {
        if (endpoint.isNullOrBlank()) return "status=SKIPPED reason=SDK init endpoint unavailable"
        val separator = endpoint.lastIndexOf(':')
        val host = endpoint.substring(0, separator.coerceAtLeast(0))
        val port = endpoint.substringAfterLast(':').toIntOrNull()
        if (separator <= 0 || host.isBlank() || port == null) {
            return "status=SKIPPED endpoint=$endpoint reason=invalid endpoint"
        }
        val startedAt = SystemClock.elapsedRealtime()
        return runCatching {
            Socket().use { socket ->
                socket.connect(InetSocketAddress(host, port), LOCAL_PROBE_TIMEOUT_MILLIS)
            }
            "status=CONNECTED endpoint=$endpoint elapsed_ms=${SystemClock.elapsedRealtime() - startedAt}"
        }.getOrElse {
            "status=FAILED endpoint=$endpoint elapsed_ms=${SystemClock.elapsedRealtime() - startedAt} " +
                "error=${errorSummary(it)}"
        }
    }

    private fun networkInterfaces(): String = runCatching {
        Collections.list(NetworkInterface.getNetworkInterfaces())
            .sortedBy { it.name }
            .joinToString("\n") { networkInterface ->
                val addresses = Collections.list(networkInterface.inetAddresses)
                    .joinToString(",") { it.hostAddress ?: "<unknown>" }
                "name=${networkInterface.name} display=${networkInterface.displayName} " +
                    "up=${networkInterface.isUp} loopback=${networkInterface.isLoopback} " +
                    "virtual=${networkInterface.isVirtual} mtu=${networkInterface.mtu} " +
                    "addresses=[$addresses]"
            }
            .ifBlank { "<none>" }
    }.getOrElse { "error=${errorSummary(it)}" }

    @Suppress("DEPRECATION")
    private fun connectivity(context: Context): String = runCatching {
        val manager = context.getSystemService(ConnectivityManager::class.java)
        manager.allNetworks.joinToString("\n") { network ->
            val capabilities = manager.getNetworkCapabilities(network)
            val properties = manager.getLinkProperties(network)
            val transports = buildList {
                if (capabilities?.hasTransport(NetworkCapabilities.TRANSPORT_VPN) == true) add("VPN")
                if (capabilities?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true) add("WIFI")
                if (capabilities?.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) == true) add("CELLULAR")
                if (capabilities?.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) == true) add("ETHERNET")
            }
            "network=$network transports=$transports interface=${properties?.interfaceName} " +
                "addresses=${properties?.linkAddresses} routes=${properties?.routes} " +
                "dns=${properties?.dnsServers} capabilities=$capabilities"
        }.ifBlank { "<none>" }
    }.getOrElse { "error=${errorSummary(it)}" }

    private fun readProcFile(path: String): String = runCatching {
        File(path).readText().trimEnd().ifBlank { "<empty>" }
    }.getOrElse { "error=${errorSummary(it)}" }

    private fun processLogcat(filters: List<String>, lineLimit: Int): String = runCatching {
        val arguments = mutableListOf(
            "logcat",
            "-d",
            "-v",
            "threadtime",
            "--pid=${Process.myPid()}",
            "-t",
            lineLimit.toString(),
        ).apply {
            addAll(filters)
            if (filters.isNotEmpty()) add("*:S")
        }
        val process = ProcessBuilder(arguments).redirectErrorStream(true).start()
        val output = AtomicReference("")
        val readError = AtomicReference<Throwable?>(null)
        val reader = Thread({
            try {
                output.set(process.inputStream.bufferedReader().use { it.readText() })
            } catch (error: Throwable) {
                readError.set(error)
            }
        }, "agent-diagnostic-logcat-reader").apply { start() }
        val completed = process.waitFor(LOGCAT_TIMEOUT_SECONDS, TimeUnit.SECONDS)
        if (!completed) {
            process.destroyForcibly()
        }
        reader.join(LOGCAT_READER_JOIN_MILLIS)
        if (reader.isAlive) {
            reader.interrupt()
            return@runCatching "error=logcat timed out after ${LOGCAT_TIMEOUT_SECONDS}s"
        }
        readError.get()?.let { throw it }
        val captured = output.get()
            .trimEnd()
            .ifBlank { "<empty or unavailable on this Android build>" }
        if (completed) captured else "warning=logcat forcibly stopped after ${LOGCAT_TIMEOUT_SECONDS}s\n$captured"
    }.getOrElse { "error=${errorSummary(it)}" }

    private fun errorSummary(error: Throwable): String {
        val root = generateSequence(error) { it.cause }.last()
        return "${root::class.java.simpleName}: ${root.message ?: "no message"}"
    }

    private const val LOCAL_PROBE_TIMEOUT_MILLIS = 1_500
    private const val LOGCAT_RELEVANT_LINE_LIMIT = 10_000
    private const val LOGCAT_PROCESS_LINE_LIMIT = 1_000
    private const val LOGCAT_TIMEOUT_SECONDS = 4L
    private const val LOGCAT_READER_JOIN_MILLIS = 1_000L
    private val LOGCAT_RELEVANT_FILTERS = listOf(
        "AgentLinkLab:V",
        "AgentSdk:V",
        "AgentSdkRuntime:V",
        "AgentSdkLocalServer:V",
        "AgentSdkVpn:V",
        "AgentSdkWebRtc:V",
        "System.out:I",
    )
    private val PROC_NETWORK_FILES = listOf(
        "/proc/self/net/route",
        "/proc/self/net/tcp",
        "/proc/self/net/tcp6",
        "/proc/self/net/udp",
        "/proc/self/net/udp6",
    )
}
