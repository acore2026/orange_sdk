package com.rayneo.agent.sdk.security

import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.Signature
import java.security.interfaces.ECPublicKey
import java.security.spec.ECGenParameterSpec
import java.security.spec.X509EncodedKeySpec
import java.time.Instant
import java.util.Base64

class TestCapabilityVcIssuerTest {
    @get:Rule
    val temporaryFolder = TemporaryFolder()

    @Test
    fun `issues one IDM compatible VC for each capability`() {
        val keyPair = p256KeyPair()
        val issuer = issuerWithKey(keyPair)

        val credentials = issuer.issue(
            agentId = "did:example:agent-a",
            agentName = "Agent Alpha",
            capabilities = listOf("robot-control", "voice"),
            now = Instant.parse("2026-08-20T00:00:00Z"),
        )

        assertEquals(2, credentials.size)
        assertEquals(
            listOf("robot-control", "voice"),
            credentials.map { it.getValue("claims").jsonObject
                .getValue("capability").jsonPrimitive.content },
        )
        credentials.forEach { credential ->
            assertEquals(
                listOf("VerifiableCredential", "CapabilityCredential"),
                credential.getValue("type").jsonArray.map { it.jsonPrimitive.content },
            )
            assertEquals(
                TEST_CAPABILITY_ISSUER_DID,
                credential.getValue("issuer").jsonPrimitive.content,
            )
            assertEquals(
                "2026-08-20T00:00:00Z",
                credential.getValue("valid_from").jsonPrimitive.content,
            )
            assertEquals(
                "2027-08-20T00:00:00Z",
                credential.getValue("valid_until").jsonPrimitive.content,
            )
            verifyCredential(credential, keyPair.public as ECPublicKey)
        }
    }

    @Test
    fun `signature rejects a tampered capability`() {
        val keyPair = p256KeyPair()
        val credential = issuerWithKey(keyPair).issue(
            "did:example:agent-a",
            "Agent Alpha",
            listOf("robot-control"),
        ).single()
        val tampered = buildJsonObject {
            credential.forEach { (key, value) ->
                put(
                    key,
                    if (key == "claims") buildJsonObject {
                        value.jsonObject.forEach { (claimKey, claimValue) ->
                            put(
                                claimKey,
                                if (claimKey == "capability") {
                                    JsonPrimitive("tampered")
                                } else {
                                    claimValue
                                },
                            )
                        }
                    } else value,
                )
            }
        }

        assertTrue(
            runCatching {
                verifyCredential(tampered, keyPair.public as ECPublicKey)
            }.isFailure
        )
    }

    @Test
    fun `missing imported key and duplicate capabilities fail clearly`() {
        val issuer = TestCapabilityVcIssuer(temporaryFolder.newFile("missing-key.pem"))
        temporaryFolder.root.resolve("missing-key.pem").delete()
        val missing = runCatching {
            issuer.issue("did:example:a", "Agent A", listOf("text"))
        }.exceptionOrNull() as AgentSdkException
        assertEquals(ErrorCode.SIGNATURE_ERROR, missing.code)
        assertEquals("testCapabilityIssuerPrivateKey", missing.field)

        val duplicate = runCatching {
            issuerWithKey(p256KeyPair()).issue(
                "did:example:a",
                "Agent A",
                listOf("text", "text"),
            )
        }.exceptionOrNull() as AgentSdkException
        assertEquals(ErrorCode.INVALID_ARGUMENT, duplicate.code)
        assertEquals("capabilities", duplicate.field)
    }

    @Test
    fun `embedded lab private key verifies with embedded public key`() {
        val issuer = TestCapabilityVcIssuer(
            temporaryFolder.newFolder("real-key").resolve("issuer-private-key.pem")
        )
        issuer.importPrivateKey(embeddedTestCapabilityIssuerPrivateKeyPem())
        val credential = issuer.issue(
            "did:example:agent-a",
            "Agent Alpha",
            listOf("robot-control"),
        ).single()

        verifyCredential(
            credential,
            parsePublicKey(embeddedTestCapabilityIssuerPublicKeyPem()),
        )
    }

    @Test
    fun `canonical test VC JSON escapes non ASCII like Python ensure ascii`() {
        val value = buildJsonObject {
            put("emoji", "😀")
            put("a", "中文")
        }

        assertEquals(
            "{\"a\":\"\\u4e2d\\u6587\",\"emoji\":\"\\ud83d\\ude00\"}",
            String(canonicalAsciiJson(value), Charsets.UTF_8),
        )
    }

    private fun issuerWithKey(keyPair: KeyPair): TestCapabilityVcIssuer {
        val issuer = TestCapabilityVcIssuer(
            temporaryFolder.newFolder().resolve("issuer-private-key.pem")
        )
        issuer.importPrivateKey(privateKeyPem(keyPair))
        return issuer
    }

    private fun verifyCredential(credential: JsonObject, publicKey: ECPublicKey) {
        val unsigned = buildJsonObject {
            listOf(
                "context",
                "id",
                "type",
                "issuer",
                "valid_from",
                "valid_until",
                "claims",
            ).forEach { field -> put(field, credential.getValue(field)) }
        }
        val signature = Base64.getDecoder().decode(
            credential.getValue("proof").jsonObject
                .getValue("signature_value").jsonPrimitive.content
        )
        val valid = Signature.getInstance("SHA256withECDSA").run {
            initVerify(publicKey)
            update(canonicalAsciiJson(unsigned))
            verify(signature)
        }
        assertTrue(valid)
    }

    private fun p256KeyPair(): KeyPair = KeyPairGenerator.getInstance("EC").run {
        initialize(ECGenParameterSpec("secp256r1"))
        generateKeyPair()
    }

    private fun privateKeyPem(keyPair: KeyPair): ByteArray = buildString {
        appendLine("-----BEGIN PRIVATE KEY-----")
        appendLine(Base64.getMimeEncoder(64, byteArrayOf('\n'.code.toByte()))
            .encodeToString(keyPair.private.encoded))
        appendLine("-----END PRIVATE KEY-----")
    }.toByteArray(Charsets.US_ASCII)

    private fun parsePublicKey(pem: ByteArray): ECPublicKey {
        val encoded = String(pem, Charsets.US_ASCII)
            .replace("-----BEGIN PUBLIC KEY-----", "")
            .replace("-----END PUBLIC KEY-----", "")
            .filterNot(Char::isWhitespace)
        return KeyFactory.getInstance("EC").generatePublic(
            X509EncodedKeySpec(Base64.getDecoder().decode(encoded))
        ) as ECPublicKey
    }
}
