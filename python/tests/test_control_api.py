from __future__ import annotations

import httpx

from agent_sdk.runtime import HttpRuntimeTransport


async def test_identity_uses_raw_request_and_vc0_response(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]

    profile = await sdk.apply_identity(
        "Alice",
        "AliceAgent",
        "public-key",
        "AgentModel-X",
        {"os": "Linux"},
    )

    method, path, body = runtime.requests[-1]
    assert (method, path) == ("POST", "/idm/v1/identity-applications")
    assert body == {
        "owner": "Alice",
        "name": "AliceAgent",
        "public_key": "public-key",
        "description": "AgentModel-X",
        "metadata": {"os": "Linux"},
        "timestamp": "2026-08-19T00:00:00Z",
        "signature": "test-signature",
        "signature_encoding": "base64",
    }
    assert profile.agent_name == "Agent A"
    assert profile.identity_vc["id"] == "vc-a"


async def test_network_ability_uses_raw_vc1_response(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]

    ability = await sdk.get_network_ability("did:example:agent-a")

    method, path, body = runtime.requests[-1]
    assert (method, path) == ("POST", "/idm/v1/network-ability")
    assert body == {
        "agent_id": "did:example:agent-a",
        "intent": "Get Network Ability VC",
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
    assert body["request_id"].startswith("urn:uuid:")
    assert body["request_type"] == "agent_registration_update"
    assert body["agent_id"] == "did:example:agent-a"
    assert body["update_items"] == update_items
    assert body["credentials"] == credentials
    assert body["timestamp"] == "2026-08-19T00:00:00Z"
    assert body["proof"] == {"jws": "test-proof"}


async def test_discovery_parses_raw_result_agent_card(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]

    agents = await sdk.discover_agents(
        "task-1",
        "did:example:agent-a",
        "Patrol Area A",
        ["camera"],
    )

    method, path, body = runtime.requests[-1]
    assert (method, path) == ("POST", "/arf/v1/agent-discoveries")
    assert body["timestamp"] == "2026-08-19T00:00:00Z"
    assert body["proof"] == {"jws": "test-proof"}
    assert len(agents) == 1
    assert agents[0].agent_id == "did:example:agent-b"
    assert (agents[0].ip, agents[0].tcp_port, agents[0].udp_port) == (
        "8.8.8.8",
        4001,
        28443,
    )


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
    assert body == {
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


async def test_legacy_control_requests_include_original_signature_fields(
    sdk_fixture,
):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]

    await sdk.register_capabilities(
        "did:example:agent-a", 2, [{"id": "vc-camera"}]
    )
    _, publish_path, publish_body = runtime.requests[-1]
    assert publish_path == "/arf/v1/agent-cards"
    assert publish_body["timestamp"] == "2026-08-19T00:00:00Z"
    assert publish_body["signature"] == "test-signature"
    assert publish_body["signature_encoding"] == "base64"

    await sdk.deregister_identity("did:example:agent-a")
    _, deregister_path, deregister_body = runtime.requests[-1]
    assert deregister_path == "/acn-agent/v1/agent-deletions"
    assert deregister_body["reason"] == "retired"
    assert deregister_body["timestamp"] == "2026-08-19T00:00:00Z"
    assert deregister_body["signature"] == "test-signature"
    assert deregister_body["signature_encoding"] == "base64"


async def test_runtime_transport_accepts_empty_success_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request)

    transport = HttpRuntimeTransport("runtime.example", 443)
    await transport._client.aclose()
    transport._client = httpx.AsyncClient(
        base_url="https://runtime.example:443",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await transport.request("POST", "/arf/v1/agent-cards", {}) == {}
    finally:
        await transport.close()
