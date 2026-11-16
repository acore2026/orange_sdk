from __future__ import annotations

import json

import pytest

from agent_sdk import AgentLifecycleState, AgentProfile, AgentSdkError, ErrorCode
from agent_sdk.agent_state import (
    AgentCardContext,
    AgentStateStore,
    IdentityApplicationContext,
)


async def test_agent_lifecycle_persists_transitions_and_replaces_duplicate_card(
    sdk_without_profile_fixture,
):
    sdk = sdk_without_profile_fixture["sdk"]
    runtime = sdk_without_profile_fixture["runtime"]

    assert sdk.agent_lifecycle_state is AgentLifecycleState.NO_IDENTITY
    assert sdk.local_profile is None

    profile = await sdk.apply_identity(
        "Alice",
        "AliceAgent",
        "AgentModel-X",
        {"region": "CN", "os": "Linux", "version": "0.14.0"},
    )
    assert sdk.agent_lifecycle_state is AgentLifecycleState.IDENTITY_READY
    assert sdk.local_profile == profile

    replacement = await sdk.apply_identity(
        "Alice",
        "AliceAgentReplacement",
        "AgentModel-X replacement",
        {"region": "CN", "os": "Linux", "version": "0.14.0"},
    )
    assert sdk.agent_lifecycle_state is AgentLifecycleState.IDENTITY_READY
    assert sdk.local_profile == replacement
    assert [request[1] for request in runtime.requests[-2:]] == [
        "/acn-agent/v1/agent-deletions",
        "/idm/v1/identity-applications",
    ]
    profile = replacement

    result = await sdk.register_capabilities(
        profile.agent_id,
        priority=1,
        credentials=[{"id": "vc-network-a"}],
    )
    assert result.success is True
    assert sdk.agent_lifecycle_state is AgentLifecycleState.CARD_PUBLISHED

    request_count = len(runtime.requests)
    repeated = await sdk.register_capabilities(
        profile.agent_id,
        priority=2,
        credentials=[{"id": "vc-network-b"}],
    )
    assert repeated.success is True
    assert sdk.agent_lifecycle_state is AgentLifecycleState.CARD_PUBLISHED
    assert [request[1] for request in runtime.requests[request_count:]] == [
        "/acn-agent/v1/agent-deletions",
        "/idm/v1/identity-applications",
        "/arf/v1/agent-cards",
    ]
    profile = sdk.local_profile
    assert profile is not None

    request_count = len(runtime.requests)
    updated = await sdk.update_capabilities(
        profile.agent_id,
        update_items=[{
            "update_type": "add_skill",
            "skill_name": "camera",
            "reference_vc_id": "vc-camera",
        }],
        credentials=[{"id": "vc-camera", "claims": {"skill_name": "camera"}}],
    )
    assert updated.success is True
    assert sdk.agent_lifecycle_state is AgentLifecycleState.CARD_PUBLISHED
    assert [request[1] for request in runtime.requests[request_count:]] == [
        "/arf/v1/agent-cards-update",
    ]
    assert sdk.local_profile == profile

    deregistered = await sdk.deregister_identity(profile.agent_id)
    assert deregistered.success is True
    assert sdk.agent_lifecycle_state is AgentLifecycleState.NO_IDENTITY
    assert sdk.local_profile is None


async def test_identity_ready_can_deregister_without_publishing_card(
    sdk_without_profile_fixture,
):
    sdk = sdk_without_profile_fixture["sdk"]
    profile = await sdk.apply_identity(
        "Alice",
        "AliceAgent",
        "AgentModel-X",
        {"region": "CN", "os": "Linux", "version": "0.14.0"},
    )

    await sdk.deregister_identity(profile.agent_id)

    assert sdk.agent_lifecycle_state is AgentLifecycleState.NO_IDENTITY
    assert sdk.local_profile is None


async def test_reset_agent_is_idempotent_in_state_one(sdk_without_profile_fixture):
    sdk = sdk_without_profile_fixture["sdk"]
    runtime = sdk_without_profile_fixture["runtime"]
    request_count = len(runtime.requests)

    result = await sdk.reset_agent()

    assert result.success is True
    assert result.message == "Agent is already in NO_IDENTITY state"
    assert len(runtime.requests) == request_count
    assert sdk.agent_lifecycle_state is AgentLifecycleState.NO_IDENTITY
    assert sdk.local_profile is None


