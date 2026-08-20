from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .errors import AgentSdkError, ErrorCode


TEST_CAPABILITY_ISSUER_DID = (
    "did:thirdpartyissuer@6gc.mnc015.mcc234.3gppnetwork"
)
TEST_CAPABILITY_ISSUER_KEY_ID = f"{TEST_CAPABILITY_ISSUER_DID}#keys-1"


def default_test_capability_private_key_path() -> Path:
    """Return the lab-only third-party issuer key location.

    The key is deliberately read from outside the package. It must never be
    included in a Wheel or copied to a production Agent device.
    """

    return Path.home() / "lpx" / "cert" / "third-party" / "private-key.pem"


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

    Signing follows the existing IDM test convention: the seven VC fields are
    serialized as sorted compact ASCII JSON, then signed with P-256
    ECDSA/SHA-256. ``signature_value`` is ASN.1 DER encoded and standard Base64.
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

    resolved_key_path = Path(
        private_key_path or default_test_capability_private_key_path()
    ).expanduser()
    try:
        private_key = serialization.load_pem_private_key(
            resolved_key_path.read_bytes(),
            password=None,
        )
    except (OSError, ValueError, TypeError) as exc:
        raise AgentSdkError(
            ErrorCode.SIGNATURE_ERROR,
            f"cannot load test capability issuer private key: {resolved_key_path}",
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
    expires_at = issued_at + timedelta(days=validity_days)

    credentials: list[dict[str, Any]] = []
    for capability in normalized_capabilities:
        credential: dict[str, Any] = {
            "context": ["3gpp-ts-33.xxx-v20.0.0"],
            "id": f"urn:uuid:{uuid.uuid4()}",
            "type": ["VerifiableCredential", "CapabilityCredential"],
            "issuer": TEST_CAPABILITY_ISSUER_DID,
            "valid_from": _format_utc(issued_at),
            "valid_until": _format_utc(expires_at),
            "claims": {
                "agent_id": normalized_agent_id,
                "agent_name": normalized_agent_name,
                "capability": capability,
                "authorization_mode": authorization_mode,
            },
        }
        signing_payload = {
            field: credential[field]
            for field in (
                "context",
                "id",
                "type",
                "issuer",
                "valid_from",
                "valid_until",
                "claims",
            )
        }
        signing_message = json.dumps(
            signing_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = private_key.sign(signing_message, ec.ECDSA(hashes.SHA256()))
        credential["proof"] = {
            "creator": TEST_CAPABILITY_ISSUER_KEY_ID,
            "signature_value": base64.b64encode(signature).decode("ascii"),
        }
        credentials.append(credential)
    return credentials


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
