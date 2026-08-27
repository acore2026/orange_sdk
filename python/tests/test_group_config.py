from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_sdk import AgentSdkError, ErrorCode, NetworkMessageAction

from conftest import AckNetworkListener, PEER_ID, group_payload


async def test_group_config_caches_by_agent_id_and_installs_route(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    backend = sdk_fixture["backend"]
    listener = AckNetworkListener()
    sdk.register_network_message_listener(listener)

    action = await runtime.deliver_group_config(group_payload())

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
    assert member.skills == ("text", "voice")
    assert member.service_endpoint == "http://8.8.8.8:4001/A2A/message"
    assert "8.8.8.8/32" in backend.routes
    assert "8.8.8.7/32" not in backend.routes


async def test_group_config_commits_without_listener(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    backend = sdk_fixture["backend"]
    messenger = sdk_fixture["messenger"]

    action = await runtime.deliver_group_config(group_payload())
    receipt = await sdk.send_message(
        "g1", PEER_ID, {"command": "patrol"},
        message_type="control", task_id="task-patrol",
    )

    assert action is NetworkMessageAction.ACK
    assert receipt.delivered is True
    snapshot = await sdk.get_group_snapshot("g1")
    assert snapshot is not None
    assert snapshot.members_by_agent_id[PEER_ID].agent_ip == "8.8.8.8"
    assert "8.8.8.8/32" in backend.routes
    assert messenger.calls[0][0] == "http://8.8.8.8:4001/A2A/message"


async def test_listener_reject_is_only_a_notification_result(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    backend = sdk_fixture["backend"]
    sdk.register_network_message_listener(
        AckNetworkListener(NetworkMessageAction.REJECT)
    )

    action = await runtime.deliver_group_config(group_payload())

    assert action is NetworkMessageAction.ACK
    assert await sdk.get_group_snapshot("g1") is not None
    assert "8.8.8.8/32" in backend.routes


async def test_listener_failure_does_not_roll_back_group_config(sdk_fixture):
    class FailingListener:
        async def on_network_message(self, message_type, payload):
            raise RuntimeError("application callback failed")

    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    backend = sdk_fixture["backend"]
    sdk.register_network_message_listener(FailingListener())

    action = await runtime.deliver_group_config(group_payload())

    assert action is NetworkMessageAction.ACK
    assert await sdk.get_group_snapshot("g1") is not None
    assert "8.8.8.8/32" in backend.routes


@pytest.mark.parametrize("invalid_port", ["0", "65536", "not-a-port"])
async def test_invalid_service_endpoint_port_rejects_entire_config(
    sdk_fixture, invalid_port
):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    sdk.register_network_message_listener(AckNetworkListener())
    payload = group_payload()
    payload["members"]["arbitrary-label"]["service_endpoints"] = (
        f"http://agent-b.example:{invalid_port}/A2A/message"
    )

    with pytest.raises(AgentSdkError) as exc:
        await runtime.deliver_group_config(payload)

    assert exc.value.code is ErrorCode.GROUP_CONFIG_INVALID
    assert await sdk.get_group_snapshot("g1") is None


async def test_stale_config_keeps_current_snapshot(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    sdk.register_network_message_listener(AckNetworkListener())
    now = datetime.now(timezone.utc)
    await runtime.deliver_group_config(group_payload(timestamp=now))

    with pytest.raises(AgentSdkError) as exc:
        await runtime.deliver_group_config(
            group_payload(timestamp=now - timedelta(seconds=1), peer_ip="8.8.8.9")
        )

    assert exc.value.code is ErrorCode.GROUP_CONFIG_STALE
    snapshot = await sdk.get_group_snapshot("g1")
    assert snapshot is not None
    assert snapshot.members_by_agent_id[PEER_ID].agent_ip == "8.8.8.8"


async def test_local_agent_ip_must_match_tun(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    sdk.register_network_message_listener(AckNetworkListener())
    payload = group_payload()
    payload["members"]["agent1"]["agent_ip"] = "8.8.8.99"

    with pytest.raises(AgentSdkError) as exc:
        await runtime.deliver_group_config(payload)

    assert exc.value.code is ErrorCode.AGENT_IP_MISMATCH
    assert await sdk.get_group_snapshot("g1") is None


async def test_group_config_target_must_match_local_agent(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    payload = group_payload()
    payload["target_agent_id"] = PEER_ID

    with pytest.raises(AgentSdkError) as exc:
        await runtime.deliver_group_config(payload)

    assert exc.value.code is ErrorCode.GROUP_CONFIG_INVALID
    assert exc.value.field == "target_agent_id"


async def test_group_config_requires_semantic_version(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    sdk.register_network_message_listener(AckNetworkListener())
    payload = group_payload()
    payload["version"] = "1"

    with pytest.raises(AgentSdkError) as exc:
        await runtime.deliver_group_config(payload)

    assert exc.value.code is ErrorCode.GROUP_CONFIG_INVALID


async def test_send_message_uses_only_cached_tcp_endpoint(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    runtime = sdk_fixture["runtime"]
    messenger = sdk_fixture["messenger"]
    sdk.register_network_message_listener(AckNetworkListener())
    await runtime.deliver_group_config(
        group_payload(peer_ip="8.8.8.8", peer_tcp_port="4567")
    )

    receipt = await sdk.send_message(
        "g1", PEER_ID, {"command": "patrol"},
        message_type="control", task_id="task-patrol",
    )

    assert receipt.delivered is True
    assert len(messenger.calls) == 1
    endpoint, body, _ = messenger.calls[0]
    assert endpoint == "http://8.8.8.8:4567/A2A/message"
    assert body["dst_agent_id"] == PEER_ID
    assert body["src_agent_id"] == "did:example:agent-a"
    assert body["type"] == "control"
    assert body["task_id"] == "task-patrol"


async def test_send_without_group_config_never_falls_back(sdk_fixture):
    sdk = sdk_fixture["sdk"]

    with pytest.raises(AgentSdkError) as exc:
        await sdk.send_message(
            "g1", PEER_ID, {"hello": "world"},
            message_type="text", task_id="task-patrol",
        )

    assert exc.value.code is ErrorCode.GROUP_NOT_ACTIVE
    assert sdk_fixture["messenger"].calls == []
