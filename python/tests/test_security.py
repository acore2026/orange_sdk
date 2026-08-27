from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from agent_sdk import AgentSdkError, ErrorCode
from agent_sdk.security import (
    CoreNetworkProofVerifier,
    DeviceControlRequestAuthenticator,
    DeviceMessageSigner,
    DeviceSigningIdentity,
    DeviceSigningIdentityStore,
    DidKeyMessageSignatureVerifier,
    DisabledMessageSignatureVerifier,
    DisabledProofVerifier,
    _proof_signing_bytes,
    canonical_json,
    embedded_core_network_public_key_pem,
    identity_application_signing_bytes,
)


async def test_internal_test_verifiers_accept_unsigned_inbound_messages():
    await DisabledProofVerifier().verify_group_config({"group_id": "g1"})
    await DisabledMessageSignatureVerifier().verify_a2a(
        {"payload": {"text": "unsigned"}},
        "not-a-did-key",
    )


def test_device_signing_identity_is_generated_once_and_persisted(tmp_path: Path):
    directory = tmp_path / "security"
    first = DeviceSigningIdentityStore(directory).ensure()
    second = DeviceSigningIdentityStore(directory).ensure()

    assert first.public_key_base64 == second.public_key_base64
    assert first.did_key == second.did_key
    assert first.did_key.startswith("did:key:z")
    assert (directory / "device-private-key.pem").stat().st_mode & 0o777 == 0o600
    assert (directory / "device-public-key.pem").stat().st_mode & 0o777 == 0o600
    public_key = serialization.load_der_public_key(
        base64.b64decode(first.public_key_base64)
    )
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    assert isinstance(public_key.curve, ec.SECP256R1)


def test_identity_signing_bytes_match_cross_platform_golden_vector():
    payload = {
        "owner": "Alice",
        "name": "AliceAgent",
        "public_key": (
            "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEaxfR8uEsQkf4vOblY6RA8ncD"
            "fYEt6zOg9KE5RdiYwpZP40Li/hp/m47n60p8D54WK84zV2sxXs7LtkBoN79R9Q=="
        ),
        "description": "AgentModel-X, SN123456",
        "timestamp": "2026-08-20T10:30:15.123Z",
        "metadata": {"region": "CN", "os": "Linux", "version": "1.0.0"},
    }

    encoded = identity_application_signing_bytes(payload)

    assert len(encoded) == 204
    assert hashlib.sha256(encoded).hexdigest() == (
        "97eea3ebc7f7d6018d789b285bf36c16d545698b2931987855881b799d7fea60"
    )
    assert encoded.endswith(
        b'\x00.{"region":"CN","os":"Linux","version":"1.0.0"}'
    )


async def test_identity_signature_uses_field_encoding_and_excludes_request_id(tmp_path: Path):
    identity = DeviceSigningIdentityStore(tmp_path / "security").ensure()
    authenticator = DeviceControlRequestAuthenticator(
        DeviceSigningIdentityStore(tmp_path / "security")
    )
    payload = {
        "request_id": "a3282bda-6d55-4c31-a0f6-d56f2cd2b1e2",
        "owner": "Alice",
        "name": "Agent A",
        "public_key": identity.public_key_base64,
        "description": "AgentModel-X",
        "metadata": {"region": "CN", "os": "Linux", "version": "0.12.0"},
    }

    authentication = await authenticator.authenticate(
        "/idm/v1/identity-applications", payload
    )
    signed_document = {
        **{key: value for key, value in payload.items() if key != "request_id"},
        "timestamp": authentication["timestamp"],
    }
    identity.public_key.verify(
        base64.b64decode(authentication["signature"]),
        identity_application_signing_bytes(signed_document),
        ec.ECDSA(hashes.SHA256()),
    )
    assert identity_application_signing_bytes(signed_document).startswith(
        b"ACN-H-ID-v1\0\x00\x05Alice"
    )
    assert authentication["signature_encoding"] == "base64"


async def test_non_identity_proof_does_not_sign_http_request_id(tmp_path: Path):
    store = DeviceSigningIdentityStore(tmp_path / "security")
    authenticator = DeviceControlRequestAuthenticator(store)
    body = {
        "request_id": "9e4b0db9-450a-43a7-bda2-a539885f25be",
        "agent_id": "did:example:a",
        "intent": "Issue Network Ability Credential",
    }
    authentication = await authenticator.authenticate(
        "/idm/v1/network-ability", body
    )
    signed = {
        "agent_id": body["agent_id"],
        "intent": body["intent"],
        "timestamp": authentication["timestamp"],
        "proof": authentication["proof"],
    }
    from agent_sdk.security import verify_proof

    verify_proof(signed, store.ensure().public_key, expected_purpose="authentication")


