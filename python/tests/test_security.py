from __future__ import annotations

import base64
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
    canonical_json,
    embedded_core_network_public_key_pem,
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


async def test_legacy_control_signature_covers_business_body_and_timestamp(tmp_path: Path):
    identity = DeviceSigningIdentityStore(tmp_path / "security").ensure()
    authenticator = DeviceControlRequestAuthenticator(
        DeviceSigningIdentityStore(tmp_path / "security")
    )
    payload = {"owner": "Alice", "name": "Agent A", "public_key": "key"}

    authentication = await authenticator.authenticate(
        "/idm/v1/identity-applications", payload
    )
    signed_document = {**payload, "timestamp": authentication["timestamp"]}
    identity.public_key.verify(
        base64.b64decode(authentication["signature"]),
        canonical_json(signed_document),
        ec.ECDSA(hashes.SHA256()),
    )
    assert authentication["signature_encoding"] == "base64"


async def test_a2a_detached_jws_verifies_with_sender_did_key(tmp_path: Path):
    store = DeviceSigningIdentityStore(tmp_path / "security")
    identity = store.ensure()
    unsigned = {
        "message_id": "m1",
        "group_id": "g1",
        "sender_agent_id": "a1",
        "target_agent_id": "a2",
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
