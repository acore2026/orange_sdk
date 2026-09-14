package com.rayneo.agent.sdk.transport

import android.util.Log
import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import com.rayneo.agent.sdk.model.NetworkMessageAction
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Dns
import okhttp3.Call
import okhttp3.Callback
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.RequestBody.Companion.toRequestBody
import java.net.InetAddress
import java.net.Proxy
import java.io.IOException
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

const val DOWNLINK_WEBSOCKET_PATH = "/v1/acn/downlink-websocket"
const val UE_INFO_PATH = "/v1/ue/info"
const val ACN_STATUS_PATH = "/v1/acn/status"

internal class OkHttpSandboxTransport(
    private val baseClient: OkHttpClient = OkHttpClient(),
    private val json: Json = Json,
) : SandboxTransport {
    override suspend fun requestWithStatus(
        method: String,
        url: String,
        body: JsonObject?,
        timeoutSeconds: Double,
        sourceIpv4: String,
    ): RuntimeHttpResponse = withContext(Dispatchers.IO) {
        if (sourceIpv4.isBlank()) {
            throw AgentSdkException(ErrorCode.INVALID_ARGUMENT, "sourceIpv4 is required")
        }
        val timeoutMillis = (timeoutSeconds * 1_000).toLong().coerceAtLeast(1)
        val client = baseClient.newBuilder()
            .callTimeout(timeoutMillis, TimeUnit.MILLISECONDS)
            .proxy(Proxy.NO_PROXY)
            .dns(object : Dns {
                override fun lookup(hostname: String): List<InetAddress> =
                    Dns.SYSTEM.lookup(hostname).filter { it.address.size == 4 }
                        .ifEmpty {
                            throw java.net.UnknownHostException(
                                "No IPv4 address for $hostname",
                            )
                        }
            })
            .build()
        val builder = Request.Builder().url(url)
        val requestBody = body?.toString()?.toRequestBody("application/json".toMediaType())
        when (method.uppercase()) {
            "POST" -> builder.post(checkNotNull(requestBody))
            "PUT" -> builder.put(checkNotNull(requestBody))
            "GET" -> builder.get()
            "DELETE" -> builder.delete()
            else -> throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "Unsupported Sandbox HTTP method $method",
            )
        }
        try {
            client.newCall(builder.build()).execute().use { response ->
                val text = response.body?.string().orEmpty()
                val payload = if (text.isBlank()) {
                    JsonObject(emptyMap())
                } else {
                    json.parseToJsonElement(text) as? JsonObject
                        ?: throw AgentSdkException(
                            ErrorCode.MEDIA_NEGOTIATION_FAILED,
                            "Sandbox response must be a JSON object",
                        )
                }
                RuntimeHttpResponse(response.code, payload)
            }
        } catch (error: AgentSdkException) {
            throw error
        } catch (error: java.net.SocketTimeoutException) {
            throw AgentSdkException(
                ErrorCode.TIMEOUT,
                "Sandbox media request timed out",
                retryable = true,
                cause = error,
            )
        } catch (error: Exception) {
            throw AgentSdkException(
                ErrorCode.MEDIA_NEGOTIATION_FAILED,
                "Sandbox media request failed",
                retryable = true,
                cause = error,
            )
        }
    }

    override suspend fun close() {
        baseClient.dispatcher.executorService.shutdown()
        baseClient.connectionPool.evictAll()
    }
}

