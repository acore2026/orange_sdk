package com.rayneo.agent.sdk.transport

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

}
