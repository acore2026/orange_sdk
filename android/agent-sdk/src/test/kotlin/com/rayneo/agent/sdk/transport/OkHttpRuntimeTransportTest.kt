package com.rayneo.agent.sdk.transport

import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import com.rayneo.agent.sdk.model.NetworkMessageAction
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.net.SocketException

class OkHttpRuntimeTransportTest {
    @Test
    fun `Sandbox transport uploads multipart audio and standalone ASR needs no UE address`() = runTest {
        val server = MockWebServer()
        server.enqueue(
            MockResponse().setResponseCode(200).setBody("""{"text":"向左移动"}"""),
        )
        server.start()
        val transport = OkHttpSandboxTransport()
        try {
            val response = transport.uploadWithStatus(
                url = server.url("/api/v1/transcribe").toString(),
                fields = mapOf("language" to "zh", "source" to "glasses"),
                fileFieldName = "file",
                fileName = "speech.m4a",
                contentType = "audio/mp4",
                content = byteArrayOf(1, 2, 3),
                timeoutSeconds = 2.0,
                sourceIpv4 = null,
            )
            assertEquals(200, response.statusCode)
            val request = server.takeRequest(2, TimeUnit.SECONDS)!!
            val body = request.body.readUtf8()
            assertTrue(request.getHeader("Content-Type")!!.startsWith("multipart/form-data;"))
            assertTrue(body.contains("name=\"language\""))
            assertTrue(body.contains("zh"))
            assertTrue(body.contains("filename=\"speech.m4a\""))
        } finally {
            transport.close()
            server.shutdown()
        }
    }

