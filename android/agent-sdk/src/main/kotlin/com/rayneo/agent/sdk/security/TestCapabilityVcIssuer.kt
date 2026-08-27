package com.rayneo.agent.sdk.security

import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.io.File
import java.security.KeyFactory
import java.security.PrivateKey
import java.security.Signature
import java.security.interfaces.ECPrivateKey
import java.security.spec.PKCS8EncodedKeySpec
import java.time.Instant
import java.time.temporal.ChronoUnit
import java.time.format.DateTimeFormatterBuilder
import java.util.Base64
import java.util.Locale
import java.util.UUID

internal const val TEST_CAPABILITY_ISSUER_DID =
    "did:thirdpartyissuer@6gc.mnc015.mcc234.3gppnetwork"
internal const val TEST_CAPABILITY_ISSUER_KEY_ID = "$TEST_CAPABILITY_ISSUER_DID#keys-1"

internal fun embeddedTestCapabilityIssuerPublicKeyPem(): ByteArray =
    TestCapabilityVcIssuer::class.java.getResourceAsStream(
        "/certs/third-party-capability-public-key.pem"
    )?.use { it.readBytes() } ?: throw signatureError(
        "Embedded test capability issuer public key is missing"
    )

internal fun embeddedTestCapabilityIssuerPrivateKeyPem(): ByteArray =
    TestCapabilityVcIssuer::class.java.getResourceAsStream(
        "/certs/third-party-capability-private-key.pem"
    )?.use { it.readBytes() } ?: throw signatureError(
        "Embedded test capability issuer private key is missing"
    )

/** Lab-only issuer. The packaged test key is copied to app-private storage. */
internal class TestCapabilityVcIssuer(
    private val privateKeyFile: File,
) {
    fun importPrivateKey(privateKeyPem: ByteArray) {
        parseP256PrivateKey(privateKeyPem)
        val parent = privateKeyFile.parentFile ?: throw signatureError(
            "Test capability issuer key directory is unavailable"
        )
        if (!parent.exists() && !parent.mkdirs()) {
            throw signatureError("Cannot create test capability issuer key directory")
        }
        parent.setReadable(false, false)
        parent.setWritable(false, false)
        parent.setExecutable(false, false)
        parent.setReadable(true, true)
        parent.setWritable(true, true)
        parent.setExecutable(true, true)

        val temporary = File(parent, "${privateKeyFile.name}.${UUID.randomUUID()}.tmp")
        try {
            temporary.writeBytes(privateKeyPem)
            temporary.setReadable(false, false)
            temporary.setWritable(false, false)
            temporary.setExecutable(false, false)
            temporary.setReadable(true, true)
            temporary.setWritable(true, true)
            if (privateKeyFile.exists() && !privateKeyFile.delete()) {
                throw signatureError("Cannot replace test capability issuer private key")
            }
            if (!temporary.renameTo(privateKeyFile)) {
                throw signatureError("Cannot persist test capability issuer private key")
            }
            privateKeyFile.setReadable(false, false)
            privateKeyFile.setWritable(false, false)
            privateKeyFile.setExecutable(false, false)
            privateKeyFile.setReadable(true, true)
            privateKeyFile.setWritable(true, true)
        } finally {
            temporary.delete()
        }
    }

    fun issue(
        agentId: String,
        agentName: String,
        capabilities: List<String>,
        validityDays: Long = 365,
        authorizationMode: String = "Mode2",
        now: Instant = Instant.now(),
    ): List<JsonObject> {
        val normalizedAgentId = agentId.trim().takeIf { it.isNotEmpty() }
            ?: invalid("agentId must be a non-empty string", "agentId")
        val normalizedAgentName = agentName.trim().takeIf { it.isNotEmpty() }
            ?: invalid("agentName must be a non-empty string", "agentName")
        val normalizedCapabilities = capabilities.map(String::trim)
        if (normalizedCapabilities.isEmpty() || normalizedCapabilities.any(String::isEmpty)) {
            invalid(
                "capabilities must contain at least one non-empty string",
                "capabilities",
            )
        }
        if (normalizedCapabilities.distinct().size != normalizedCapabilities.size) {
            invalid("capabilities must not contain duplicates", "capabilities")
        }
        if (validityDays <= 0) {
            invalid("validityDays must be greater than zero", "validityDays")
        }
        val privateKey = loadPrivateKey()
        val issuedAt = now.truncatedTo(ChronoUnit.SECONDS)
        val expiresAt = issuedAt.plus(validityDays, ChronoUnit.DAYS)

        return normalizedCapabilities.map { capability ->
            val unsigned = buildJsonObject {
                put("context", buildJsonArray {
                    add(JsonPrimitive("3gpp-ts-33.xxx-v20.0.0"))
                })
                put("id", "urn:uuid:${UUID.randomUUID()}")
                put("type", buildJsonArray {
                    add(JsonPrimitive("VerifiableCredential"))
                    add(JsonPrimitive("AgentCapabilityCredential"))
                })
                put("issuer", TEST_CAPABILITY_ISSUER_DID)
                put("valid_from", issuedAt.toString())
                put("valid_until", expiresAt.toString())
                put("claims", buildJsonObject {
                    put("agent_id", normalizedAgentId)
                    put("agent_name", normalizedAgentName)
                    put("skill_name", capability)
                    put("authorization_mode", authorizationMode)
                })
            }
            val proofOptions = buildJsonObject {
                put("type", "JsonWebSignature2020")
                put("verification_method", TEST_CAPABILITY_ISSUER_KEY_ID)
                put("proof_purpose", "assertionMethod")
                put("created", canonicalTimestamp(issuedAt))
            }
            val protected = base64Url(canonicalJson(buildJsonObject {
                put("alg", "ES256")
                put("b64", false)
                put("crit", buildJsonArray { add(JsonPrimitive("b64")) })
            }))
            val signingInput = protected.toByteArray(Charsets.US_ASCII) +
                byteArrayOf('.'.code.toByte()) + proofSigningBytes(unsigned, proofOptions)
            val signature = Signature.getInstance("SHA256withECDSA").run {
                initSign(privateKey)
                update(signingInput)
                sign()
            }
            buildJsonObject {
                unsigned.forEach(::put)
                put("proof", buildJsonObject {
                    proofOptions.forEach(::put)
                    put("jws", "$protected..${base64Url(derToJose(signature))}")
                })
            }
        }
    }

    private fun loadPrivateKey(): PrivateKey {
        val encoded = try {
            privateKeyFile.readBytes()
        } catch (error: Exception) {
            throw AgentSdkException(
                ErrorCode.SIGNATURE_ERROR,
                "Test capability issuer private key is not imported",
                "testCapabilityIssuerPrivateKey",
                cause = error,
            )
        }
        return parseP256PrivateKey(encoded)
    }
}

