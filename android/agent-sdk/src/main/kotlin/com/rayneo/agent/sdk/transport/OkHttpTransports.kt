package com.rayneo.agent.sdk.transport

import android.util.Log
import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import com.rayneo.agent.sdk.model.NetworkMessageAction
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.RequestBody.Companion.toRequestBody
import java.net.InetAddress
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

const val DOWNLINK_WEBSOCKET_PATH = "/v1/acn/downlink-websocket"

class OkHttpRuntimeTransport(
    host: String,
    port: Int,
    private val client: OkHttpClient = OkHttpClient(),
    private val json: Json = Json,
) : RuntimeTransport {
    private val baseUrl = "http://$host:$port"
    private val downlinkScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val downlinkStarted = AtomicBoolean(false)
    @Volatile private var downlinkSocket: WebSocket? = null

    override suspend fun connect() = withContext(Dispatchers.IO) {
        try {
            client.newCall(Request.Builder().url("$baseUrl/health").get().build()).execute().close()
        } catch (error: Exception) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_UNREACHABLE,
                "AgentRuntime is unreachable",
                retryable = true,
                cause = error,
            )
        }
    }

    override suspend fun startDownlink(
        handler: suspend (String, Int, JsonObject) -> NetworkMessageAction,
    ) {
        if (!downlinkStarted.compareAndSet(false, true)) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "Runtime downlink WebSocket is already started",
            )
        }
        try {
            suspendCancellableCoroutine<Unit> { continuation ->
                val request = Request.Builder()
                    .url(baseUrl + DOWNLINK_WEBSOCKET_PATH)
                    .get()
                    .build()
                val opened = AtomicBoolean(false)
                val socket = client.newWebSocket(request, object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        opened.set(true)
                        downlinkSocket = webSocket
                        Log.i(TAG, "Runtime downlink WebSocket connected")
                        if (continuation.isActive) continuation.resume(Unit)
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        downlinkScope.launch {
                            processDownlinkFrame(webSocket, text, handler)
                        }
                    }

                    override fun onFailure(
                        webSocket: WebSocket,
                        t: Throwable,
                        response: Response?,
                    ) {
                        Log.e(TAG, "Runtime downlink WebSocket failed", t)
                        if (!opened.get() && continuation.isActive) {
                            continuation.resumeWithException(
                                AgentSdkException(
                                    ErrorCode.RUNTIME_UNREACHABLE,
                                    "AgentRuntime downlink WebSocket is unreachable",
                                    retryable = true,
                                    cause = t,
                                )
                            )
                        }
                    }
                })
                downlinkSocket = socket
                continuation.invokeOnCancellation { socket.cancel() }
            }
        } catch (error: Exception) {
            downlinkStarted.set(false)
            throw error
        }
    }

    private suspend fun processDownlinkFrame(
        socket: WebSocket,
        text: String,
        handler: suspend (String, Int, JsonObject) -> NetworkMessageAction,
    ) {
        var requestId: String? = null
        val action = try {
            val message = json.parseToJsonElement(text) as? JsonObject
                ?: throw IllegalArgumentException("WebSocket message must be a JSON object")
            requestId = message["request_id"]?.jsonPrimitive?.contentOrNull
                ?.takeIf { it.isNotEmpty() }
                ?: throw IllegalArgumentException("request_id must be a non-empty string")
            if (message["kind"]?.jsonPrimitive?.contentOrNull != "request") {
                throw IllegalArgumentException("kind must be request")
            }
            val messageType = message["message_type"]?.jsonPrimitive?.contentOrNull
                ?.takeIf { it.isNotEmpty() }
                ?: throw IllegalArgumentException("message_type must be a non-empty string")
            val transactionId = message["transaction_id"]?.jsonPrimitive?.intOrNull
                ?: throw IllegalArgumentException("transaction_id must be an integer")
            val payload = message["payload"] as? JsonObject
                ?: throw IllegalArgumentException("payload must be a JSON object")
            handler(messageType, transactionId, payload)
        } catch (error: Exception) {
            Log.e(TAG, "Runtime downlink WebSocket request rejected", error)
            NetworkMessageAction.REJECT
        }
        val correlatedRequestId = requestId ?: return
        val response = buildJsonObject {
            put("kind", "response")
            put("request_id", correlatedRequestId)
            put("payload", buildJsonObject { put("result", action.name) })
        }
        if (!socket.send(response.toString())) {
            Log.w(TAG, "Runtime downlink WebSocket response queue is closed")
        }
    }

    override suspend fun registerEndpoint(
        localIp: String,
        tcpPort: Int,
        udpPort: Int,
    ): EndpointRegistration {
        val response = request("POST", "/sdk/v1/endpoints", buildJsonObject {
            put("local_vlan_ip", localIp)
            put("tcp_port", tcpPort)
            put("udp_port", udpPort)
        })
        val rawUeIp = response["ue_ip"]?.jsonPrimitive?.contentOrNull
            ?: throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "Endpoint registration has no ue_ip",
                "ue_ip",
            )
        val ueIp = normalizeIpLiteral(rawUeIp)
        val maxPrefix = if (ueIp.contains(':')) 128 else 32
        val uePrefixLength = response["ue_prefix_length"]?.jsonPrimitive?.intOrNull
            ?.takeIf { it in 0..maxPrefix }
            ?: throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "ue_prefix_length must be an integer in 0..$maxPrefix",
                "ue_prefix_length",
            )
        return EndpointRegistration(ueIp, uePrefixLength)
    }

    private fun normalizeIpLiteral(value: String): String {
        val ipv4Candidate = IPV4_LITERAL.matches(value)
        val ipv6Candidate = value.contains(':') &&
            value.all { it.isDigit() || it in ":abcdefABCDEF" }
        if (!ipv4Candidate && !ipv6Candidate) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "ue_ip must be an IP literal",
                "ue_ip",
            )
        }
        return try {
            val address = InetAddress.getByName(value)
            if ((ipv4Candidate && address.address.size != 4) ||
                (ipv6Candidate && address.address.size != 16)
            ) {
                throw IllegalArgumentException("IP address family mismatch")
            }
            address.hostAddress?.substringBefore('%')
                ?: throw IllegalArgumentException("IP address has no normalized representation")
        } catch (error: Exception) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "ue_ip must be an IP literal",
                "ue_ip",
                cause = error,
            )
        }
    }

    private companion object {
        const val TAG = "AgentSdkRuntime"
        val IPV4_LITERAL = Regex("(?:[0-9]{1,3}\\.){3}[0-9]{1,3}")
    }

    override suspend fun request(method: String, path: String, body: JsonObject): JsonObject =
        withContext(Dispatchers.IO) {
            val requestBody = body.toString().toRequestBody("application/json".toMediaType())
            val builder = Request.Builder().url(baseUrl + path)
            when (method.uppercase()) {
                "POST" -> builder.post(requestBody)
                "PUT" -> builder.put(requestBody)
                "PATCH" -> builder.patch(requestBody)
                else -> throw AgentSdkException(ErrorCode.INVALID_ARGUMENT, "Unsupported HTTP method")
            }
            try {
                client.newCall(builder.build()).execute().use { response ->
                    if (!response.isSuccessful) {
                        throw AgentSdkException(
                            ErrorCode.RUNTIME_REJECTED,
                            "Runtime returned HTTP ${response.code}",
                        )
                    }
                    val text = response.body?.string() ?: "{}"
                    json.parseToJsonElement(text) as? JsonObject
                        ?: throw AgentSdkException(
                            ErrorCode.RUNTIME_REJECTED,
                            "Runtime response must be a JSON object",
                        )
                }
            } catch (error: AgentSdkException) {
                throw error
            } catch (error: Exception) {
                throw AgentSdkException(
                    ErrorCode.RUNTIME_UNREACHABLE,
                    "Runtime request failed",
                    retryable = true,
                    cause = error,
                )
            }
        }

    override suspend fun close() {
        downlinkSocket?.close(1000, "SDK closed")
        downlinkSocket = null
        downlinkStarted.set(false)
        downlinkScope.cancel()
    }
}

