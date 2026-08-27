from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import struct
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

from .errors import AgentSdkError, ErrorCode


CORE_NETWORK_VERIFICATION_METHOD = (
    "did:udid:core@6gc.mnc015.mcc234.3gppnetwork.org#keys-1"
)
_IDENTITY_APPLICATION_PATH = "/idm/v1/identity-applications"
_IDENTITY_SIGNATURE_DOMAIN = b"ACN-H-ID-v1\0"
_UTC_RFC3339_MILLIS = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$"
)
_P256_DID_MULTICODEC = b"\x80\x24"
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _identity_string(
    payload: Mapping[str, Any], field: str, *, container: str | None = None
) -> str:
    source: Any = payload
    if container is not None:
        source = payload.get(container)
        if not isinstance(source, Mapping):
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                f"{container} must be a JSON object",
                field=container,
            )
    value = source.get(field) if isinstance(source, Mapping) else None
    if not isinstance(value, str) or not value:
        qualified = f"{container}.{field}" if container else field
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            f"{qualified} must be a non-empty string",
            field=qualified,
        )
    return value


def _lp16(value: bytes, field: str) -> bytes:
    if len(value) > 0xFFFF:
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            f"{field} is too long for LP16 encoding",
            field=field,
        )
    return struct.pack("!H", len(value)) + value


def identity_application_signing_bytes(payload: Mapping[str, Any]) -> bytes:
    """Encode the H-ID signature input exactly as specified by ACN-H-ID-v1."""

    owner = _identity_string(payload, "owner").encode("utf-8")
    name = _identity_string(payload, "name").encode("utf-8")
    description = _identity_string(payload, "description").encode("utf-8")
    timestamp = _identity_string(payload, "timestamp")
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            "metadata must be a JSON object",
            field="metadata",
        )
    for required in ("region", "os", "version"):
        _identity_string(payload, required, container="metadata")
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in metadata.items()
    ):
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            "metadata keys and values must be strings",
            field="metadata",
        )
    metadata_json = json.dumps(
        metadata,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    try:
        public_key_der = base64.b64decode(
            _identity_string(payload, "public_key"), validate=True
        )
        public_key = serialization.load_der_public_key(public_key_der)
    except (ValueError, TypeError, binascii.Error, UnsupportedAlgorithm) as exc:
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            "public_key must be standard Base64 SPKI DER",
            field="public_key",
        ) from exc
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            "public_key must be an ECDSA P-256 SPKI key",
            field="public_key",
        )
    if not _UTC_RFC3339_MILLIS.fullmatch(timestamp):
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            "timestamp must be UTC RFC3339 with at most millisecond precision",
            field="timestamp",
        )
    try:
        instant = datetime.fromisoformat(timestamp.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            "timestamp is not a valid UTC instant",
            field="timestamp",
        ) from exc
    delta = instant - datetime(1970, 1, 1, tzinfo=timezone.utc)
    timestamp_millis = (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )
    if timestamp_millis < 0:
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            "timestamp must not precede the Unix epoch",
            field="timestamp",
        )
    return b"".join(
        (
            _IDENTITY_SIGNATURE_DOMAIN,
            _lp16(owner, "owner"),
            _lp16(name, "name"),
            _lp16(public_key_der, "public_key"),
            _lp16(description, "description"),
            struct.pack("!Q", timestamp_millis),
            _lp16(metadata_json, "metadata"),
        )
    )


