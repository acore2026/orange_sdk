package com.rayneo.agent.sdk.security

import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import com.rayneo.agent.sdk.transport.MessageSignatureVerifier
import com.rayneo.agent.sdk.transport.MessageSigner
import com.rayneo.agent.sdk.transport.ProofVerifier
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

class RejectUnconfiguredProofVerifier : ProofVerifier {
    override suspend fun verifyGroupConfig(payload: JsonObject) {
        throw AgentSdkException(
            ErrorCode.SIGNATURE_ERROR,
            "No trusted group-config proof verifier is configured",
        )
    }
}

class DemoAcceptAllProofVerifier : ProofVerifier {
    override suspend fun verifyGroupConfig(payload: JsonObject) {
        if (payload["proof"] == null) {
            throw AgentSdkException(ErrorCode.SIGNATURE_ERROR, "proof is required")
        }
    }
}

object RejectUnconfiguredMessageSigner : MessageSigner {
    override suspend fun signA2a(payload: JsonObject): JsonObject {
        throw AgentSdkException(ErrorCode.SIGNATURE_ERROR, "No A2A message signer is configured")
    }
}

object RejectUnconfiguredMessageSignatureVerifier : MessageSignatureVerifier {
    override suspend fun verifyA2a(payload: JsonObject, expectedDidKey: String) {
        throw AgentSdkException(
            ErrorCode.SIGNATURE_ERROR,
            "No A2A message signature verifier is configured",
        )
    }
}

object DemoMessageSigner : MessageSigner {
    override suspend fun signA2a(payload: JsonObject): JsonObject = buildJsonObject {
        put("type", "DemoOnly")
        put("jws", "not-a-production-signature")
    }
}

object DemoMessageSignatureVerifier : MessageSignatureVerifier {
    override suspend fun verifyA2a(payload: JsonObject, expectedDidKey: String) {
        if (payload["proof"] == null) {
            throw AgentSdkException(ErrorCode.SIGNATURE_ERROR, "A2A proof is required")
        }
    }
}
