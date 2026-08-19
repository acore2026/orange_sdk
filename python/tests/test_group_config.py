from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_sdk import AgentSdkError, ErrorCode, NetworkMessageAction

from conftest import AckNetworkListener, PEER_ID, group_payload


async def test_group_config_caches_by_agent_id_and_installs_route(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    server = sdk_fixture["server"]
    backend = sdk_fixture["backend"]
    listener = AckNetworkListener()
    sdk.register_network_message_listener(listener)

    action = await server.group_config(group_payload())

    assert action is NetworkMessageAction.ACK
    snapshot = await sdk.get_group_snapshot("g1")
    assert snapshot is not None
    assert set(snapshot.members_by_agent_id) == {
        "did:example:agent-a",
        PEER_ID,
    }
    member = snapshot.members_by_agent_id[PEER_ID]
    assert member.agent_ip == "8.8.8.8"
    assert member.tcp_port == 4001
    assert member.udp_port == 28443
    assert "8.8.8.8/32" in backend.routes
    assert "8.8.8.7/32" not in backend.routes


async def test_listener_reject_does_not_commit_or_add_route(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    server = sdk_fixture["server"]
    backend = sdk_fixture["backend"]
    sdk.register_network_message_listener(
        AckNetworkListener(NetworkMessageAction.REJECT)
    )

    action = await server.group_config(group_payload())

    assert action is NetworkMessageAction.REJECT
    assert await sdk.get_group_snapshot("g1") is None
    assert backend.routes == set()


@pytest.mark.parametrize("invalid_port", ["0", "65536", "not-a-port", 4001])
async def test_invalid_string_port_rejects_entire_config(sdk_fixture, invalid_port):
    sdk = sdk_fixture["sdk"]
    server = sdk_fixture["server"]
    sdk.register_network_message_listener(AckNetworkListener())
    payload = group_payload()
    payload["members"]["arbitrary-label"]["tcp_port"] = invalid_port

    with pytest.raises(AgentSdkError) as exc:
        await server.group_config(payload)

    assert exc.value.code is ErrorCode.GROUP_CONFIG_INVALID
    assert await sdk.get_group_snapshot("g1") is None


async def test_stale_config_keeps_current_snapshot(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    server = sdk_fixture["server"]
    sdk.register_network_message_listener(AckNetworkListener())
    now = datetime.now(timezone.utc)
    await server.group_config(group_payload(timestamp=now))

    with pytest.raises(AgentSdkError) as exc:
        await server.group_config(
            group_payload(timestamp=now - timedelta(seconds=1), peer_ip="8.8.8.9")
        )

    assert exc.value.code is ErrorCode.GROUP_CONFIG_STALE
    snapshot = await sdk.get_group_snapshot("g1")
    assert snapshot is not None
    assert snapshot.members_by_agent_id[PEER_ID].agent_ip == "8.8.8.8"


async def test_local_agent_ip_must_match_tun(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    server = sdk_fixture["server"]
    sdk.register_network_message_listener(AckNetworkListener())
    payload = group_payload()
    payload["members"]["agent1"]["agent_ip"] = "8.8.8.99"

    with pytest.raises(AgentSdkError) as exc:
        await server.group_config(payload)

    assert exc.value.code is ErrorCode.AGENT_IP_MISMATCH
    assert await sdk.get_group_snapshot("g1") is None


async def test_group_config_requires_semantic_version(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    server = sdk_fixture["server"]
    sdk.register_network_message_listener(AckNetworkListener())
    payload = group_payload()
    payload["version"] = "1"

    with pytest.raises(AgentSdkError) as exc:
        await server.group_config(payload)

    assert exc.value.code is ErrorCode.GROUP_CONFIG_INVALID


async def test_send_message_uses_only_cached_tcp_endpoint(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    server = sdk_fixture["server"]
    messenger = sdk_fixture["messenger"]
    sdk.register_network_message_listener(AckNetworkListener())
    await server.group_config(
        group_payload(peer_ip="8.8.8.8", peer_tcp_port="4567")
    )

    receipt = await sdk.send_message("g1", PEER_ID, {"command": "patrol"})

    assert receipt.delivered is True
    assert len(messenger.calls) == 1
    ip, port, body, _ = messenger.calls[0]
    assert (ip, port) == ("8.8.8.8", 4567)
    assert body["target_agent_id"] == PEER_ID


async def test_send_without_group_config_never_falls_back(sdk_fixture):
    sdk = sdk_fixture["sdk"]

    with pytest.raises(AgentSdkError) as exc:
        await sdk.send_message("g1", PEER_ID, {"hello": "world"})

    assert exc.value.code is ErrorCode.GROUP_NOT_ACTIVE
    assert sdk_fixture["messenger"].calls == []
