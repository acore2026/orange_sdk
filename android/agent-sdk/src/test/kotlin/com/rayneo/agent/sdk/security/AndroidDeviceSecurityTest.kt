package com.rayneo.agent.sdk.security

import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.Signature
import java.security.MessageDigest
import java.security.interfaces.ECPublicKey
import java.security.spec.ECGenParameterSpec
import java.util.Base64

class AndroidDeviceSecurityTest {
    @Test
    fun `identity signing bytes match cross-platform golden vector`() {
        val payload = buildJsonObject {
            put("owner", "Alice")
            put("name", "AliceAgent")
            put(
                "public_key",
                "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEaxfR8uEsQkf4vOblY6RA8ncD" +
                    "fYEt6zOg9KE5RdiYwpZP40Li/hp/m47n60p8D54WK84zV2sxXs7LtkBoN79R9Q==",
            )
            put("description", "AgentModel-X, SN123456")
            put("timestamp", "2026-08-20T10:30:15.123Z")
            put("metadata", buildJsonObject {
                put("region", "CN")
                put("os", "Linux")
                put("version", "1.0.0")
            })
        }

        val encoded = identityApplicationSigningBytes(payload)
        val digest = MessageDigest.getInstance("SHA-256")
            .digest(encoded)
            .joinToString("") { "%02x".format(it) }

        assertEquals(174, encoded.size)
        assertEquals(
            "483881296c5966469dcc901c15e7ff1c970644d7cb81446493f6178837e47a03",
            digest,
        )
    }

    @Test
    fun `control request is signed with the persistent device key`() = runTest {
        val backend = SoftwareP256Backend()
        val security = AndroidDeviceSecurity(backend, SoftwareP256Backend().publicKey)
        val payload = buildJsonObject {
            put("request_id", "a3282bda-6d55-4c31-a0f6-d56f2cd2b1e2")
            put("owner", "Alice")
            put("name", "Agent A")
            put("public_key", security.publicKeyBase64)
            put("description", "AgentModel-X")
            put("metadata", buildJsonObject {
                put("region", "CN")
                put("os", "Android")
                put("version", "0.11.0")
            })
        }

        val authentication = security.authenticate(
            "/idm/v1/identity-applications",
            payload,
        )
        val document = buildJsonObject {
            payload.forEach { (key, value) -> if (key != "request_id") put(key, value) }
            put("timestamp", authentication.getValue("timestamp"))
        }
        val valid = Signature.getInstance("SHA256withECDSA").run {
            initVerify(backend.publicKey)
            update(identityApplicationSigningBytes(document))
            verify(Base64.getDecoder().decode(authentication.getValue("signature").jsonPrimitive.content))
        }

        assertTrue(valid)
        val expectedPrefix = "ACN-H-ID-v1\u0000\u0000\u0005Alice".toByteArray()
        assertTrue(
            identityApplicationSigningBytes(document)
                .copyOfRange(0, expectedPrefix.size)
                .contentEquals(expectedPrefix)
        )
        assertEquals("base64", authentication.getValue("signature_encoding").jsonPrimitive.content)
    }

    @Test
    fun `A2A detached JWS verifies by sender did key and rejects tampering`() = runTest {
        val backend = SoftwareP256Backend()
        val security = AndroidDeviceSecurity(backend, SoftwareP256Backend().publicKey)
        val unsigned = buildJsonObject {
            put("message_id", "m1")
            put("group_id", "g1")
            put("src_agent_id", "a1")
            put("dst_agent_id", "a2")
            put("type", "control")
            put("task_id", "task-patrol")
            put("timestamp", "2026-08-20T00:00:00Z")
            put("payload", buildJsonObject { put("command", "patrol") })
        }
        val signed = buildJsonObject {
            unsigned.forEach(::put)
            put("proof", security.signA2a(unsigned))
        }

        security.verifyA2a(signed, security.didKey)
        val tampered = buildJsonObject {
            signed.forEach { (key, value) ->
                put(
                    key,
                    if (key == "payload") {
                        buildJsonObject { put("command", "stop") }
                    } else {
                        value
                    },
                )
            }
        }
        val error = runCatching { security.verifyA2a(tampered, security.didKey) }
            .exceptionOrNull() as AgentSdkException
        assertEquals(ErrorCode.SIGNATURE_ERROR, error.code)
    }

    @Test
    fun `group config is verified only by the pinned core key`() = runTest {
        val coreBackend = SoftwareP256Backend()
        val coreSigner = AndroidDeviceSecurity(coreBackend, coreBackend.publicKey)
        val verifier = AndroidDeviceSecurity(SoftwareP256Backend(), coreBackend.publicKey)
        val unsigned = buildJsonObject {
            put("notification_type", "acf_group_config")
            put("version", "1.0.0")
            put("timestamp", "2026-08-20T00:00:00Z")
            put("group_id", "g1")
            put("members", buildJsonObject { })
        }
        val signed = buildJsonObject {
            unsigned.forEach(::put)
            put("proof", coreSigner.createProof(unsigned, "assertionMethod"))
        }

        verifier.verifyGroupConfig(signed)
        val tampered = buildJsonObject {
            signed.forEach { (key, value) ->
                put(key, if (key == "group_id") kotlinx.serialization.json.JsonPrimitive("g2") else value)
            }
        }
        val error = runCatching { verifier.verifyGroupConfig(tampered) }
            .exceptionOrNull() as AgentSdkException
        assertEquals(ErrorCode.SIGNATURE_ERROR, error.code)
    }

    private class SoftwareP256Backend : DeviceKeyBackend {
        private val keyPair: KeyPair = KeyPairGenerator.getInstance("EC").run {
            initialize(ECGenParameterSpec("secp256r1"))
            generateKeyPair()
        }

        override fun ensure() = Unit
        override val publicKey: ECPublicKey = keyPair.public as ECPublicKey

        override fun sign(document: ByteArray): ByteArray =
            Signature.getInstance("SHA256withECDSA").run {
                initSign(keyPair.private)
                update(document)
                sign()
            }
    }
}