    @Test
    fun `Sandbox transport supports Sandbox resource methods`() = runTest {
        val server = MockWebServer()
        server.enqueue(
            MockResponse()
                .setResponseCode(201)
                .setHeader("Content-Type", "application/json")
                .setBody("""{"media_connection_id":"media-001"}"""),
        )
        server.enqueue(MockResponse().setResponseCode(204))
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody("""{"status":"APPLIED"}"""),
        )
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody("""{"status":"APPLIED"}"""),
        )
        server.start()
        val transport = OkHttpSandboxTransport()
        try {
            val body = buildJsonObject { put("request_id", "media-request-001") }
            val created = transport.requestWithStatus(
                "POST",
                server.url("/v1/media-connections").toString(),
                body,
                2.0,
                "127.0.0.1",
            )
            assertEquals(201, created.statusCode)
            assertEquals("media-001", created.body["media_connection_id"]!!.jsonPrimitive.content)
            val post = server.takeRequest(2, TimeUnit.SECONDS)!!
            assertEquals("POST", post.method)
            assertEquals("/v1/media-connections", post.path)
            assertEquals(body, Json.parseToJsonElement(post.body.readUtf8()))

            val deleted = transport.requestWithStatus(
                "DELETE",
                server.url("/v1/media-connections/media-001").toString(),
                null,
                2.0,
                "127.0.0.1",
            )
            assertEquals(204, deleted.statusCode)
            val delete = server.takeRequest(2, TimeUnit.SECONDS)!!
            assertEquals("DELETE", delete.method)
            assertEquals(0L, delete.bodySize)

            val recognitionBody = buildJsonObject { put("request_id", "recognition-001") }
            val updated = transport.requestWithStatus(
                "PUT",
                server.url("/v1/recognition-targets/css-001").toString(),
                recognitionBody,
                2.0,
                "127.0.0.1",
            )
            assertEquals(200, updated.statusCode)
            val put = server.takeRequest(2, TimeUnit.SECONDS)!!
            assertEquals("PUT", put.method)
            assertEquals(recognitionBody, Json.parseToJsonElement(put.body.readUtf8()))

            val fetched = transport.requestWithStatus(
                "GET",
                server.url("/v1/recognition-targets/css-001").toString(),
                null,
                2.0,
                "127.0.0.1",
            )
            assertEquals(200, fetched.statusCode)
            val get = server.takeRequest(2, TimeUnit.SECONDS)!!
            assertEquals("GET", get.method)
            assertEquals(0L, get.bodySize)
        } finally {
            transport.close()
            server.shutdown()
        }
    }

    @Test
    fun `UE info uses exact GET and returns active default PDU IPv4`() = runTest {
        val server = MockWebServer()
        server.enqueue(MockResponse()
            .setResponseCode(200)
            .setHeader("Content-Type", "application/json")
            .setBody(UE_INFO_JSON))
        server.start()
        val transport = OkHttpRuntimeTransport(server.hostName, server.port)
        try {
            assertEquals("10.60.0.11", transport.getUeAgentIp())
            val request = server.takeRequest(2, TimeUnit.SECONDS)!!
            assertEquals("GET", request.method)
            assertEquals(UE_INFO_PATH, request.path)
            assertEquals(0L, request.bodySize)
            assertEquals("application/json", request.getHeader("Content-Type"))
        } finally {
            transport.close()
            server.shutdown()
        }
    }

    @Test
    fun `UE info rejects inactive PDU Session`() = runTest {
        val server = MockWebServer()
        server.enqueue(MockResponse()
            .setResponseCode(200)
            .setHeader("Content-Type", "application/json")
            .setBody(UE_INFO_JSON.replace("\"active\"", "\"inactive\"")))
        server.start()
        val transport = OkHttpRuntimeTransport(server.hostName, server.port)
        try {
            val error = runCatching { transport.getUeAgentIp() }
                .exceptionOrNull() as AgentSdkException
            assertEquals(ErrorCode.RUNTIME_REJECTED, error.code)
            assertEquals("pdu_sessions", error.field)
        } finally {
            transport.close()
            server.shutdown()
        }
    }

    @Test
    fun `request with status preserves C04 on HTTP 422`() = runTest {
        val server = MockWebServer()
        server.enqueue(
            MockResponse()
                .setResponseCode(422)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """{"message_type":"COMPUTE_SESSION_STATUS","request_id":"create-001","status":"CLARIFICATION_REQUIRED","cause":"missing-capability","missing_fields":["constraints.capability_id"]}""",
                ),
        )
        server.start()
        val transport = OkHttpRuntimeTransport(server.hostName, server.port)
        try {
            val response = transport.requestWithStatus(
                "POST",
                "/v1/computing/session-requests",
                buildJsonObject { put("request_id", "create-001") },
            )
            assertEquals(422, response.statusCode)
            assertEquals(
                "CLARIFICATION_REQUIRED",
                response.body["status"]!!.jsonPrimitive.content,
            )
        } finally {
            transport.close()
            server.shutdown()
        }
    }

    @Test
    fun `runtime request cancellation closes a pending HTTP call`() = runTest {
        val server = MockWebServer()
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))
        server.start()
        val transport = OkHttpRuntimeTransport(server.hostName, server.port)
        try {
            val request = async {
                transport.requestWithStatus(
                    "POST",
                    "/v1/computing/session-requests",
                    buildJsonObject { put("request_id", "create-001") },
                    timeoutSeconds = 30.0,
                )
            }
            assertTrue(
                withContext(Dispatchers.IO) {
                    server.takeRequest(2, TimeUnit.SECONDS) != null
                },
            )

            request.cancelAndJoin()

            assertTrue(request.isCancelled)
        } finally {
            transport.close()
            server.shutdown()
        }
    }

    @Test
    fun `peer messenger retries a replayable A2A request after socket replacement`() = runTest {
        val server = MockWebServer()
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody("""{"status":"OK"}"""),
        )
        server.start()
        val attempts = AtomicInteger()
        val client = okhttp3.OkHttpClient.Builder()
            .retryOnConnectionFailure(false)
            .addInterceptor { chain ->
                if (attempts.incrementAndGet() == 1) {
                    throw SocketException("Socket closed during TUN replacement")
                }
                chain.proceed(chain.request())
            }
            .build()
        val messenger = OkHttpPeerMessenger(client)
        try {
            val response = messenger.send(
                server.url("/A2A/message").toString(),
                buildJsonObject { put("message_id", "message-001") },
                5_000,
            )

            assertEquals("OK", response["status"]!!.jsonPrimitive.content)
            assertEquals(2, attempts.get())
            assertEquals(1, server.requestCount)
        } finally {
            server.shutdown()
        }
    }

    @Test
    fun `downlink status event has no websocket response`() = runTest {
        val server = MockWebServer()
        val opened = CompletableDeferred<WebSocket>()
        val delivered = CompletableDeferred<Unit>()
        val responses = LinkedBlockingQueue<String>()
        server.enqueue(MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                opened.complete(webSocket)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                responses.put(text)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, reason)
            }
        }))
        server.start()
        val transport = OkHttpRuntimeTransport(server.hostName, server.port)
        try {
            transport.startDownlink { messageType, transactionId, _ ->
                assertEquals("COMPUTE_SESSION_STATUS", messageType)
                assertEquals(34, transactionId)
                delivered.complete(Unit)
                null
            }
            opened.await().send(
                """{"kind":"event","message_type":"COMPUTE_SESSION_STATUS","transaction_id":34,"payload":{"request_id":"create-001","status":"ACCEPTED","cause":""}}""",
            )
            delivered.await()
            assertNull(responses.poll(200, TimeUnit.MILLISECONDS))
        } finally {
            transport.close()
            server.shutdown()
        }
    }

    @Test
    fun `downlink websocket uses runtime port and allows out of order responses`() = runTest {
        val server = MockWebServer()
        val opened = CompletableDeferred<WebSocket>()
        val releaseFirst = CompletableDeferred<Unit>()
        val responses = LinkedBlockingQueue<String>()
        server.enqueue(MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                opened.complete(webSocket)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                responses.put(text)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, reason)
            }
        }))
        server.start()
        val transport = OkHttpRuntimeTransport(server.hostName, server.port)
        try {
            transport.startDownlink { _, _, payload ->
                if (payload["sequence"]!!.jsonPrimitive.int == 1) {
                    releaseFirst.await()
                    buildJsonObject {
                        put("group_id", "group-invitation")
                        put("result", NetworkMessageAction.ACCEPT.name)
                    }
                } else {
                    buildJsonObject {
                        put("group_id", "group-config")
                        put("result", NetworkMessageAction.ACK.name)
                    }
                }
            }
            val serverSocket = opened.await()
            val upgrade = server.takeRequest(2, TimeUnit.SECONDS)!!
            assertEquals(DOWNLINK_WEBSOCKET_PATH, upgrade.path)
            assertEquals("websocket", upgrade.getHeader("Upgrade")?.lowercase())

            serverSocket.send(downlinkRequest(
                requestId = "delivery-1",
                messageType = "ACN_AGENT_GROUPING_INVITATION",
                transactionId = 49,
                groupId = "group-invitation",
                sequence = 1,
            ))
            serverSocket.send(downlinkRequest(
                requestId = "delivery-2",
                messageType = "ACN_AGENT_GROUPING_NOTIFICATION",
                transactionId = 50,
                groupId = "group-config",
                sequence = 2,
            ))
            assertEquals(
                buildJsonObject {
                    put("kind", "response")
                    put("request_id", "delivery-2")
                    put("payload", buildJsonObject {
                        put("group_id", "group-config")
                        put("result", "ACK")
                    })
                },
                Json.parseToJsonElement(responses.poll(2, TimeUnit.SECONDS)),
            )
            releaseFirst.complete(Unit)
            assertEquals(
                buildJsonObject {
                    put("kind", "response")
                    put("request_id", "delivery-1")
                    put("payload", buildJsonObject {
                        put("group_id", "group-invitation")
                        put("result", "ACCEPT")
                    })
                },
                Json.parseToJsonElement(responses.poll(2, TimeUnit.SECONDS)),
            )
            assertTrue(upgrade.requestUrl?.port == server.port)
        } finally {
            releaseFirst.complete(Unit)
            transport.close()
            server.shutdown()
        }
    }

    @Test
    fun `downlink websocket reconnects after an unexpected server close`() = runTest {
        val server = MockWebServer()
        val firstOpened = CompletableDeferred<WebSocket>()
        val secondOpened = LinkedBlockingQueue<WebSocket>()
        val responses = LinkedBlockingQueue<String>()
        server.enqueue(MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                firstOpened.complete(webSocket)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, reason)
            }
        }))
        server.enqueue(MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                secondOpened.put(webSocket)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                responses.put(text)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, reason)
            }
        }))
        server.start()
        val transport = OkHttpRuntimeTransport(
            server.hostName,
            server.port,
            reconnectInitialDelayMillis = 10,
            reconnectMaxDelayMillis = 20,
        )
        try {
            transport.startDownlink { _, _, payload ->
                buildJsonObject {
                    put("group_id", payload["group_id"]!!)
                    put("result", NetworkMessageAction.ACK.name)
                }
            }
            firstOpened.await().close(1011, "simulated runtime restart")
            val reconnected = checkNotNull(secondOpened.poll(2, TimeUnit.SECONDS)) {
                "downlink WebSocket did not reconnect"
            }

            reconnected.send(downlinkRequest(
                requestId = "delivery-after-reconnect",
                messageType = "ACN_AGENT_GROUPING_NOTIFICATION",
                transactionId = 51,
                groupId = "group-after-reconnect",
                sequence = 3,
            ))
            assertEquals(
                buildJsonObject {
                    put("kind", "response")
                    put("request_id", "delivery-after-reconnect")
                    put("payload", buildJsonObject {
                        put("group_id", "group-after-reconnect")
                        put("result", "ACK")
                    })
                },
                Json.parseToJsonElement(responses.poll(2, TimeUnit.SECONDS)),
            )
            assertEquals(DOWNLINK_WEBSOCKET_PATH, server.takeRequest(2, TimeUnit.SECONDS)?.path)
            assertEquals(DOWNLINK_WEBSOCKET_PATH, server.takeRequest(2, TimeUnit.SECONDS)?.path)
        } finally {
            transport.close()
            server.shutdown()
        }
    }

    private fun downlinkRequest(
        requestId: String,
        messageType: String,
        transactionId: Int,
        groupId: String,
        sequence: Int,
    ) =
        buildJsonObject {
            put("kind", "request")
            put("request_id", requestId)
            put("message_type", messageType)
            put("transaction_id", transactionId)
            put("payload", buildJsonObject {
                if (messageType == "ACN_AGENT_GROUPING_INVITATION") {
                    put("group_info", buildJsonObject {
                        put("target_agent_id", "agent-b")
                        put("group_id", groupId)
                        put("group_name", "task-patrol")
                    })
                    put("group_administrator", buildJsonObject {
                        put("agent_id", "agent-a")
                    })
                } else {
                    put("group_id", groupId)
                }
                put("sequence", sequence)
            })
        }.toString()

    private companion object {
        val UE_INFO_JSON = """
            {
              "identity": {
                "supi": "imsi-001010000000001",
                "imei": "356938035643803",
                "imeisv": "3569380356438031"
              },
              "serving_plmn": {"mcc": "001", "mnc": "01"},
              "nas": {
                "state": "session_ready",
                "registered": true,
                "security_context": true
              },
              "pdu_sessions": [{
                "pdu_session_id": 1,
                "state": "active",
                "dnn": "internet",
                "type": "IPv4",
                "snssai": {"sst": 1, "sd": "010203"},
                "ssc_mode": 1,
                "ipv4": "10.60.0.11",
                "auto_establish": true,
                "default_route": true
              }]
            }
        """.trimIndent()
    }
}
