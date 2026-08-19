from __future__ import annotations

from typing import Any, Mapping

from .errors import AgentSdkError, ErrorCode


class RejectUnconfiguredProofVerifier:
    async def verify_group_config(self, payload: Mapping[str, Any]) -> None:
        del payload
        raise AgentSdkError(
            ErrorCode.SIGNATURE_ERROR,
            "no trusted group-config proof verifier is configured",
        )


class DemoAcceptAllProofVerifier:
    """Test/example only. Production applications must not use this verifier."""

    async def verify_group_config(self, payload: Mapping[str, Any]) -> None:
        proof = payload.get("proof")
        if not isinstance(proof, Mapping) or not proof.get("jws"):
            raise AgentSdkError(ErrorCode.SIGNATURE_ERROR, "proof.jws is required")


class RejectUnconfiguredMessageSigner:
    async def sign_a2a(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        del payload
        raise AgentSdkError(
            ErrorCode.SIGNATURE_ERROR,
            "no A2A message signer is configured",
        )


class RejectUnconfiguredMessageSignatureVerifier:
    async def verify_a2a(
        self, payload: Mapping[str, Any], expected_did_key: str
    ) -> None:
        del payload, expected_did_key
        raise AgentSdkError(
            ErrorCode.SIGNATURE_ERROR,
            "no A2A message signature verifier is configured",
        )


class DemoMessageSigner:
    async def sign_a2a(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        del payload
        return {"type": "DemoOnly", "jws": "not-a-production-signature"}


class DemoMessageSignatureVerifier:
    async def verify_a2a(
        self, payload: Mapping[str, Any], expected_did_key: str
    ) -> None:
        del expected_did_key
        proof = payload.get("proof")
        if not isinstance(proof, Mapping) or not proof.get("jws"):
            raise AgentSdkError(ErrorCode.SIGNATURE_ERROR, "A2A proof.jws is required")