def canonical_json(value: Any) -> bytes:
    """Return the UTF-8, sorted, compact JSON representation signed by the SDK."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    encoded = value.encode("ascii")
    return base64.b64decode(
        encoded + b"=" * (-len(encoded) % 4),
        altchars=b"-_",
        validate=True,
    )


def _base58_encode(value: bytes) -> str:
    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _BASE58_ALPHABET[remainder] + encoded
    leading_zeroes = len(value) - len(value.lstrip(b"\0"))
    return "1" * leading_zeroes + (encoded or "1")


def _base58_decode(value: str) -> bytes:
    number = 0
    for character in value:
        try:
            digit = _BASE58_ALPHABET.index(character)
        except ValueError as exc:
            raise ValueError("did:key contains invalid base58btc data") from exc
        number = number * 58 + digit
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big")
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return b"\0" * leading_zeroes + decoded


def _document_without_signature(payload: Mapping[str, Any]) -> dict[str, Any]:
    document = dict(payload)
    document.pop("signature", None)
    document.pop("signature_encoding", None)
    proof = document.get("proof")
    if isinstance(proof, Mapping):
        proof_options = dict(proof)
        proof_options.pop("jws", None)
        document["proof"] = proof_options
    return document


def _proof_signing_bytes(
    document: Mapping[str, Any], proof: Mapping[str, Any]
) -> bytes:
    """Build proofHash || documentHash for the ACN detached-JWS profile.

    ``proof`` is canonicalized without its signature value.  The business
    document is canonicalized without the complete ``proof`` member.  The
    existing snake_case wire names are intentionally preserved.
    """

    proof_options = dict(proof)
    proof_options.pop("jws", None)
    unsecured_document = dict(document)
    unsecured_document.pop("proof", None)
    proof_hash = hashlib.sha256(canonical_json(proof_options)).digest()
    document_hash = hashlib.sha256(canonical_json(unsecured_document)).digest()
    return proof_hash + document_hash


def _load_p256_public_key(pem: bytes) -> ec.EllipticCurvePublicKey:
    try:
        public_key = serialization.load_pem_public_key(pem)
    except (TypeError, ValueError) as exc:
        raise AgentSdkError(
            ErrorCode.SIGNATURE_ERROR,
            "invalid embedded core-network public key",
        ) from exc
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        raise AgentSdkError(
            ErrorCode.SIGNATURE_ERROR,
            "core-network public key must be P-256",
        )
    return public_key


def embedded_core_network_public_key_pem() -> bytes:
    return (
        files("agent_sdk")
        .joinpath("certs/core-network-public-key.pem")
        .read_bytes()
    )


class DeviceSigningIdentity:
    def __init__(self, private_key: ec.EllipticCurvePrivateKey) -> None:
        self._private_key = private_key
        self._public_key = private_key.public_key()
        compressed = self._public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.CompressedPoint,
        )
        self.did_key = "did:key:z" + _base58_encode(
            _P256_DID_MULTICODEC + compressed
        )
        fragment = self.did_key.removeprefix("did:key:")
        self.verification_method = f"{self.did_key}#{fragment}"
        public_der = self._public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.public_key_base64 = base64.b64encode(public_der).decode("ascii")

    @property
    def public_key(self) -> ec.EllipticCurvePublicKey:
        return self._public_key

    def sign_base64(self, document: Mapping[str, Any]) -> str:
        signature = self._private_key.sign(
            canonical_json(_document_without_signature(document)),
            ec.ECDSA(hashes.SHA256()),
        )
        return base64.b64encode(signature).decode("ascii")

    def sign_identity_application(self, document: Mapping[str, Any]) -> str:
        signature = self._private_key.sign(
            identity_application_signing_bytes(document),
            ec.ECDSA(hashes.SHA256()),
        )
        return base64.b64encode(signature).decode("ascii")

    def create_proof(
        self,
        payload: Mapping[str, Any],
        *,
        purpose: str,
        created: str | None = None,
    ) -> dict[str, Any]:
        proof: dict[str, Any] = {
            "type": "JsonWebSignature2020",
            "verification_method": self.verification_method,
            "proof_purpose": purpose,
            "created": created or _utc_now(),
        }
        protected = _base64url_encode(
            canonical_json({"alg": "ES256", "b64": False, "crit": ["b64"]})
        )
        verify_data = _proof_signing_bytes(payload, proof)
        signing_input = protected.encode("ascii") + b"." + verify_data
        der_signature = self._private_key.sign(
            signing_input, ec.ECDSA(hashes.SHA256())
        )
        r_value, s_value = decode_dss_signature(der_signature)
        raw_signature = r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big")
        proof["jws"] = f"{protected}..{_base64url_encode(raw_signature)}"
        return proof


class DeviceSigningIdentityStore:
    """Persist one SDK-owned P-256 signing identity for the current device."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory or self._default_directory()
        self._identity: DeviceSigningIdentity | None = None

    @staticmethod
    def _default_directory() -> Path:
        state_home = os.environ.get("XDG_STATE_HOME")
        base = Path(state_home) if state_home else Path.home() / ".local" / "state"
        return base / "agent-sdk" / "security"

    def ensure(self) -> DeviceSigningIdentity:
        if self._identity is not None:
            return self._identity
        private_path = self._directory / "device-private-key.pem"
        public_path = self._directory / "device-public-key.pem"
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._directory, 0o700)
        if public_path.exists() and not private_path.exists():
            raise AgentSdkError(
                ErrorCode.SIGNATURE_ERROR,
                "device signing identity is incomplete: private key is missing",
            )
        if not private_path.exists():
            private_key = ec.generate_private_key(ec.SECP256R1())
            private_pem = private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            self._atomic_write(private_path, private_pem, 0o600)
        try:
            private_key = serialization.load_pem_private_key(
                private_path.read_bytes(), password=None
            )
        except (OSError, TypeError, ValueError) as exc:
            raise AgentSdkError(
                ErrorCode.SIGNATURE_ERROR,
                f"invalid device signing private key: {exc}",
            ) from exc
        if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
            private_key.curve, ec.SECP256R1
        ):
            raise AgentSdkError(
                ErrorCode.SIGNATURE_ERROR,
                "device signing private key must be P-256",
            )
        public_pem = private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if public_path.exists() and public_path.read_bytes() != public_pem:
            raise AgentSdkError(
                ErrorCode.SIGNATURE_ERROR,
                "device signing public and private keys do not match",
            )
        if not public_path.exists():
            self._atomic_write(public_path, public_pem, 0o600)
        os.chmod(private_path, 0o600)
        os.chmod(public_path, 0o600)
        self._identity = DeviceSigningIdentity(private_key)
        return self._identity

    @staticmethod
    def _atomic_write(path: Path, content: bytes, mode: int) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise


