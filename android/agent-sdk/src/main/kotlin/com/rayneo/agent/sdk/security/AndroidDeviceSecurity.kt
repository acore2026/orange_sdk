package com.rayneo.agent.sdk.security

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import com.rayneo.agent.sdk.transport.ControlRequestAuthenticator
import com.rayneo.agent.sdk.transport.DevicePublicKeyProvider
import com.rayneo.agent.sdk.transport.MessageSignatureVerifier
import com.rayneo.agent.sdk.transport.MessageSigner
import com.rayneo.agent.sdk.transport.ProofVerifier
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.math.BigInteger
import java.security.AlgorithmParameters
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.MessageDigest
import java.security.PrivateKey
import java.security.PublicKey
import java.security.Signature
import java.security.interfaces.ECPublicKey
import java.security.spec.ECGenParameterSpec
import java.security.spec.ECPoint
import java.security.spec.ECPublicKeySpec
import java.security.spec.X509EncodedKeySpec
import java.time.Instant
import java.time.temporal.ChronoUnit
import java.util.Base64

private const val KEY_ALIAS = "agent-sdk-device-signing-v1"
private const val IDENTITY_APPLICATION_PATH = "/idm/v1/identity-applications"
private val IDENTITY_SIGNATURE_DOMAIN = "ACN-H-ID-v1\u0000".toByteArray(Charsets.US_ASCII)
private val UTC_RFC3339_MILLIS = Regex(
    "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d{1,3})?Z$"
)
private val P256_DID_MULTICODEC = byteArrayOf(0x80.toByte(), 0x24)
private const val BASE58_ALPHABET =
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

private fun utcNow(): String = Instant.now().truncatedTo(ChronoUnit.MILLIS).toString()

private fun JsonObject.requiredIdentityString(field: String): String =
    this[field]?.jsonPrimitive?.contentOrNull?.takeIf(String::isNotEmpty)
        ?: throw AgentSdkException(
            ErrorCode.INVALID_ARGUMENT,
            "$field must be a non-empty string",
            field,
        )

private fun ByteArrayOutputStream.writeLp16(value: ByteArray, field: String) {
    if (value.size > 0xffff) {
        throw AgentSdkException(
            ErrorCode.INVALID_ARGUMENT,
            "$field is too long for LP16 encoding",
            field,
        )
    }
    write((value.size ushr 8) and 0xff)
    write(value.size and 0xff)
    write(value)
}

private fun isP256(publicKey: ECPublicKey): Boolean {
    val expected = AlgorithmParameters.getInstance("EC").apply {
        init(ECGenParameterSpec("secp256r1"))
    }.getParameterSpec(java.security.spec.ECParameterSpec::class.java)
    return publicKey.params.curve == expected.curve &&
        publicKey.params.generator == expected.generator &&
        publicKey.params.order == expected.order &&
        publicKey.params.cofactor == expected.cofactor
}