class OkHttpPeerMessenger(
    private val baseClient: OkHttpClient = OkHttpClient(),
    private val json: Json = Json,
    private val scheme: String = "http",
) : PeerMessenger {
    override suspend fun send(
        ip: String,
        port: Int,
        body: JsonObject,
        timeoutMillis: Long,
    ): JsonObject = withContext(Dispatchers.IO) {
        val host = if (ip.contains(':')) "[$ip]" else ip
        val client = baseClient.newBuilder()
            .callTimeout(timeoutMillis, TimeUnit.MILLISECONDS)
            .build()
        val request = Request.Builder()
            .url("$scheme://$host:$port/A2A/message")
            .post(body.toString().toRequestBody("application/json".toMediaType()))
            .build()
        try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    throw AgentSdkException(
                        ErrorCode.MESSAGE_DELIVERY_FAILED,
                        "Peer returned HTTP ${response.code}",
                    )
                }
                json.parseToJsonElement(response.body?.string() ?: "{}") as? JsonObject
                    ?: throw AgentSdkException(
                        ErrorCode.MESSAGE_DELIVERY_FAILED,
                        "Peer response must be a JSON object",
                    )
            }
        } catch (error: AgentSdkException) {
            throw error
        } catch (error: Exception) {
            throw AgentSdkException(
                ErrorCode.MESSAGE_DELIVERY_FAILED,
                "A2A delivery failed",
                retryable = true,
                cause = error,
            )
        }
    }
}