private fun canonicalTimestamp(value: Instant): String =
    DateTimeFormatterBuilder().appendInstant(3).toFormatter().format(value)

internal fun canonicalAsciiJson(value: JsonObject): ByteArray = buildString {
    String(canonicalJson(value), Charsets.UTF_8).forEach { character ->
        if (character.code <= 0x7f) {
            append(character)
        } else {
            append("\\u")
            append(character.code.toString(16).padStart(4, '0').lowercase(Locale.ROOT))
        }
    }
}.toByteArray(Charsets.UTF_8)

private fun parseP256PrivateKey(pem: ByteArray): ECPrivateKey {
    val text = String(pem, Charsets.US_ASCII)
    val encoded = text
        .replace("-----BEGIN PRIVATE KEY-----", "")
        .replace("-----END PRIVATE KEY-----", "")
        .filterNot(Char::isWhitespace)
    val privateKey = try {
        KeyFactory.getInstance("EC").generatePrivate(
            PKCS8EncodedKeySpec(Base64.getDecoder().decode(encoded))
        ) as? ECPrivateKey
    } catch (error: Exception) {
        throw AgentSdkException(
            ErrorCode.SIGNATURE_ERROR,
            "Test capability issuer private key must be PKCS#8 PEM",
            "testCapabilityIssuerPrivateKey",
            cause = error,
        )
    } ?: throw signatureError("Test capability issuer private key must be an EC key")
    if (privateKey.params.curve.field.fieldSize != 256) {
        throw signatureError("Test capability issuer private key must be P-256")
    }
    return privateKey
}

private fun invalid(message: String, field: String): Nothing =
    throw AgentSdkException(ErrorCode.INVALID_ARGUMENT, message, field)

private fun signatureError(message: String): AgentSdkException =
    AgentSdkException(
        ErrorCode.SIGNATURE_ERROR,
        message,
        "testCapabilityIssuerPrivateKey",
    )