internal fun identityApplicationSigningBytes(payload: JsonObject): ByteArray {
    val metadata = payload["metadata"] as? JsonObject ?: throw AgentSdkException(
        ErrorCode.INVALID_ARGUMENT,
        "metadata must be a JSON object",
        "metadata",
    )
    listOf("region", "os", "version").forEach(metadata::requiredIdentityString)
    if (metadata.any { (_, value) -> value !is JsonPrimitive || !value.isString }) {
        throw AgentSdkException(
            ErrorCode.INVALID_ARGUMENT,
            "metadata keys and values must be strings",
            "metadata",
        )
    }
    val publicKeyDer = try {
        Base64.getDecoder().decode(payload.requiredIdentityString("public_key"))
    } catch (error: IllegalArgumentException) {
        throw AgentSdkException(
            ErrorCode.INVALID_ARGUMENT,
            "public_key must be standard Base64 SPKI DER",
            "public_key",
            cause = error,
        )
    }
    val publicKey = try {
        KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(publicKeyDer))
    } catch (error: Exception) {
        throw AgentSdkException(
            ErrorCode.INVALID_ARGUMENT,
            "public_key must be a valid ECDSA SPKI key",
            "public_key",
            cause = error,
        )
    }
    if (publicKey !is ECPublicKey || !isP256(publicKey)) {
        throw AgentSdkException(
            ErrorCode.INVALID_ARGUMENT,
            "public_key must be an ECDSA P-256 SPKI key",
            "public_key",
        )
    }
    val timestamp = payload.requiredIdentityString("timestamp")
    if (!UTC_RFC3339_MILLIS.matches(timestamp)) {
        throw AgentSdkException(
            ErrorCode.INVALID_ARGUMENT,
            "timestamp must be UTC RFC3339 with at most millisecond precision",
            "timestamp",
        )
    }
    val timestampMillis = try {
        Instant.parse(timestamp).toEpochMilli()
    } catch (error: Exception) {
        throw AgentSdkException(
            ErrorCode.INVALID_ARGUMENT,
            "timestamp is not a valid UTC instant",
            "timestamp",
            cause = error,
        )
    }
    if (timestampMillis < 0) {
        throw AgentSdkException(
            ErrorCode.INVALID_ARGUMENT,
            "timestamp must not precede the Unix epoch",
            "timestamp",
        )
    }
    return ByteArrayOutputStream().apply {
        write(IDENTITY_SIGNATURE_DOMAIN)
        writeLp16(payload.requiredIdentityString("owner").toByteArray(), "owner")
        writeLp16(payload.requiredIdentityString("name").toByteArray(), "name")
        writeLp16(publicKeyDer, "public_key")
        writeLp16(payload.requiredIdentityString("description").toByteArray(), "description")
        for (shift in 56 downTo 0 step 8) {
            write(((timestampMillis ushr shift) and 0xff).toInt())
        }
        writeLp16(metadata.toString().toByteArray(Charsets.UTF_8), "metadata")
    }.toByteArray()
}

internal interface DeviceKeyBackend {
    fun ensure()
    val publicKey: ECPublicKey
    fun sign(document: ByteArray): ByteArray
}

internal class AndroidKeyStoreBackend(
    private val alias: String = KEY_ALIAS,
) : DeviceKeyBackend {
    private val keyStore: KeyStore
        get() = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    override fun ensure() {
        if (keyStore.containsAlias(alias)) return
        val generator = KeyPairGenerator.getInstance(
            KeyProperties.KEY_ALGORITHM_EC,
            "AndroidKeyStore",
        )
        generator.initialize(
            KeyGenParameterSpec.Builder(
                alias,
                KeyProperties.PURPOSE_SIGN or KeyProperties.PURPOSE_VERIFY,
            )
                .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                .setDigests(KeyProperties.DIGEST_SHA256)
                .setUserAuthenticationRequired(false)
                .build()
        )
        generator.generateKeyPair()
    }

    override val publicKey: ECPublicKey
        get() {
            ensure()
            return keyStore.getCertificate(alias).publicKey as? ECPublicKey
                ?: throw AgentSdkException(
                    ErrorCode.SIGNATURE_ERROR,
                    "Android device signing key must be P-256",
                )
        }

    override fun sign(document: ByteArray): ByteArray {
        ensure()
        val privateKey = keyStore.getKey(alias, null) as? PrivateKey
            ?: throw AgentSdkException(
                ErrorCode.SIGNATURE_ERROR,
                "Android device signing private key is unavailable",
            )
        return Signature.getInstance("SHA256withECDSA").run {
            initSign(privateKey)
            update(document)
            sign()
        }
    }
}

