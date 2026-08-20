package com.rayneo.agent.sdk.server

import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import com.rayneo.agent.sdk.transport.LocalServer
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.io.BufferedOutputStream
import java.io.InputStream
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket

class TcpJsonLocalServer(
    private val json: Json = Json,
) : LocalServer {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val tcpSockets = mutableListOf<ServerSocket>()
    private val udpSockets = mutableListOf<DatagramSocket>()
    private val jobs = mutableListOf<Job>()

    override suspend fun start(
        physicalIp: String,
        agentIp: String,
        tcpPort: Int,
        udpPort: Int,
        onA2aMessage: suspend (JsonObject) -> Unit,
    ) {
        try {
            val physical = bindTcp(physicalIp, tcpPort)
            tcpSockets += physical
            jobs += acceptLoop(physical) { path, _ ->
                throw AgentSdkException(
                    ErrorCode.INVALID_ARGUMENT,
                    "Runtime downlink uses WebSocket; path $path is not available",
                )
            }

            val agent = bindTcp(agentIp, tcpPort)
            tcpSockets += agent
            jobs += acceptLoop(agent) { path, payload ->
                if (path != "/A2A/message") {
                    throw AgentSdkException(
                        ErrorCode.INVALID_ARGUMENT,
                        "Path $path is not allowed on Agent TUN ingress",
                    )
                }
                onA2aMessage(payload)
                buildJsonObject { put("ack", true) }
            }

            listOf(physicalIp, agentIp).distinct().forEach { address ->
                udpSockets += DatagramSocket(null).apply {
                    reuseAddress = false
                    bind(InetSocketAddress(InetAddress.getByName(address), udpPort))
                }
            }
        } catch (error: Exception) {
            close()
            throw AgentSdkException(
                ErrorCode.LOCAL_PORT_IN_USE,
                "Failed to bind SDK TCP/UDP listeners",
                cause = error,
            )
        }
    }

    private fun bindTcp(address: String, port: Int): ServerSocket =
        ServerSocket().apply {
            reuseAddress = false
            bind(InetSocketAddress(InetAddress.getByName(address), port))
        }

    private fun acceptLoop(
        server: ServerSocket,
        handler: suspend (String, JsonObject) -> JsonObject,
    ): Job = scope.launch {
        while (isActive && !server.isClosed) {
            val socket = try {
                server.accept()
            } catch (_: Exception) {
                break
            }
            launch { handleSocket(socket, handler) }
        }
    }

    private suspend fun handleSocket(
        socket: Socket,
        handler: suspend (String, JsonObject) -> JsonObject,
    ) {
        socket.use {
            val output = BufferedOutputStream(socket.getOutputStream())
            try {
                val (path, contentLength) = readRequestHead(socket)
                if (contentLength !in 0..MAX_BODY_BYTES) {
                    throw IllegalArgumentException("Invalid Content-Length")
                }
                val bytes = readExactly(socket.getInputStream(), contentLength)
                val payload = json.parseToJsonElement(bytes.decodeToString()) as? JsonObject
                    ?: throw IllegalArgumentException("JSON object required")
                writeResponse(output, 200, handler(path, payload))
            } catch (error: AgentSdkException) {
                writeResponse(output, 400, buildJsonObject {
                    put("error", error.code.name)
                    put("message", error.message ?: "SDK error")
                })
            } catch (error: Exception) {
                writeResponse(output, 400, buildJsonObject {
                    put("error", "BAD_REQUEST")
                    put("message", error.message ?: "Bad request")
                })
            }
        }
    }

    private fun readRequestHead(socket: Socket): Pair<String, Int> {
        val input = socket.getInputStream()
        val header = ArrayList<Byte>()
        var matched = 0
        while (header.size < MAX_HEADER_BYTES && matched < 4) {
            val value = input.read()
            if (value < 0) throw IllegalArgumentException("Connection closed")
            header += value.toByte()
            matched = when {
                matched == 0 && value == '\r'.code -> 1
                matched == 1 && value == '\n'.code -> 2
                matched == 2 && value == '\r'.code -> 3
                matched == 3 && value == '\n'.code -> 4
                value == '\r'.code -> 1
                else -> 0
            }
        }
        if (matched != 4) throw IllegalArgumentException("HTTP header too large")
        val lines = header.toByteArray().decodeToString().split("\r\n")
        val request = lines.firstOrNull()?.split(' ') ?: emptyList()
        if (request.size < 2 || request[0] != "POST") {
            throw IllegalArgumentException("Only HTTP POST is supported")
        }
        val contentLength = lines.drop(1)
            .firstOrNull { it.startsWith("Content-Length:", ignoreCase = true) }
            ?.substringAfter(':')
            ?.trim()
            ?.toIntOrNull()
            ?: throw IllegalArgumentException("Content-Length is required")
        return request[1] to contentLength
    }

    private fun readExactly(input: InputStream, length: Int): ByteArray {
        val result = ByteArray(length)
        var offset = 0
        while (offset < length) {
            val count = input.read(result, offset, length - offset)
            if (count < 0) throw IllegalArgumentException("Truncated body")
            if (count > 0) offset += count
        }
        return result
    }

    private fun writeResponse(output: BufferedOutputStream, status: Int, body: JsonObject) {
        val bytes = body.toString().encodeToByteArray()
        val reason = if (status == 200) "OK" else "Bad Request"
        output.write(
            "HTTP/1.1 $status $reason\r\nContent-Type: application/json\r\nContent-Length: ${bytes.size}\r\nConnection: close\r\n\r\n"
                .encodeToByteArray()
        )
        output.write(bytes)
        output.flush()
    }

    override suspend fun close() {
        tcpSockets.forEach { runCatching { it.close() } }
        udpSockets.forEach { runCatching { it.close() } }
        tcpSockets.clear()
        udpSockets.clear()
        jobs.forEach { it.cancel() }
        jobs.clear()
        scope.cancel()
    }

    private companion object {
        const val MAX_HEADER_BYTES = 16 * 1024
        const val MAX_BODY_BYTES = 1024 * 1024
    }
}