def _public_key_from_did_key(value: str) -> ec.EllipticCurvePublicKey:
    did_key = value.split("#", 1)[0]
    if not did_key.startswith("did:key:z"):
        raise AgentSdkError(
            ErrorCode.SIGNATURE_ERROR,
            "peer did_key must use P-256 did:key base58btc encoding",
        )
    try:
        decoded = _base58_decode(did_key.removeprefix("did:key:z"))
        if not decoded.startswith(_P256_DID_MULTICODEC):
            raise ValueError("did:key is not a P-256 public key")
        public_key = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), decoded[len(_P256_DID_MULTICODEC) :]
        )
    except (ValueError, TypeError) as exc:
        raise AgentSdkError(ErrorCode.SIGNATURE_ERROR, str(exc)) from exc
    return public_key


def verify_proof(
    payload: Mapping[str, Any],
    public_key: ec.EllipticCurvePublicKey,
    *,
    expected_purpose: str,
) -> None:
    proof = payload.get("proof")
    if not isinstance(proof, Mapping):
        raise AgentSdkError(ErrorCode.SIGNATURE_ERROR, "proof is required")
    if proof.get("type") != "JsonWebSignature2020":
        raise AgentSdkError(
            ErrorCode.SIGNATURE_ERROR,
            "proof.type must be JsonWebSignature2020",
        )
    if proof.get("proof_purpose") != expected_purpose:
        raise AgentSdkError(
            ErrorCode.SIGNATURE_ERROR,
            f"proof.proof_purpose must be {expected_purpose}",
        )
    if not isinstance(proof.get("created"), str) or not proof["created"]:
        raise AgentSdkError(ErrorCode.SIGNATURE_ERROR, "proof.created is required")
    jws = proof.get("jws")
    if not isinstance(jws, str) or not jws:
        raise AgentSdkError(ErrorCode.SIGNATURE_ERROR, "proof.jws is required")
    verify_data = _proof_signing_bytes(payload, proof)
    try:
        if jws.count(".") == 2:
            protected, detached_payload, encoded_signature = jws.split(".")
            if detached_payload:
                raise ValueError("proof.jws payload must be detached")
            header = json.loads(_base64url_decode(protected))
            if not isinstance(header, dict) or header.get("alg") != "ES256":
                raise ValueError("proof.jws algorithm must be ES256")
            if header.get("b64") is not False or header.get("crit") != ["b64"]:
                raise ValueError("proof.jws must use critical b64=false")
            signing_input = protected.encode("ascii") + b"." + verify_data
            raw_signature = _base64url_decode(encoded_signature)
            if len(raw_signature) != 64:
                raise ValueError("proof.jws ES256 signature must be 64 bytes")
            der_signature = encode_dss_signature(
                int.from_bytes(raw_signature[:32], "big"),
                int.from_bytes(raw_signature[32:], "big"),
            )
            public_key.verify(
                der_signature, signing_input, ec.ECDSA(hashes.SHA256())
            )
        else:
            encoded = jws.encode("ascii")
            encoded += b"=" * (-len(encoded) % 4)
            der_signature = base64.b64decode(encoded, validate=True)
            public_key.verify(
                der_signature, verify_data, ec.ECDSA(hashes.SHA256())
            )
    except (
        InvalidSignature,
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise AgentSdkError(
            ErrorCode.SIGNATURE_ERROR,
            "message signature verification failed",
        ) from exc


class DeviceControlRequestAuthenticator:
    def __init__(self, identity_store: DeviceSigningIdentityStore) -> None:
        self._identity_store = identity_store

    async def authenticate(
        self, path: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        identity = self._identity_store.ensure()
        timestamp = _utc_now()
        business_payload = {
            key: value for key, value in payload.items() if key != "request_id"
        }
        document = {**business_payload, "timestamp": timestamp}
        if path == _IDENTITY_APPLICATION_PATH:
            return {
                "timestamp": timestamp,
                "signature": identity.sign_identity_application(document),
                "signature_encoding": "base64",
            }
        return {
            "timestamp": timestamp,
            "proof": identity.create_proof(
                document,
                purpose="authentication",
                created=timestamp,
            ),
        }


class CoreNetworkProofVerifier:
    def __init__(self, public_key_pem: bytes | None = None) -> None:
        self._public_key = _load_p256_public_key(
            public_key_pem or embedded_core_network_public_key_pem()
        )

    async def verify_group_config(self, payload: Mapping[str, Any]) -> None:
        verify_proof(
            payload,
            self._public_key,
            expected_purpose="assertionMethod",
        )


class DeviceMessageSigner:
    def __init__(self, identity_store: DeviceSigningIdentityStore) -> None:
        self._identity_store = identity_store

    async def sign_a2a(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._identity_store.ensure().create_proof(
            payload, purpose="authentication"
        )


class DidKeyMessageSignatureVerifier:
    async def verify_a2a(
        self, payload: Mapping[str, Any], expected_did_key: str
    ) -> None:
        verify_proof(
            payload,
            _public_key_from_did_key(expected_did_key),
            expected_purpose="authentication",
        )


class DisabledProofVerifier:
    """Internal-test verifier that deliberately accepts all group configs."""

    async def verify_group_config(self, payload: Mapping[str, Any]) -> None:
        del payload


class DisabledMessageSignatureVerifier:
    """Internal-test verifier that deliberately accepts all A2A messages."""

    async def verify_a2a(
        self, payload: Mapping[str, Any], expected_did_key: str
    ) -> None:
        del payload, expected_did_key


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
    """Test-only compatibility implementation. Applications do not need this."""

    async def authenticate(
        self, path: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        del payload
        timestamp = _utc_now()
        if path == _IDENTITY_APPLICATION_PATH:
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
    """Test-only compatibility implementation. Applications do not need this."""

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
