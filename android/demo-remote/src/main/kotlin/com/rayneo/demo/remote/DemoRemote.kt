package com.rayneo.demo.remote

import java.io.Closeable
import java.io.InputStream
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URI
import java.net.HttpURLConnection
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/** One bounded UTF-8 line per request, compatible with the reference TCP transport. */
object DemoRemote {
    const val PORT = 8791
    const val MAX_LINE = 8192
    val commands = setOf("STATUS", "PATROL", "CONFIRM", "CANCEL", "DETER", "EXPEL", "DEREGISTER_GLASSES",
        "CONFIRM_EXPEL", "VOICE_START", "VOICE_STOP", "RTC_RECONNECT", "VIDEO_SHOW", "VIDEO_HIDE", "CONFIG_CORE", "CONFIG_SANDBOX", "FORWARD", "BACK", "LEFT", "RIGHT", "STOP")
    fun coreHost(raw: String): String {
        val host = raw.trim().lowercase()
        require(host.length in 1..253 && host.matches(Regex("[a-z0-9][a-z0-9.-]*[a-z0-9]|[a-z0-9]"))) { "Enter an IPv4 address or DNS hostname, without scheme or port" }
        require(host.split('.').all { it.isNotEmpty() && it.length <= 63 && !it.startsWith('-') && !it.endsWith('-') }) { "Invalid hostname" }
        if (host.all { it.isDigit() || it == '.' }) require(host.split('.').size == 4 && host.split('.').all { it.toIntOrNull() in 0..255 }) { "Invalid IPv4 address" }
        return host
    }
    fun endpoint(raw: String): InetSocketAddress {
        val uri = URI("tcp://${raw.trim()}")
        require(uri.userInfo == null && uri.path.orEmpty().isEmpty() && uri.query == null && uri.fragment == null && uri.host != null) { "Use glasses IP:port" }
        val port = if (uri.port == -1) PORT else uri.port
        require(port in 1..65535)
        return InetSocketAddress(coreHost(uri.host), port)
    }
    fun readLine(input: InputStream): String {
        val bytes = java.io.ByteArrayOutputStream()
        while (true) {
            val ch = input.read()
            require(ch >= 0) { "Connection closed before response" }
            if (ch == 10) break
            require(bytes.size() < MAX_LINE) { "Message too long" }
            bytes.write(ch)
        }
        return bytes.toByteArray().toString(Charsets.UTF_8).trimEnd('\r')
    }
    /** Direct LAN protocol: one JSON object per line, no pairing envelope. */
    fun envelope(command: String): String {
        require(command.isNotBlank() && !command.contains('\n') && !command.contains('\r'))
        require(command.toByteArray(Charsets.UTF_8).size <= MAX_LINE)
        return command
    }
    fun exchange(endpoint: String, request: String): String = Socket().use { socket ->
        require(!request.contains('\n') && request.toByteArray().size <= MAX_LINE)
        socket.soTimeout = 8000
        socket.connect(endpoint(endpoint), 4000)
        socket.getOutputStream().write((request + "\n").toByteArray(Charsets.UTF_8))
        socket.getOutputStream().flush()
        readLine(socket.getInputStream())
    }

    fun dogAdmin(endpoint: String, host: String? = null): String {
        val address = endpoint(endpoint)
        val port = if (URI("tcp://${endpoint.trim()}").port == -1) 8081 else address.port
        val path = if (host == null) "/health" else "/config/core"
        val connection = URI("http://${address.hostString}:$port$path").toURL().openConnection() as HttpURLConnection
        return try {
            connection.connectTimeout = 4000
            connection.readTimeout = 8000
            connection.instanceFollowRedirects = false
            if (host != null) {
                val safeHost = coreHost(host)
                connection.requestMethod = "POST"
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                connection.outputStream.use { it.write(("{\"host\":\"$safeHost\"}").toByteArray()) }
            }
            val code = connection.responseCode
            val body = (if (code in 200..299) connection.inputStream else connection.errorStream)
                ?.bufferedReader()?.use { it.readText() }.orEmpty()
            check(code in 200..299) { "Dog HTTP $code: $body" }
            body
        } finally { connection.disconnect() }
    }

    /** POST once; subsequent requests only retrieve this operation's result. */
    fun dogIdentity(endpoint: String, requestId: String, submit: Boolean): String {
        require(requestId.matches(Regex("[a-zA-Z0-9_-]{8,80}")))
        val uri = URI("http://${endpoint.trim()}")
        require(uri.userInfo == null && uri.path.orEmpty().isEmpty() && uri.query == null && uri.fragment == null && uri.host != null)
        val host = coreHost(uri.host)
        val port = if (uri.port == -1) 8081 else uri.port
        require(port in 1..65535)
        val path = if (submit) "/identity/deregister" else "/identity/status?request_id=$requestId"
        val connection = URI("http://$host:$port$path").toURL().openConnection() as HttpURLConnection
        return try {
            connection.connectTimeout = 4000
            connection.readTimeout = 8000
            connection.instanceFollowRedirects = false
            if (submit) {
                connection.requestMethod = "POST"
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                connection.outputStream.use { it.write(("{\"request_id\":\"$requestId\"}").toByteArray(Charsets.UTF_8)) }
            }
            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val body = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
            check(code in 200..299) { "Dog identity HTTP $code: $body" }
            body
        } finally { connection.disconnect() }
    }

}

/** Sequential accept loop keeps command order; each handler must only enqueue work. */
class DemoRemoteServer(private val handler: (String) -> String, private val error: (Exception) -> Unit) : Closeable {
    private val running = AtomicBoolean(false)
    private val executor = Executors.newSingleThreadExecutor()
    @Volatile private var server: ServerSocket? = null
    @Volatile private var client: Socket? = null
    fun start(port: Int = DemoRemote.PORT) {
        check(running.compareAndSet(false, true))
        executor.execute {
            try {
                ServerSocket().use { listener ->
                    server = listener
                    listener.reuseAddress = true
                    listener.bind(InetSocketAddress(port))
                    while (running.get()) {
                        try {
                            listener.accept().use { socket ->
                                client = socket
                                socket.soTimeout = 3000
                                val response = handler(DemoRemote.readLine(socket.getInputStream()))
                                socket.getOutputStream().write((response + "\n").toByteArray(Charsets.UTF_8))
                                socket.getOutputStream().flush()
                            }
                        } catch (e: Exception) { if (running.get()) error(e) }
                        finally { client = null }
                    }
                }
            } catch (e: Exception) { if (running.get()) error(e) }
        }
    }
    override fun close() {
        running.set(false)
        runCatching { client?.close() }
        runCatching { server?.close() }
        executor.shutdownNow()
    }
}
