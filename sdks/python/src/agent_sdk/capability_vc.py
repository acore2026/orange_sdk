from __future__ import annotations

import base64
import hashlib
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from .errors import AgentSdkError, ErrorCode
from .security import canonical_json


TEST_CAPABILITY_ISSUER_DID = (
    "did:thirdpartyissuer@6gc.mnc015.mcc234.3gppnetwork"
)
TEST_CAPABILITY_ISSUER_KEY_ID = f"{TEST_CAPABILITY_ISSUER_DID}#keys-1"


def embedded_test_capability_public_key_pem() -> bytes:
    """Read the lab issuer public key from the installed SDK package."""

    return (
        resources.files("agent_sdk.certs")
        .joinpath("third-party-capability-public-key.pem")
        .read_bytes()
    )


def embedded_test_capability_private_key_pem() -> bytes:
    """Read the lab-only issuer private key from the installed SDK package."""

    return (
        resources.files("agent_sdk.certs")
        .joinpath("third-party-capability-private-key.pem")
        .read_bytes()
    )


def issue_test_capability_vcs(
    *,
    agent_id: str,
    agent_name: str,
    capabilities: Sequence[str],
    private_key_path: str | Path | None = None,
    validity_days: int = 365,
    authorization_mode: str = "Mode2",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Issue one lab-only third-party VC for each capability string.

    Signing follows the ACN JsonWebSignature2020 credential profile with a
    detached ES256 JWS and ``proof_purpose=assertionMethod``.
    """

    normalized_agent_id = agent_id.strip()
    normalized_agent_name = agent_name.strip()
    if not normalized_agent_id:
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            "agent_id must be a non-empty string",
            field="agent_id",
        )
    if not normalized_agent_name:
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            "agent_name must be a non-empty string",
            field="agent_name",
        )
    if isinstance(capabilities, (str, bytes)):
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            "capabilities must be a sequence of non-empty strings",
            field="capabilities",
        )
    normalized_capabilities = [
        capability.strip() if isinstance(capability, str) else ""
        for capability in capabilities
    ]
    if not normalized_capabilities or any(
        not capability for capability in normalized_capabilities
    ):
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            "capabilities must contain at least one non-empty string",
            field="capabilities",
        )
    if len(set(normalized_capabilities)) != len(normalized_capabilities):
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            "capabilities must not contain duplicates",
            field="capabilities",
        )
    if validity_days <= 0:
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            "validity_days must be greater than zero",
            field="validity_days",
        )

    resolved_key_path: Path | None = None
    key_source = "SDK resource agent_sdk/certs/third-party-capability-private-key.pem"
    try:
        if private_key_path is None:
            private_key_pem = embedded_test_capability_private_key_pem()
        else:
            resolved_key_path = Path(private_key_path).expanduser()
            private_key_pem = resolved_key_path.read_bytes()
            key_source = str(resolved_key_path)
        private_key = serialization.load_pem_private_key(
            private_key_pem,
            password=None,
        )
    except (OSError, ValueError, TypeError) as exc:
        raise AgentSdkError(
            ErrorCode.SIGNATURE_ERROR,
            f"cannot load test capability issuer private key: {key_source}",
            field="test_vc_private_key_path",
        ) from exc
    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
        private_key.curve, ec.SECP256R1
    ):
        raise AgentSdkError(
            ErrorCode.SIGNATURE_ERROR,
            "test capability issuer private key must be a P-256 EC key",
            field="test_vc_private_key_path",
        )

    issued_at = now or datetime.now(timezone.utc)
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=timezone.utc)
    issued_at = issued_at.astimezone(timezone.utc).replace(microsecond=0)
    effective_from = _previous_calendar_year(issued_at)
    expires_at = issued_at + timedelta(days=validity_days)

    credentials: list[dict[str, Any]] = []
    for capability in normalized_capabilities:
        credential: dict[str, Any] = {
            "context": ["3gpp-ts-33.xxx-v20.0.0"],
            "id": f"urn:uuid:{uuid.uuid4()}",
            "type": ["VerifiableCredential", "AgentCapabilityCredential"],
            "issuer": TEST_CAPABILITY_ISSUER_DID,
            "valid_from": _format_utc(effective_from),
            "valid_until": _format_utc(expires_at),
            "claims": {
                "agent_id": normalized_agent_id,
                "agent_name": normalized_agent_name,
                "skill_name": capability,
                "authorization_mode": authorization_mode,
            },
        }
        credential["proof"] = _create_credential_proof(
            credential,
            private_key,
            created=_format_utc_millis(issued_at),
        )
        credentials.append(credential)
    return credentials


def _create_credential_proof(
    document: dict[str, Any],
    private_key: ec.EllipticCurvePrivateKey,
    *,
    created: str,
) -> dict[str, Any]:
    proof: dict[str, Any] = {
        "type": "JsonWebSignature2020",
        "verification_method": TEST_CAPABILITY_ISSUER_KEY_ID,
        "proof_purpose": "assertionMethod",
        "created": created,
    }
    protected = _base64url(
        canonical_json({"alg": "ES256", "b64": False, "crit": ["b64"]})
    )
    proof_hash = hashlib.sha256(canonical_json(proof)).digest()
    document_hash = hashlib.sha256(canonical_json(document)).digest()
    signing_input = (
        protected.encode("ascii") + b"." + proof_hash + document_hash
    )
    der_signature = private_key.sign(
        signing_input,
        ec.ECDSA(hashes.SHA256()),
    )
    r_value, s_value = decode_dss_signature(der_signature)
    raw_signature = r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big")
    proof["jws"] = f"{protected}..{_base64url(raw_signature)}"
    return proof


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _previous_calendar_year(value: datetime) -> datetime:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        # February 29 becomes February 28 in a non-leap target year.
        return value.replace(year=value.year - 1, day=28)


def _format_utc_millis(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
