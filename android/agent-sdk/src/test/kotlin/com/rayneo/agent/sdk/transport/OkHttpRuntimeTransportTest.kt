package com.rayneo.agent.sdk.transport

import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.Buffer
import org.junit.Assert.assertEquals
import org.junit.Test

class OkHttpRuntimeTransportTest {
    @Test
    fun `endpoint registration sends callback endpoint and reads UE assignment`() = runTest {
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
