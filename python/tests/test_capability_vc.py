from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from agent_sdk import AgentSdkError, ErrorCode
from agent_sdk.capability_vc import (
    TEST_CAPABILITY_ISSUER_DID,
    TEST_CAPABILITY_ISSUER_KEY_ID,
    issue_test_capability_vcs,
)


def _write_private_key(tmp_path):
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_key_path = tmp_path / "third-party-private-key.pem"
    private_key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return private_key, private_key_path


def _signing_message(credential):
    payload = {
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
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def test_issue_test_capability_vcs_matches_idm_signature_format(tmp_path):
    private_key, private_key_path = _write_private_key(tmp_path)

    credentials = issue_test_capability_vcs(
        agent_id="did:example:agent-a",
        agent_name="Agent Alpha",
        capabilities=["robot-control", "voice"],
        private_key_path=private_key_path,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert len(credentials) == 2
    assert [item["claims"]["capability"] for item in credentials] == [
        "robot-control",
        "voice",
    ]
    for credential in credentials:
        assert credential["context"] == ["3gpp-ts-33.xxx-v20.0.0"]
        assert credential["type"] == [
            "VerifiableCredential",
            "CapabilityCredential",
        ]
        assert credential["issuer"] == TEST_CAPABILITY_ISSUER_DID
        assert credential["valid_from"] == "2026-08-20T00:00:00Z"
        assert credential["valid_until"] == "2027-08-20T00:00:00Z"
        assert credential["claims"]["agent_id"] == "did:example:agent-a"
        assert credential["claims"]["agent_name"] == "Agent Alpha"
        assert credential["claims"]["authorization_mode"] == "Mode2"
        assert credential["proof"]["creator"] == TEST_CAPABILITY_ISSUER_KEY_ID
        private_key.public_key().verify(
            base64.b64decode(credential["proof"]["signature_value"]),
            _signing_message(credential),
            ec.ECDSA(hashes.SHA256()),
        )

    credentials[0]["claims"]["capability"] = "tampered"
    with pytest.raises(InvalidSignature):
        private_key.public_key().verify(
            base64.b64decode(credentials[0]["proof"]["signature_value"]),
            _signing_message(credentials[0]),
            ec.ECDSA(hashes.SHA256()),
        )


async def test_register_capabilities_accepts_existing_vcs_and_raw_capabilities(
    sdk_fixture,
    tmp_path,
):
    _, private_key_path = _write_private_key(tmp_path)
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]

    result = await sdk.register_capabilities(
        "did:example:agent-a",
        priority=2,
        credentials=[{"id": "vc0", "type": ["VerifiableCredential"]}],
        capabilities=["robot-control", "voice"],
        test_vc_private_key_path=private_key_path,
    )

    method, path, body = runtime.requests[-1]
    assert result.success is True
    assert (method, path) == ("POST", "/arf/v1/agent-cards")
    assert body["vc_list"][0]["id"] == "vc0"
    assert [item["claims"]["capability"] for item in body["vc_list"][1:]] == [
        "robot-control",
        "voice",
    ]
    assert all(
        item["claims"]["agent_name"] == "Agent A"
        for item in body["vc_list"][1:]
    )
    assert body["timestamp"] == "2026-08-19T00:00:00Z"
    assert body["signature"] == "test-signature"


async def test_register_raw_capabilities_requires_agent_name_without_profile(
    sdk_fixture,
    tmp_path,
):
    _, private_key_path = _write_private_key(tmp_path)

    with pytest.raises(AgentSdkError) as captured:
        await sdk_fixture["sdk"].register_capabilities(
            "did:example:different-agent",
            priority=1,
            capabilities=["text"],
            test_vc_private_key_path=private_key_path,
        )

    assert captured.value.code is ErrorCode.INVALID_ARGUMENT
    assert captured.value.field == "agent_name"


def test_test_capability_vc_rejects_invalid_inputs_and_missing_key(tmp_path):
    with pytest.raises(AgentSdkError) as duplicate:
        issue_test_capability_vcs(
            agent_id="did:example:agent-a",
            agent_name="Agent Alpha",
            capabilities=["text", "text"],
            private_key_path=tmp_path / "missing.pem",
        )
    assert duplicate.value.code is ErrorCode.INVALID_ARGUMENT
    assert duplicate.value.field == "capabilities"

    with pytest.raises(AgentSdkError) as missing_key:
        issue_test_capability_vcs(
            agent_id="did:example:agent-a",
            agent_name="Agent Alpha",
            capabilities=["text"],
            private_key_path=tmp_path / "missing.pem",
        )
    assert missing_key.value.code is ErrorCode.SIGNATURE_ERROR
    assert missing_key.value.field == "test_vc_private_key_path"