class OkHttpRuntimeTransport(
    host: String,
    port: Int,
    private val client: OkHttpClient = OkHttpClient(),
    private val json: Json = Json,
    private val reconnectInitialDelayMillis: Long = 1_000,
    private val reconnectMaxDelayMillis: Long = 30_000,
) : RuntimeTransport {
    private val baseUrl = "http://$host:$port"
    private val downlinkScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val downlinkClient = client.newBuilder()
        .pingInterval(DOWNLINK_PING_INTERVAL_SECONDS, TimeUnit.SECONDS)
        .build()
    private val downlinkStarted = AtomicBoolean(false)
    private val downlinkClosed = AtomicBoolean(false)
    private val downlinkLock = Any()
    @Volatile private var downlinkSocket: WebSocket? = null
    @Volatile private var downlinkHandler:
        (suspend (String, Int, JsonObject) -> JsonObject?)? = null
    @Volatile private var downlinkReconnectHandler: (suspend () -> Unit)? = null
    private var reconnectJob: Job? = null
    private var reconnectAttempt = 0

    override suspend fun getUeInfo(): JsonObject = getJson(UE_INFO_PATH)

    override suspend fun getAcnStatus(): JsonObject = getJson(ACN_STATUS_PATH)

    suspend fun getUeAgentIp(): String = selectDefaultUeAgentIp(getUeInfo())

    internal fun selectUeAgentIp(payload: JsonObject): String {
        return selectDefaultUeAgentIp(payload)
    }

    private suspend fun getJson(path: String): JsonObject = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url(baseUrl + path)
            .header("Content-Type", "application/json")
            .get()
            .build()
        val payload = try {
            Log.i(TAG, "GET $path")
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    throw AgentSdkException(
                        ErrorCode.RUNTIME_REJECTED,
                        "Runtime returned HTTP ${response.code} for $path",
                    )
                }
                json.parseToJsonElement(response.body?.string() ?: "{}") as? JsonObject
                    ?: throw AgentSdkException(
                        ErrorCode.RUNTIME_REJECTED,
                        "GET $path response must be a JSON object",
                    )
            }
        } catch (error: AgentSdkException) {
            throw error
        } catch (error: Exception) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_UNREACHABLE,
                "GET $path failed",
                retryable = true,
                cause = error,
            )
        }
        payload
    }

    private fun selectDefaultUeAgentIp(payload: JsonObject): String {
        val nas = payload["nas"] as? JsonObject
            ?: ueInfoRejected("response has no valid nas object", "nas")
        if (nas.boolean("registered") != true) {
            ueInfoRejected("UERANSIM UE is not registered", "nas.registered", true)
        }
        if (nas.string("state") != "session_ready") {
            ueInfoRejected("UERANSIM NAS state is not session_ready", "nas.state", true)
        }
        if (nas.boolean("security_context") != true) {
            ueInfoRejected(
                "UERANSIM NAS security context is not ready",
                "nas.security_context",
                true,
            )
        }

        val sessions = payload["pdu_sessions"] as? JsonArray
            ?: ueInfoRejected("response has no valid pdu_sessions array", "pdu_sessions")
        val activeIpv4 = mutableListOf<Pair<String, Boolean>>()
        sessions.forEach { element ->
            val session = element as? JsonObject ?: return@forEach
            if (session.string("state") != "active" || session.string("type") != "IPv4") {
                return@forEach
            }
            val rawIpv4 = session.string("ipv4")
                ?: ueInfoRejected(
                    "active IPv4 PDU Session has no ipv4 address",
                    "pdu_sessions.ipv4",
                )
            activeIpv4 += normalizeIpv4(rawIpv4) to
                (session.boolean("default_route") == true)
        }
        val defaults = activeIpv4.filter { it.second }.map { it.first }
        if (defaults.size != 1) {
            ueInfoRejected(
                "exactly one active default IPv4 PDU Session is required",
                "pdu_sessions",
                true,
            )
        }
        return defaults.single()
    }

    private fun normalizeIpv4(value: String): String {
        if (!IPV4_LITERAL.matches(value)) {
            ueInfoRejected("PDU Session ipv4 must be an IPv4 literal", "pdu_sessions.ipv4")
        }
        val address = try {
            InetAddress.getByName(value)
        } catch (error: Exception) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "PDU Session ipv4 must be an IPv4 literal",
                "pdu_sessions.ipv4",
                cause = error,
            )
        }
        if (address.address.size != 4) {
            ueInfoRejected("PDU Session ipv4 must be an IPv4 literal", "pdu_sessions.ipv4")
        }
        return address.hostAddress
            ?: ueInfoRejected("PDU Session ipv4 has no normalized form", "pdu_sessions.ipv4")
    }

    private fun JsonObject.string(field: String): String? =
        (this[field] as? JsonPrimitive)?.contentOrNull

    private fun JsonObject.boolean(field: String): Boolean? =
        (this[field] as? JsonPrimitive)?.booleanOrNull

    private fun ueInfoRejected(
        message: String,
        field: String,
        retryable: Boolean = false,
    ): Nothing = throw AgentSdkException(
        ErrorCode.RUNTIME_REJECTED,
        "GET $UE_INFO_PATH $message",
        field,
        retryable,
    )

    override suspend fun startDownlink(
        onReconnected: suspend () -> Unit,
        handler: suspend (String, Int, JsonObject) -> JsonObject?,
    ) {
        if (!downlinkStarted.compareAndSet(false, true)) {
            throw AgentSdkException(
                ErrorCode.INVALID_ARGUMENT,
                "Runtime downlink WebSocket is already started",
            )
        }
        require(reconnectInitialDelayMillis >= 0) {
            "reconnectInitialDelayMillis must not be negative"
        }
        require(reconnectMaxDelayMillis >= reconnectInitialDelayMillis) {
            "reconnectMaxDelayMillis must be at least reconnectInitialDelayMillis"
        }
        downlinkClosed.set(false)
        downlinkHandler = handler
        downlinkReconnectHandler = onReconnected
        try {
            suspendCancellableCoroutine<Unit> { continuation ->
                val socket = connectDownlink(
                    handler,
                    onReconnected,
                    continuation,
                    attempt = 0,
                )
                continuation.invokeOnCancellation { socket.cancel() }
            }
        } catch (error: Exception) {
            downlinkStarted.set(false)
            downlinkHandler = null
            throw error
        }
    }

    private fun connectDownlink(
        handler: suspend (String, Int, JsonObject) -> JsonObject?,
        onReconnected: suspend () -> Unit,
        initialContinuation: kotlinx.coroutines.CancellableContinuation<Unit>?,
        attempt: Int,
    ): WebSocket {
        val request = Request.Builder()
            .url(baseUrl + DOWNLINK_WEBSOCKET_PATH)
            .get()
            .build()
        val opened = AtomicBoolean(false)
        Log.i(
            TAG,
            if (attempt == 0) {
                "Runtime downlink WebSocket connecting url=${request.url}"
            } else {
                "Runtime downlink WebSocket reconnecting attempt=$attempt url=${request.url}"
            },
        )
        val socket = downlinkClient.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                opened.set(true)
                if (downlinkClosed.get()) {
                    webSocket.close(NORMAL_CLOSE_CODE, "SDK closed")
                    return
                }
                synchronized(downlinkLock) { downlinkSocket = webSocket }
                Log.i(
                    TAG,
                    if (attempt == 0) {
                        "Runtime downlink WebSocket connected http=${response.code}"
                    } else {
                        "Runtime downlink WebSocket reconnected attempt=$attempt http=${response.code}"
                    },
                )
                if (attempt > 0) {
                    downlinkScope.launch { onReconnected() }
                }
                if (initialContinuation?.isActive == true) initialContinuation.resume(Unit)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                synchronized(downlinkLock) { reconnectAttempt = 0 }
                Log.d(TAG, "Runtime downlink WebSocket frame received bytes=${text.length}")
                // Start inline until the handler first suspends. Compute lifecycle handlers
                // therefore enter their FIFO mutex in WebSocket frame order while independent
                // group callbacks can still complete out of order.
                downlinkScope.launch(start = CoroutineStart.UNDISPATCHED) {
                    processDownlinkFrame(webSocket, text, handler)
                }
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                Log.w(
                    TAG,
                    "Runtime downlink WebSocket closing code=$code reason=${reason.take(200)}",
                )
                webSocket.close(code, reason)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                clearDownlinkSocket(webSocket)
                if (!downlinkClosed.get()) {
                    Log.w(
                        TAG,
                        "Runtime downlink WebSocket closed unexpectedly code=$code " +
                            "reason=${reason.take(200)}",
                    )
                    scheduleDownlinkReconnect("closed code=$code")
                } else {
                    Log.i(TAG, "Runtime downlink WebSocket closed code=$code")
                }
            }

            override fun onFailure(
                webSocket: WebSocket,
                t: Throwable,
                response: Response?,
            ) {
                clearDownlinkSocket(webSocket)
                val detail = throwableSummary(t)
                Log.e(
                    TAG,
                    "Runtime downlink WebSocket failed http=${response?.code ?: "none"} " +
                        "error=$detail",
                    t,
                )
                if (!opened.get() && initialContinuation?.isActive == true) {
                    initialContinuation.resumeWithException(
                        AgentSdkException(
                            ErrorCode.RUNTIME_UNREACHABLE,
                            "AgentRuntime downlink WebSocket is unreachable: $detail",
                            retryable = true,
                            cause = t,
                        )
                    )
                } else if (!downlinkClosed.get()) {
                    scheduleDownlinkReconnect("failure $detail")
                }
            }
        })
        synchronized(downlinkLock) {
            if (downlinkClosed.get()) socket.cancel() else downlinkSocket = socket
        }
        return socket
    }

    private fun clearDownlinkSocket(socket: WebSocket) {
        synchronized(downlinkLock) {
            if (downlinkSocket === socket) downlinkSocket = null
        }
    }

    private fun scheduleDownlinkReconnect(reason: String) {
        val handler = downlinkHandler ?: return
        val attempt: Int
        val delayMillis: Long
        synchronized(downlinkLock) {
            if (downlinkClosed.get() || reconnectJob?.isActive == true) return
            reconnectAttempt += 1
            attempt = reconnectAttempt
            delayMillis = reconnectDelayMillis(attempt)
            reconnectJob = downlinkScope.launch {
                Log.w(
                    TAG,
                    "Runtime downlink WebSocket reconnect scheduled attempt=$attempt " +
                        "delay_ms=$delayMillis reason=${reason.take(240)}",
                )
                delay(delayMillis)
                if (!downlinkClosed.get()) {
                    connectDownlink(
                        handler,
                        downlinkReconnectHandler ?: {},
                        initialContinuation = null,
                        attempt = attempt,
                    )
                }
            }
        }
    }

    private fun reconnectDelayMillis(attempt: Int): Long {
        if (reconnectInitialDelayMillis == 0L) return 0L
        var result = reconnectInitialDelayMillis
        repeat((attempt - 1).coerceAtMost(30)) {
            if (result >= reconnectMaxDelayMillis / 2) return reconnectMaxDelayMillis
            result *= 2
        }
        return result.coerceAtMost(reconnectMaxDelayMillis)
    }

    private fun throwableSummary(error: Throwable): String {
        val root = generateSequence(error) { it.cause }.last()
        val message = root.message?.takeIf(String::isNotBlank) ?: "no message"
        return "${root::class.java.simpleName}: $message"
    }

    private suspend fun processDownlinkFrame(
        socket: WebSocket,
        text: String,
        handler: suspend (String, Int, JsonObject) -> JsonObject?,
    ) {
        var requestId: String? = null
        var kind: String? = null
        val responsePayload = try {
            val message = json.parseToJsonElement(text) as? JsonObject
                ?: throw IllegalArgumentException("WebSocket message must be a JSON object")
            kind = (message["kind"] as? JsonPrimitive)
                ?.takeIf(JsonPrimitive::isString)?.contentOrNull
            if (kind !in setOf("request", "event")) {
                throw IllegalArgumentException("kind must be request or event")
            }
            val rawRequestId = (message["request_id"] as? JsonPrimitive)
                ?.takeIf(JsonPrimitive::isString)?.contentOrNull
            if (kind == "request") {
                requestId = rawRequestId?.takeIf { it.isNotEmpty() }
                    ?: throw IllegalArgumentException(
                        "request_id must be a non-empty string for request",
                    )
            } else if (rawRequestId != null) {
                throw IllegalArgumentException("request_id must be omitted for event")
            }
            val messageType = (message["message_type"] as? JsonPrimitive)
                ?.takeIf(JsonPrimitive::isString)?.contentOrNull
                ?.takeIf { it.isNotEmpty() }
                ?: throw IllegalArgumentException("message_type must be a non-empty string")
            val transactionId = message["transaction_id"]?.jsonPrimitive?.intOrNull
                ?: throw IllegalArgumentException("transaction_id must be an integer")
            if (transactionId !in 1..255) {
                throw IllegalArgumentException("transaction_id must be in 1..255")
            }
            val payload = message["payload"] as? JsonObject
                ?: throw IllegalArgumentException("payload must be a JSON object")
            handler(messageType, transactionId, payload)
        } catch (error: Exception) {
            Log.e(TAG, "Runtime downlink WebSocket request rejected", error)
            buildJsonObject { put("result", NetworkMessageAction.REJECT.name) }
        }
        if (kind != "request") return
        val correlatedRequestId = requestId ?: return
        val response = buildJsonObject {
            put("kind", "response")
            put("request_id", correlatedRequestId)
            put("payload", responsePayload ?: buildJsonObject {
                put("result", NetworkMessageAction.REJECT.name)
            })
        }
        if (!socket.send(response.toString())) {
            Log.w(TAG, "Runtime downlink WebSocket response queue is closed")
        }
    }

    private companion object {
        const val TAG = "AgentSdkRuntime"
        const val NORMAL_CLOSE_CODE = 1000
        const val DOWNLINK_PING_INTERVAL_SECONDS = 20L
        val IPV4_LITERAL = Regex("(?:[0-9]{1,3}\\.){3}[0-9]{1,3}")
    }

    override suspend fun requestWithStatus(
        method: String,
        path: String,
        body: JsonObject,
    ): RuntimeHttpResponse = requestWithStatus(method, path, body, 10.0)

    override suspend fun requestWithStatus(
        method: String,
        path: String,
        body: JsonObject,
        timeoutSeconds: Double,
    ): RuntimeHttpResponse {
        val requestBody = body.toString().toRequestBody("application/json".toMediaType())
        val builder = Request.Builder().url(baseUrl + path)
        when (method.uppercase()) {
            "POST" -> builder.post(requestBody)
            "PUT" -> builder.put(requestBody)
            "PATCH" -> builder.patch(requestBody)
            else -> throw AgentSdkException(ErrorCode.INVALID_ARGUMENT, "Unsupported HTTP method")
        }
        return suspendCancellableCoroutine { continuation ->
            val call = client.newCall(builder.build()).also {
                it.timeout().timeout(
                    (timeoutSeconds * 1000.0).toLong().coerceAtLeast(1L),
                    TimeUnit.MILLISECONDS,
                )
            }
            continuation.invokeOnCancellation { call.cancel() }
            call.enqueue(object : Callback {
                override fun onFailure(call: Call, error: IOException) {
                    val failure = AgentSdkException(
                        ErrorCode.RUNTIME_UNREACHABLE,
                        "Runtime request failed",
                        retryable = true,
                        cause = error,
                    )
                    continuation.resumeWithException(failure)
                }

                override fun onResponse(call: Call, response: Response) {
                    val result = runCatching {
                        response.use {
                            val text = response.body?.string() ?: "{}"
                            val payload = json.parseToJsonElement(text) as? JsonObject
                                ?: throw AgentSdkException(
                                    ErrorCode.RUNTIME_REJECTED,
                                    "Runtime response must be a JSON object",
                                )
                            RuntimeHttpResponse(response.code, payload)
                        }
                    }
                    result.onSuccess { value ->
                        continuation.resume(value)
                    }.onFailure { error ->
                        val failure = if (error is AgentSdkException) error else AgentSdkException(
                            ErrorCode.RUNTIME_UNREACHABLE,
                            "Runtime request failed",
                            retryable = true,
                            cause = error,
                        )
                        continuation.resumeWithException(failure)
                    }
                }
            })
        }
    }

    override suspend fun request(method: String, path: String, body: JsonObject): JsonObject {
        val response = requestWithStatus(method, path, body)
        if (response.statusCode !in 200..299) {
            throw AgentSdkException(
                ErrorCode.RUNTIME_REJECTED,
                "Runtime returned HTTP ${response.statusCode}",
            )
        }
        return response.body
    }

    override suspend fun close() {
        downlinkClosed.set(true)
        val socket = synchronized(downlinkLock) {
            reconnectJob?.cancel()
            reconnectJob = null
            downlinkSocket.also { downlinkSocket = null }
        }
        socket?.close(NORMAL_CLOSE_CODE, "SDK closed")
        downlinkStarted.set(false)
        downlinkHandler = null
        downlinkReconnectHandler = null
        downlinkScope.cancel()
    }
}