async def test_reset_agent_clears_published_identity_locally_without_http(
    sdk_fixture,
):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    profile = sdk.local_profile
    assert profile is not None
    await sdk.register_capabilities(
        profile.agent_id,
        priority=1,
        credentials=[{"id": "vc-network-a"}],
    )
    runtime.requests.clear()

    result = await sdk.reset_agent()

    assert result.success is True
    assert result.message == (
        "Local Agent state reset to NO_IDENTITY; network identity was not changed"
    )
    assert runtime.requests == []
    assert sdk.agent_lifecycle_state is AgentLifecycleState.NO_IDENTITY
    assert sdk.local_profile is None


async def test_reset_agent_clears_identity_ready_state_locally_without_http(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    runtime.requests.clear()

    result = await sdk.reset_agent()

    assert result.success is True
    assert runtime.requests == []
    assert sdk.agent_lifecycle_state is AgentLifecycleState.NO_IDENTITY
    assert sdk.local_profile is None


async def test_invalid_card_replacement_is_rejected_before_deregistration(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    profile = sdk.local_profile
    assert profile is not None
    await sdk.register_capabilities(
        profile.agent_id,
        priority=1,
        credentials=[{"id": "vc-network-a"}],
    )
    runtime.requests.clear()

    with pytest.raises(AgentSdkError) as invalid:
        await sdk.register_capabilities(
            profile.agent_id,
            priority=2,
            credentials=[{
                "id": "vc-external-bound",
                "issuer": "did:example:external-issuer",
                "claims": {
                    "agent_id": profile.agent_id,
                    "skill_name": "camera",
                },
            }],
        )

    assert invalid.value.code is ErrorCode.CREDENTIAL_EXPIRED
    assert runtime.requests == []
    assert sdk.agent_lifecycle_state is AgentLifecycleState.CARD_PUBLISHED
    assert sdk.local_profile == profile


async def test_invalid_replacement_does_not_deregister_current_identity(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    request_count = len(runtime.requests)

    with pytest.raises(AgentSdkError) as invalid:
        await sdk.apply_identity(
            "Alice",
            "AliceAgentReplacement",
            "AgentModel-X replacement",
            {"region": "CN", "os": "Linux", "version": 1},
        )

    assert invalid.value.code is ErrorCode.INVALID_ARGUMENT
    assert sdk.agent_lifecycle_state is AgentLifecycleState.IDENTITY_READY
    assert sdk.local_profile is not None
    assert len(runtime.requests) == request_count


def test_agent_state_store_is_scoped_by_runtime_and_validates_tun_ip(tmp_path):
    store = AgentStateStore(tmp_path / "state")
    profile_data = {
        "schema_version": 2,
        "runtime": {"host": "192.168.3.10", "port": 8088},
        "agent_tun_ip": "10.60.0.2",
        "state": "IDENTITY_READY",
        "profile": {
            "agent_id": "did:example:a",
            "agent_name": "Agent A",
            "identity_vc": {"id": "vc0-a"},
        },
        "identity_application": {
            "owner": "Alice",
            "name": "Agent A",
            "description": "test",
            "metadata": {"region": "CN", "os": "Linux", "version": "0.14.0"},
        },
    }
    path_a = store.state_file("192.168.3.10", 8088)
    path_b = store.state_file("192.168.3.10", 8089)
    assert path_a != path_b
    path_a.parent.mkdir(parents=True)
    path_a.write_text(json.dumps(profile_data), encoding="utf-8")

    restored = store.load("192.168.3.10", 8088, "10.60.0.2")

    assert restored.state is AgentLifecycleState.IDENTITY_READY
    assert restored.profile is not None
    assert restored.profile.agent_id == "did:example:a"
    with pytest.raises(AgentSdkError) as mismatch:
        store.load("192.168.3.10", 8088, "10.60.0.99")
    assert mismatch.value.code is ErrorCode.AGENT_STATE_INVALID

    store.save(
        "192.168.3.10",
        8089,
        "10.60.0.3",
        AgentLifecycleState.CARD_PUBLISHED,
        AgentProfile("did:example:b", "Agent B", {"id": "vc0-b"}),
        IdentityApplicationContext(
            "Bob",
            "Agent B",
            "test",
            {"region": "CN", "os": "Linux", "version": "0.15.0"},
        ),
        AgentCardContext(1, ({"id": "vc-network-b"},)),
    )
    restored_card = store.load("192.168.3.10", 8089, "10.60.0.3")
    assert restored_card.state is AgentLifecycleState.CARD_PUBLISHED
    assert restored_card.agent_card is not None
    assert restored_card.agent_card.vc_list[0]["id"] == "vc-network-b"
