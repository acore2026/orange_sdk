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
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

class OkHttpRuntimeTransportTest {
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