class OkHttpPeerMessenger(
    private val baseClient: OkHttpClient = OkHttpClient(),
    private val json: Json = Json,
) : PeerMessenger {
    override suspend fun send(
        endpoint: String,
        body: JsonObject,
        timeoutMillis: Long,
    ): JsonObject = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url(endpoint)
            .post(body.toString().toRequestBody("application/json".toMediaType()))
            .build()
        val deadlineNanos = System.nanoTime() +
            TimeUnit.MILLISECONDS.toNanos(timeoutMillis.coerceAtLeast(1))
        var attempt = 0
        var delivered: JsonObject? = null
        while (delivered == null) {
            attempt += 1
            val remainingMillis = TimeUnit.NANOSECONDS.toMillis(
                (deadlineNanos - System.nanoTime()).coerceAtLeast(1),
            ).coerceAtLeast(1)
            val attemptTimeoutMillis = minOf(remainingMillis, A2A_ATTEMPT_TIMEOUT_MILLIS)
            val client = baseClient.newBuilder()
                .callTimeout(attemptTimeoutMillis, TimeUnit.MILLISECONDS)
                .build()
            try {
                delivered = client.newCall(request).execute().use { response ->
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
            } catch (error: CancellationException) {
                throw error
            } catch (error: AgentSdkException) {
                throw error
            } catch (error: Exception) {
                val root = generateSequence(error as Throwable) { it.cause }.last()
                val retryDelayMillis = A2A_RETRY_DELAYS_MILLIS.getOrNull(attempt - 1)
                val remainingAfterFailure = TimeUnit.NANOSECONDS.toMillis(
                    (deadlineNanos - System.nanoTime()).coerceAtLeast(0),
                )
                if (
                    root !is IOException || retryDelayMillis == null ||
                    remainingAfterFailure <= retryDelayMillis
                ) {
                    val detail = root.message?.takeIf(String::isNotBlank) ?: "no message"
                    throw AgentSdkException(
                        ErrorCode.MESSAGE_DELIVERY_FAILED,
                        "A2A delivery failed after $attempt attempt(s): " +
                            "${root::class.java.simpleName}: $detail",
                        retryable = root is IOException,
                        cause = error,
                    )
                }
                Log.w(
                    TAG_PEER,
                    "A2A transport attempt=$attempt failed; retrying after " +
                        "${retryDelayMillis}ms error=${root::class.java.simpleName}",
                )
                delay(retryDelayMillis)
            }
        }
        checkNotNull(delivered)
    }

    private companion object {
        const val TAG_PEER = "AgentSdkPeer"
        const val A2A_ATTEMPT_TIMEOUT_MILLIS = 2_000L
        val A2A_RETRY_DELAYS_MILLIS = longArrayOf(100L, 250L, 500L, 1_000L)
    }
}