def test_proof_signing_bytes_match_cross_platform_golden_vector():
    document = {
        "agent_id": "did:example:a",
        "intent": "Issue Network Ability Credential",
        "timestamp": "2026-08-21T00:00:00Z",
    }
    proof = {
        "type": "JsonWebSignature2020",
        "verification_method": "did:key:zExample#zExample",
        "proof_purpose": "authentication",
        "created": "2026-08-21T00:00:00Z",
        "jws": "excluded-from-proof-options",
    }

    verify_data = _proof_signing_bytes(
        {**document, "proof": proof}, proof
    )

    assert len(verify_data) == 64
    assert verify_data.hex() == (
        "1a96f0c94b92eaa51b8fb1de55b1842584e66a24be9af373507bd956581ab0b3"
        "31126a50a843b70e3b740f33884f6d0dc38054a942753600f9546c10a67122c1"
    )


def test_detached_jws_signs_proof_hash_then_document_hash(tmp_path: Path):
    identity = DeviceSigningIdentityStore(tmp_path / "security").ensure()
    document = {
        "agent_id": "did:example:a",
        "intent": "Issue Network Ability Credential",
        "timestamp": "2026-08-21T00:00:00Z",
    }
    proof = identity.create_proof(
        document,
        purpose="authentication",
        created="2026-08-21T00:00:00Z",
    )
    protected, detached_payload, encoded_signature = proof["jws"].split(".")
    raw_signature = base64.urlsafe_b64decode(
        encoded_signature + "=" * (-len(encoded_signature) % 4)
    )
    r_value = int.from_bytes(raw_signature[:32], "big")
    s_value = int.from_bytes(raw_signature[32:], "big")
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    identity.public_key.verify(
        encode_dss_signature(r_value, s_value),
        protected.encode("ascii") + b"." + _proof_signing_bytes(document, proof),
        ec.ECDSA(hashes.SHA256()),
    )
    assert detached_payload == ""
    assert "verification_method" in proof
    assert "proof_purpose" in proof
    assert "verificationMethod" not in proof
    assert "proofPurpose" not in proof


async def test_a2a_detached_jws_verifies_with_sender_did_key(tmp_path: Path):
    store = DeviceSigningIdentityStore(tmp_path / "security")
    identity = store.ensure()
    unsigned = {
        "message_id": "m1",
        "group_id": "g1",
        "src_agent_id": "a1",
        "dst_agent_id": "a2",
        "type": "control",
        "task_id": "task-patrol",
        "timestamp": "2026-08-20T00:00:00Z",
        "payload": {"command": "patrol"},
    }
    proof = await DeviceMessageSigner(store).sign_a2a(unsigned)
    signed = {**unsigned, "proof": proof}

    await DidKeyMessageSignatureVerifier().verify_a2a(signed, identity.did_key)
    tampered = {**signed, "payload": {"command": "stop"}}
    with pytest.raises(AgentSdkError) as caught:
        await DidKeyMessageSignatureVerifier().verify_a2a(
            tampered, identity.did_key
        )
    assert caught.value.code is ErrorCode.SIGNATURE_ERROR

    tampered_proof = {
        **signed,
        "proof": {
            **proof,
            "verification_method": "did:key:zTampered#zTampered",
        },
    }
    with pytest.raises(AgentSdkError) as caught:
        await DidKeyMessageSignatureVerifier().verify_a2a(
            tampered_proof, identity.did_key
        )
    assert caught.value.code is ErrorCode.SIGNATURE_ERROR


async def test_core_network_group_proof_uses_pinned_public_key():
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    signer = DeviceSigningIdentity(private_key)
    unsigned = {
        "notification_type": "acf_group_config",
        "version": "1.0.0",
        "timestamp": "2026-08-20T00:00:00Z",
        "group_id": "g1",
        "members": {},
    }
    proof = signer.create_proof(unsigned, purpose="assertionMethod")
    signed = {**unsigned, "proof": proof}
    verifier = CoreNetworkProofVerifier(public_pem)

    await verifier.verify_group_config(signed)
    with pytest.raises(AgentSdkError) as caught:
        await verifier.verify_group_config({**signed, "group_id": "g2"})
    assert caught.value.code is ErrorCode.SIGNATURE_ERROR


def test_embedded_core_network_public_key_is_expected_p256_key():
    key = serialization.load_pem_public_key(embedded_core_network_public_key_pem())
    assert isinstance(key, ec.EllipticCurvePublicKey)
    assert isinstance(key.curve, ec.SECP256R1)
    assert key.public_numbers().x == int(
        "cad12fa5ccbbe4992d1c7282f0220fe98660e711bfd3bb17331df2e345cff09f",
        16,
    )
