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
import java.security.interfaces.ECPublicKey
import java.security.spec.ECGenParameterSpec
import java.util.Base64

class AndroidDeviceSecurityTest {
    @Test
    fun `control request is signed with the persistent device key`() = runTest {
        val backend = SoftwareP256Backend()
        val security = AndroidDeviceSecurity(backend, SoftwareP256Backend().publicKey)
        val payload = buildJsonObject {
            put("owner", "Alice")
            put("name", "Agent A")
            put("public_key", security.publicKeyBase64)
        }

        val authentication = security.authenticate(
            "/idm/v1/identity-applications",
            payload,
        )
        val document = buildJsonObject {
            payload.forEach(::put)
            put("timestamp", authentication.getValue("timestamp"))
        }
        val valid = Signature.getInstance("SHA256withECDSA").run {
            initVerify(backend.publicKey)
            update(canonicalJson(document))
            verify(Base64.getDecoder().decode(authentication.getValue("signature").jsonPrimitive.content))
        }

        assertTrue(valid)
        assertEquals("base64", authentication.getValue("signature_encoding").jsonPrimitive.content)
    }

    @Test
    fun `A2A detached JWS verifies by sender did key and rejects tampering`() = runTest {
        val backend = SoftwareP256Backend()
        val security = AndroidDeviceSecurity(backend, SoftwareP256Backend().publicKey)
        val unsigned = buildJsonObject {
            put("message_id", "m1")
            put("group_id", "g1")
            put("sender_agent_id", "a1")
            put("target_agent_id", "a2")
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
