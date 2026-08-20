package com.rayneo.agent.sdk.security

import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import com.rayneo.agent.sdk.transport.MessageSignatureVerifier
import com.rayneo.agent.sdk.transport.MessageSigner
import com.rayneo.agent.sdk.transport.ProofVerifier
import kotlinx.serialization.json.JsonObject

internal class RejectUnconfiguredProofVerifier : ProofVerifier {
    override suspend fun verifyGroupConfig(payload: JsonObject) {
        throw AgentSdkException(
            ErrorCode.SIGNATURE_ERROR,
            "No trusted group-config proof verifier is configured",
        )
    }
}

internal object RejectUnconfiguredMessageSigner : MessageSigner {
    override suspend fun signA2a(payload: JsonObject): JsonObject {
        throw AgentSdkException(ErrorCode.SIGNATURE_ERROR, "No A2A message signer is configured")
    }
}

internal object RejectUnconfiguredMessageSignatureVerifier : MessageSignatureVerifier {
    override suspend fun verifyA2a(payload: JsonObject, expectedDidKey: String) {
        throw AgentSdkException(
            ErrorCode.SIGNATURE_ERROR,
            "No A2A message signature verifier is configured",
        )
    }
}
