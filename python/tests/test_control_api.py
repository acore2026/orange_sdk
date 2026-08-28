from __future__ import annotations

import base64
import uuid
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from agent_sdk import AgentSdkError, ErrorCode
from agent_sdk.runtime import HttpRuntimeTransport


def _ue_info_response() -> dict:
    return {
        "identity": {
            "supi": "imsi-001010000000001",
            "imei": "356938035643803",
            "imeisv": "3569380356438031",
        },
        "serving_plmn": {"mcc": "001", "mnc": "01"},
        "nas": {
            "state": "session_ready",
            "registered": True,
            "security_context": True,
        },
        "pdu_sessions": [
            {
                "pdu_session_id": 1,
                "state": "active",
                "dnn": "internet",
                "type": "IPv4",
                "snssai": {"sst": 1, "sd": "010203"},
                "ssc_mode": 1,
                "ipv4": "10.60.0.11",
                "auto_establish": True,
                "default_route": True,
            }
        ],
    }


async def test_identity_uses_raw_request_and_vc0_response(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]

    profile = await sdk.apply_identity(
        "Alice",
        "AliceAgent",
        "AgentModel-X",
        {"region": "CN", "os": "Linux", "version": "0.12.0"},
    )

    method, path, body = runtime.requests[-1]
    assert (method, path) == ("POST", "/idm/v1/identity-applications")
    public_key = serialization.load_der_public_key(base64.b64decode(body["public_key"]))
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    assert isinstance(public_key.curve, ec.SECP256R1)
    uuid.UUID(body["request_id"])
    assert body == {
        "request_id": body["request_id"],
        "owner": "Alice",
        "name": "AliceAgent",
        "public_key": body["public_key"],
        "description": "AgentModel-X",
        "metadata": {"region": "CN", "os": "Linux", "version": "0.12.0"},
        "timestamp": "2026-08-19T00:00:00Z",
        "signature": "test-signature",
        "signature_encoding": "base64",
    }
    assert profile.agent_name == "Agent A"
    assert profile.identity_vc["id"] == "vc-a"


async def test_identity_accepts_string_metadata_and_normalizes_field_order(
    sdk_fixture,
):
    await sdk_fixture["sdk"].apply_identity(
        "Alice",
        "AliceAgent",
        "AgentModel-X",
        {
            "zone": "north",
            "version": "0.12.0",
            "os": "Linux",
            "region": "CN",
            "platform": "edge",
        },
    )

    body = sdk_fixture["runtime"].requests[-1][2]
    assert body["metadata"] == {
        "region": "CN",
        "os": "Linux",
        "version": "0.12.0",
        "platform": "edge",
        "zone": "north",
    }


async def test_identity_rejects_non_string_metadata_value(sdk_fixture):
    with pytest.raises(AgentSdkError) as caught:
        await sdk_fixture["sdk"].apply_identity(
            "Alice",
            "AliceAgent",
            "AgentModel-X",
            {
                "region": "CN",
                "os": "Linux",
                "version": "0.12.0",
                "priority": 1,
            },
        )

    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    assert caught.value.field == "metadata"
    assert sdk_fixture["runtime"].requests == []


async def test_network_ability_uses_raw_vc1_response(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]

    ability = await sdk.get_network_ability("did:example:agent-a")

    method, path, body = runtime.requests[-1]
    assert (method, path) == ("POST", "/idm/v1/network-ability")
    uuid.UUID(body["request_id"])
    assert body == {
        "request_id": body["request_id"],
        "agent_id": "did:example:agent-a",
        "intent": "Issue Network Ability Credential",
        "timestamp": "2026-08-19T00:00:00Z",
        "proof": {"jws": "test-proof"},
    }
    assert ability.ability_vc["id"] == "vc-network-a"
    assert ability.abilities == ("compute_offloading", "agent_discovery")
    assert ability.valid_until is not None