internal class AndroidDeviceSecurity internal constructor(
    private val deviceKeys: DeviceKeyBackend,
    private val coreNetworkPublicKey: ECPublicKey,
) : ProofVerifier,
    ControlRequestAuthenticator,
    DevicePublicKeyProvider,
    MessageSigner,
    MessageSignatureVerifier {

    override fun ensure() = deviceKeys.ensure()

    override val publicKeyBase64: String
        get() {
            ensure()
            return Base64.getEncoder().encodeToString(deviceKeys.publicKey.encoded)
        }

    val didKey: String
        get() {
            ensure()
            val point = deviceKeys.publicKey.w
            val x = unsignedFixed(point.affineX, 32)
            val prefix = if (point.affineY.testBit(0)) 0x03 else 0x02
            return "did:key:z" + base58Encode(
                P256_DID_MULTICODEC + byteArrayOf(prefix.toByte()) + x
            )
        }

    private val verificationMethod: String
        get() = "$didKey#${didKey.removePrefix("did:key:")}"

    override suspend fun authenticate(path: String, payload: JsonObject): JsonObject {
        val timestamp = utcNow()
        val businessPayload = buildJsonObject {
            payload.forEach { (key, value) -> if (key != "request_id") put(key, value) }
        }
        val document = buildJsonObject {
            businessPayload.forEach(::put)
            put("timestamp", timestamp)
        }
        return if (path == IDENTITY_APPLICATION_PATH) {
            buildJsonObject {
                put("timestamp", timestamp)
                put(
                    "signature",
                    Base64.getEncoder().encodeToString(
                        deviceKeys.sign(identityApplicationSigningBytes(document))
                    ),
                )
                put("signature_encoding", "base64")
            }
        } else {
            buildJsonObject {
                put("timestamp", timestamp)
                put("proof", createProof(document, "authentication", timestamp))
            }
        }
    }

    override suspend fun signA2a(payload: JsonObject): JsonObject =
        createProof(payload, "authentication", utcNow())

    override suspend fun verifyGroupConfig(payload: JsonObject) {
        verifyProof(payload, coreNetworkPublicKey, "assertionMethod")
    }

    override suspend fun verifyA2a(payload: JsonObject, expectedDidKey: String) {
        verifyProof(payload, publicKeyFromDidKey(expectedDidKey), "authentication")
    }

    internal fun createProof(
        payload: JsonObject,
        purpose: String,
        created: String = utcNow(),
    ): JsonObject {
        val proofOptions = buildJsonObject {
            put("type", "JsonWebSignature2020")
            put("verification_method", verificationMethod)
            put("proof_purpose", purpose)
            put("created", created)
        }
        val protected = base64Url(
            canonicalJson(
                buildJsonObject {
                    put("alg", "ES256")
                    put("b64", false)
                    put("crit", buildJsonArray { add(JsonPrimitive("b64")) })
                }
            )
        )
        val signingInput = protected.toByteArray(Charsets.US_ASCII) +
            byteArrayOf('.'.code.toByte()) + proofSigningBytes(payload, proofOptions)
        val signature = derToJose(deviceKeys.sign(signingInput))
        return buildJsonObject {
            proofOptions.forEach(::put)
            put("jws", "$protected..${base64Url(signature)}")
        }
    }

    private fun verifyProof(
        payload: JsonObject,
        publicKey: ECPublicKey,
        expectedPurpose: String,
    ) {
        val proof = payload["proof"] as? JsonObject
            ?: signatureError("proof is required")
        if (proof["type"]?.jsonPrimitive?.contentOrNull != "JsonWebSignature2020") {
            signatureError("proof.type must be JsonWebSignature2020")
        }
        if (proof["proof_purpose"]?.jsonPrimitive?.contentOrNull != expectedPurpose) {
            signatureError("proof.proof_purpose must be $expectedPurpose")
        }
        if (proof["created"]?.jsonPrimitive?.contentOrNull.isNullOrBlank()) {
            signatureError("proof.created is required")
        }
        val jws = proof["jws"]?.jsonPrimitive?.contentOrNull
            ?.takeIf { it.isNotBlank() } ?: signatureError("proof.jws is required")
        val verifyData = proofSigningBytes(payload, proof)
        try {
            val (signature, signingInput) = if (jws.count { it == '.' } == 2) {
                val parts = jws.split('.', limit = 3)
                require(parts[1].isEmpty()) { "proof.jws payload must be detached" }
                val header = Json.parseToJsonElement(
                    String(base64UrlDecode(parts[0]), Charsets.UTF_8)
                ).jsonObject
                require(header["alg"]?.jsonPrimitive?.content == "ES256") {
                    "proof.jws algorithm must be ES256"
                }
                require(header["b64"]?.jsonPrimitive?.content == "false") {
                    "proof.jws must use b64=false"
                }
                require(
                    header["crit"]?.jsonArray?.map { it.jsonPrimitive.content } ==
                        listOf("b64")
                ) { "proof.jws b64=false must be critical" }
                joseToDer(base64UrlDecode(parts[2])) to
                    (parts[0].toByteArray(Charsets.US_ASCII) +
                        byteArrayOf('.'.code.toByte()) + verifyData)
            } else {
                Base64.getDecoder().decode(jws) to verifyData
            }
            val valid = Signature.getInstance("SHA256withECDSA").run {
                initVerify(publicKey)
                update(signingInput)
                verify(signature)
            }
            require(valid) { "signature mismatch" }
        } catch (error: Exception) {
            if (error is AgentSdkException) throw error
            throw AgentSdkException(
                ErrorCode.SIGNATURE_ERROR,
                "Message signature verification failed",
                cause = error,
            )
        }
    }

    companion object {
        fun create(coreNetworkPublicKeyPem: InputStream): AndroidDeviceSecurity =
            AndroidDeviceSecurity(
                AndroidKeyStoreBackend(),
                parseP256PublicKey(coreNetworkPublicKeyPem.readBytes()),
            )
    }
}

