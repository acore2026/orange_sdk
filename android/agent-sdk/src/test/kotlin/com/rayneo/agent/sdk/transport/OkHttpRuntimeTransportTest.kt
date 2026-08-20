package com.rayneo.agent.sdk.transport

import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import com.rayneo.agent.sdk.model.NetworkMessageAction
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.ResponseBody.Companion.toResponseBody
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okio.Buffer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

class OkHttpRuntimeTransportTest {
    @Test
    fun `endpoint registration sends local data endpoint and reads UE assignment`() = runTest {
        val captured = mutableListOf<Request>()
        val client = responseClient(
            captured,
            """
            {
              "ue_ip": "8.8.8.7",
              "ue_prefix_length": 24
            }
            """.trimIndent(),
        )
        val transport = OkHttpRuntimeTransport("runtime.example", 8443, client)

        val registration = transport.registerEndpoint("192.168.1.10", 4001, 28443)

        assertEquals("POST", captured.single().method)
        assertEquals(
            "http://runtime.example:8443/sdk/v1/endpoints",
            captured.single().url.toString(),
        )
        assertEquals(
            buildJsonObject {
                put("local_vlan_ip", "192.168.1.10")
                put("tcp_port", 4001)
                put("udp_port", 28443)
            },
            requestJson(captured.single()),
        )
        assertEquals("8.8.8.7", registration.ueIp)
        assertEquals(24, registration.uePrefixLength)
        assertEquals("8.8.8.7/24", registration.agentTunCidr)
    }

    @Test
    fun `endpoint registration rejects prefix outside address family`() = runTest {
        val client = responseClient(
            mutableListOf(),
            """
            {
              "ue_ip": "8.8.8.7",
              "ue_prefix_length": 33
            }
            """.trimIndent(),
        )
        val transport = OkHttpRuntimeTransport("runtime.example", 8443, client)

        val error = try {
            transport.registerEndpoint("192.168.1.10", 4001, 28443)
            throw AssertionError("Expected AgentSdkException")
        } catch (error: AgentSdkException) {
            error
        }

        assertEquals(ErrorCode.RUNTIME_REJECTED, error.code)
        assertEquals("ue_prefix_length", error.field)
    }

    @Test
    fun `endpoint registration rejects non-literal UE IP`() = runTest {
        val client = responseClient(
            mutableListOf(),
            """
            {
              "ue_ip": "ue-a.example",
              "ue_prefix_length": 24
            }
            """.trimIndent(),
        )
        val transport = OkHttpRuntimeTransport("runtime.example", 8443, client)

        val error = try {
            transport.registerEndpoint("192.168.1.10", 4001, 28443)
            throw AssertionError("Expected AgentSdkException")
        } catch (error: AgentSdkException) {
            error
        }

        assertEquals(ErrorCode.RUNTIME_REJECTED, error.code)
        assertEquals("ue_ip", error.field)
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
                    NetworkMessageAction.ACCEPT
                } else {
                    NetworkMessageAction.REJECT
                }
            }
            val serverSocket = opened.await()
            val upgrade = server.takeRequest(2, TimeUnit.SECONDS)!!
            assertEquals(DOWNLINK_WEBSOCKET_PATH, upgrade.path)
            assertEquals("websocket", upgrade.getHeader("Upgrade")?.lowercase())

            serverSocket.send(downlinkRequest("delivery-1", 49, 1))
            serverSocket.send(downlinkRequest("delivery-2", 50, 2))
            assertEquals(
                buildJsonObject {
                    put("kind", "response")
                    put("request_id", "delivery-2")
                    put("payload", buildJsonObject { put("result", "REJECT") })
                },
                Json.parseToJsonElement(responses.poll(2, TimeUnit.SECONDS)),
            )
            releaseFirst.complete(Unit)
            assertEquals(
                buildJsonObject {
                    put("kind", "response")
                    put("request_id", "delivery-1")
                    put("payload", buildJsonObject { put("result", "ACCEPT") })
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

    private fun downlinkRequest(requestId: String, transactionId: Int, sequence: Int) =
        buildJsonObject {
            put("kind", "request")
            put("request_id", requestId)
            put("message_type", "ACN_AGENT_GROUPING_INVITATION")
            put("transaction_id", transactionId)
            put("payload", buildJsonObject { put("sequence", sequence) })
        }.toString()

    private fun responseClient(captured: MutableList<Request>, body: String): OkHttpClient =
        OkHttpClient.Builder()
            .addInterceptor { chain ->
                val request = chain.request()
                captured += request
                Response.Builder()
                    .request(request)
                    .protocol(Protocol.HTTP_1_1)
                    .code(200)
                    .message("OK")
                    .body(body.toResponseBody("application/json".toMediaType()))
                    .build()
            }
            .build()

    private fun requestJson(request: Request) = Buffer().use { buffer ->
        requireNotNull(request.body).writeTo(buffer)
        Json.parseToJsonElement(buffer.readUtf8())
    }
}
