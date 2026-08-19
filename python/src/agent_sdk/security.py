from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .errors import AgentSdkError, ErrorCode


class RejectUnconfiguredControlRequestAuthenticator:
    async def authenticate(
        self, path: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        del path, payload
        raise AgentSdkError(
            ErrorCode.SIGNATURE_ERROR,
            "no control-plane request authenticator is configured",
        )


class DemoControlRequestAuthenticator:
    """Test/example only. Production applications must use real private keys."""

    _LEGACY_SIGNATURE_PATHS = {
        "/idm/v1/identity-applications",
        "/acn-agent/v1/agent-deletions",
        "/arf/v1/agent-cards",
    }

    async def authenticate(
        self, path: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        del payload
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if path in self._LEGACY_SIGNATURE_PATHS:
            return {
                "timestamp": timestamp,
                "signature": "demo-only-signature",
                "signature_encoding": "base64",
            }
        return {
            "timestamp": timestamp,
            "proof": {
                "type": "DemoOnly",
                "verification_method": "did:key:demo#demo",
                "proof_purpose": "authentication",
                "created": timestamp,
                "jws": "demo-only-jws",
            },
        }


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