internal fun proofSigningBytes(document: JsonObject, proof: JsonObject): ByteArray {
    val proofOptions = buildJsonObject {
        proof.forEach { (key, value) -> if (key != "jws") put(key, value) }
    }
    val unsecuredDocument = buildJsonObject {
        document.forEach { (key, value) -> if (key != "proof") put(key, value) }
    }
    val digest = MessageDigest.getInstance("SHA-256")
    val proofHash = digest.digest(canonicalJson(proofOptions))
    val documentHash = digest.digest(canonicalJson(unsecuredDocument))
    return proofHash + documentHash
}

internal fun canonicalJson(value: JsonElement): ByteArray = buildString {
    fun appendElement(element: JsonElement) {
        when (element) {
            is JsonObject -> {
                append('{')
                element.keys.sorted().forEachIndexed { index, key ->
                    if (index > 0) append(',')
                    append(JsonPrimitive(key).toString())
                    append(':')
                    appendElement(element.getValue(key))
                }
                append('}')
            }
            is JsonArray -> {
                append('[')
                element.forEachIndexed { index, item ->
                    if (index > 0) append(',')
                    appendElement(item)
                }
                append(']')
            }
            else -> append(element.toString())
        }
    }
    appendElement(value)
}.toByteArray(Charsets.UTF_8)

private fun parseP256PublicKey(pem: ByteArray): ECPublicKey {
    val text = String(pem, Charsets.US_ASCII)
    val encoded = text
        .replace("-----BEGIN PUBLIC KEY-----", "")
        .replace("-----END PUBLIC KEY-----", "")
        .replace(Regex("\\s"), "")
    val key = KeyFactory.getInstance("EC").generatePublic(
        X509EncodedKeySpec(Base64.getDecoder().decode(encoded))
    ) as? ECPublicKey ?: signatureError("Core-network public key must be P-256")
    if (key.params.curve.field.fieldSize != 256) {
        signatureError("Core-network public key must be P-256")
    }
    return key
}

private fun publicKeyFromDidKey(value: String): ECPublicKey {
    val didKey = value.substringBefore('#')
    if (!didKey.startsWith("did:key:z")) {
        signatureError("Peer did_key must use P-256 did:key base58btc encoding")
    }
    val decoded = base58Decode(didKey.removePrefix("did:key:z"))
    if (!decoded.take(P256_DID_MULTICODEC.size).toByteArray()
            .contentEquals(P256_DID_MULTICODEC)
    ) {
        signatureError("Peer did_key is not a P-256 public key")
    }
    val encodedPoint = decoded.copyOfRange(P256_DID_MULTICODEC.size, decoded.size)
    if (encodedPoint.size != 33 || encodedPoint[0].toInt() and 0xff !in 2..3) {
        signatureError("Peer did_key contains an invalid P-256 point")
    }
    val parameters = AlgorithmParameters.getInstance("EC").apply {
        init(ECGenParameterSpec("secp256r1"))
    }.getParameterSpec(java.security.spec.ECParameterSpec::class.java)
    val x = BigInteger(1, encodedPoint.copyOfRange(1, 33))
    val ySquared = x.modPow(BigInteger.valueOf(3), P256_PRIME)
        .subtract(x.multiply(BigInteger.valueOf(3))).add(P256_B).mod(P256_PRIME)
    var y = ySquared.modPow(P256_PRIME.add(BigInteger.ONE).shiftRight(2), P256_PRIME)
    val odd = encodedPoint[0].toInt() and 1 == 1
    if (y.testBit(0) != odd) y = P256_PRIME.subtract(y)
    return KeyFactory.getInstance("EC").generatePublic(
        ECPublicKeySpec(ECPoint(x, y), parameters)
    ) as ECPublicKey
}