async def test_update_capabilities_uses_original_body_and_new_endpoint(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    update_items = [
        {
            "update_type": "add_skill",
            "skill_name": "camera",
            "reference_vc_id": "vc-camera-002",
        }
    ]
    credentials = [{"id": "vc-camera-002"}]

    await sdk.update_capabilities(
        "did:example:agent-a",
        update_items,
        credentials,
    )

    method, path, body = runtime.requests[-1]
    assert method == "POST"
    assert path == "/arf/v1/agent-cards-update"
    uuid.UUID(body["request_id"])
    assert "request_type" not in body
    assert body["agent_id"] == "did:example:agent-a"
    assert body["update_items"] == update_items
    assert body["credentials"] == credentials
    assert body["timestamp"] == "2026-08-19T00:00:00Z"
    assert body["proof"] == {"jws": "test-proof"}


async def test_discovery_parses_raw_result_agent_card(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]

    agents = await sdk.discover_agents(
        agent_id="did:example:agent-a",
        task_description="Patrol Area A",
        required_skills=["camera"],
    )

    method, path, body = runtime.requests[-1]
    assert (method, path) == ("POST", "/arf/v1/agent-discoveries")
    uuid.UUID(body["request_id"])
    assert "task_id" not in body
    assert body["timestamp"] == "2026-08-19T00:00:00Z"
    assert body["proof"] == {"jws": "test-proof"}
    assert len(agents) == 1
    assert agents[0].agent_id == "did:example:agent-b"
    assert agents[0].service_endpoints == "http://agent-b:4001/A2A/message"
    assert agents[0].skills == ("camera",)


async def test_create_group_preserves_nested_original_body(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]

    group = await sdk.create_group(
        "did:example:agent-a",
        ["did:example:agent-b"],
        "task-patrol",
        max_members=2,
    )

    method, path, body = runtime.requests[-1]
    assert (method, path) == ("POST", "/acf/v1/agents-grouping")
    uuid.UUID(body["request_id"])
    assert body == {
        "request_id": body["request_id"],
        "agent_id": "did:example:agent-a",
        "target_agents": ["did:example:agent-b"],
        "group_config": {
            "group_name": "task-patrol",
            "scope": "private",
            "max_members": 2,
        },
        "timestamp": "2026-08-19T00:00:00Z",
        "proof": {"jws": "test-proof"},
    }
    assert group.group_id == "g1"


async def test_control_requests_use_identity_signature_and_other_proofs(
    sdk_fixture,
):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]

    await sdk.register_capabilities(
        "did:example:agent-a", 2, [{"id": "vc-camera"}]
    )
    _, publish_path, publish_body = runtime.requests[-1]
    assert publish_path == "/arf/v1/agent-cards"
    uuid.UUID(publish_body["request_id"])
    assert publish_body["timestamp"] == "2026-08-19T00:00:00Z"
    assert publish_body["service_endpoints"] == (
        "http://8.8.8.7:4001/A2A/message"
    )
    assert publish_body["proof"] == {"jws": "test-proof"}
    assert "signature" not in publish_body

    await sdk.deregister_identity("did:example:agent-a")
    _, deregister_path, deregister_body = runtime.requests[-1]
    assert deregister_path == "/acn-agent/v1/agent-deletions"
    uuid.UUID(deregister_body["request_id"])
    assert deregister_body["reason"] == "retired"
    assert deregister_body["timestamp"] == "2026-08-19T00:00:00Z"
    assert deregister_body["proof"] == {"jws": "test-proof"}
    assert "signature" not in deregister_body


async def test_runtime_transport_accepts_empty_success_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request)

    transport = HttpRuntimeTransport("runtime.example", 443)
    await transport._client.aclose()
    transport._client = httpx.AsyncClient(
        base_url="http://runtime.example:443",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await transport.request("POST", "/arf/v1/agent-cards", {}) == {}
    finally:
        await transport.close()


async def test_runtime_transport_uses_plain_http_base_url():
    transport = HttpRuntimeTransport("runtime.example", 8080)
    try:
        assert transport._base_url == "http://runtime.example:8080"
        assert str(transport._client.base_url) == "http://runtime.example:8080"
    finally:
        await transport.close()


async def test_ue_info_uses_exact_get_without_body_and_returns_pdu_ipv4():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, request=request, json=_ue_info_response())

    transport = HttpRuntimeTransport("runtime.example", 8080)
    await transport._client.aclose()
    transport._client = httpx.AsyncClient(
        base_url="http://runtime.example:8080",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await transport.get_ue_agent_ip() == "10.60.0.11"
    finally:
        await transport.close()

    assert captured[0].method == "GET"
    assert captured[0].url == "http://runtime.example:8080/v1/ue/info"
    assert captured[0].content == b""
    assert captured[0].headers["content-type"] == "application/json"


@pytest.mark.parametrize(
    "mutate,field",
    [
        (lambda body: body["nas"].update(registered=False), "nas.registered"),
        (lambda body: body["nas"].update(state="registered"), "nas.state"),
        (
            lambda body: body["nas"].update(security_context=False),
            "nas.security_context",
        ),
        (lambda body: body.update(pdu_sessions=[]), "pdu_sessions"),
        (
            lambda body: body["pdu_sessions"][0].update(default_route=False),
            "pdu_sessions",
        ),
        (
            lambda body: body["pdu_sessions"][0].update(ipv4="not-an-ip"),
            "pdu_sessions.ipv4",
        ),
    ],
)
async def test_ue_info_rejects_unready_or_invalid_assignment(mutate, field):
    response_body = _ue_info_response()
    mutate(response_body)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json=response_body)

    transport = HttpRuntimeTransport("runtime.example", 8080)
    await transport._client.aclose()
    transport._client = httpx.AsyncClient(
        base_url="http://runtime.example:8080",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(AgentSdkError) as raised:
            await transport.get_ue_agent_ip()
    finally:
        await transport.close()

    assert raised.value.code is ErrorCode.RUNTIME_REJECTED
    assert raised.value.field == field