private val P256_PRIME = BigInteger(
    "ffffffff00000001000000000000000000000000ffffffffffffffffffffffff",
    16,
)
private val P256_B = BigInteger(
    "5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b",
    16,
)

private fun base64Url(value: ByteArray): String =
    Base64.getUrlEncoder().withoutPadding().encodeToString(value)

private fun base64UrlDecode(value: String): ByteArray =
    Base64.getUrlDecoder().decode(value)

private fun base58Encode(value: ByteArray): String {
    var number = BigInteger(1, value)
    val result = StringBuilder()
    val radix = BigInteger.valueOf(58)
    while (number > BigInteger.ZERO) {
        val parts = number.divideAndRemainder(radix)
        result.append(BASE58_ALPHABET[parts[1].toInt()])
        number = parts[0]
    }
    value.takeWhile { it == 0.toByte() }.forEach { result.append('1') }
    return result.reverse().toString()
}

private fun base58Decode(value: String): ByteArray {
    var number = BigInteger.ZERO
    val radix = BigInteger.valueOf(58)
    value.forEach { character ->
        val digit = BASE58_ALPHABET.indexOf(character)
        if (digit < 0) signatureError("did:key contains invalid base58btc data")
        number = number.multiply(radix).add(BigInteger.valueOf(digit.toLong()))
    }
    val raw = number.toByteArray().let {
        if (it.size > 1 && it[0] == 0.toByte()) it.copyOfRange(1, it.size) else it
    }
    return ByteArray(value.takeWhile { it == '1' }.length) + raw
}

private fun unsignedFixed(value: BigInteger, size: Int): ByteArray {
    val raw = value.toByteArray().let {
        if (it.size > 1 && it[0] == 0.toByte()) it.copyOfRange(1, it.size) else it
    }
    require(raw.size <= size) { "integer does not fit P-256 coordinate" }
    return ByteArray(size - raw.size) + raw
}

private fun derToJose(der: ByteArray): ByteArray {
    require(der.size >= 8 && der[0] == 0x30.toByte()) { "Invalid ECDSA DER signature" }
    var offset = 2
    require(der[offset++] == 0x02.toByte()) { "Invalid ECDSA DER r value" }
    val rLength = der[offset++].toInt() and 0xff
    val r = der.copyOfRange(offset, offset + rLength)
    offset += rLength
    require(der[offset++] == 0x02.toByte()) { "Invalid ECDSA DER s value" }
    val sLength = der[offset++].toInt() and 0xff
    val s = der.copyOfRange(offset, offset + sLength)
    return unsignedFixed(BigInteger(1, r), 32) + unsignedFixed(BigInteger(1, s), 32)
}

private fun joseToDer(raw: ByteArray): ByteArray {
    require(raw.size == 64) { "ES256 signature must be 64 bytes" }
    fun integer(bytes: ByteArray): ByteArray {
        var value = bytes.dropWhile { it == 0.toByte() }.toByteArray()
        if (value.isEmpty()) value = byteArrayOf(0)
        if (value[0].toInt() and 0x80 != 0) value = byteArrayOf(0) + value
        return byteArrayOf(0x02, value.size.toByte()) + value
    }
    val r = integer(raw.copyOfRange(0, 32))
    val s = integer(raw.copyOfRange(32, 64))
    return byteArrayOf(0x30, (r.size + s.size).toByte()) + r + s
}

private fun signatureError(message: String): Nothing = throw AgentSdkException(
    ErrorCode.SIGNATURE_ERROR,
    message,
)
