from __future__ import annotations

import pytest

from agent_sdk import AgentSdkError, ErrorCode, NetworkMessageAction

from conftest import AckNetworkListener, PEER_ID, group_payload


class RecordingGroupListener:
    def __init__(self) -> None:
        self.messages = []

    async def on_group_message(self, group_id, sender_agent_id, payload):
        self.messages.append((group_id, sender_agent_id, payload))


async def test_a2a_delivery_uses_committed_member_and_current_contract(
    sdk_fixture,
):
    sdk = sdk_fixture["sdk"]
    server = sdk_fixture["server"]
    runtime = sdk_fixture["runtime"]
    sdk.register_network_message_listener(AckNetworkListener())
    listener = RecordingGroupListener()
    sdk.register_group_message_listener(listener)
    assert (
        await runtime.deliver_group_config(group_payload())
        is NetworkMessageAction.ACK
    )

    await server.a2a(
        {
            "message_id": "m1",
            "group_id": "g1",
            "src_agent_id": PEER_ID,
            "dst_agent_id": "did:example:agent-a",
            "type": "text",
            "task_id": "task-patrol",
            "timestamp": "2026-08-21T09:00:00Z",
            "payload": {"hello": "world"},
        }
    )

    assert sdk_fixture["signature_verifier"].keys == []
    assert listener.messages == [("g1", PEER_ID, {"hello": "world"})]


async def test_a2a_delivery_rejects_legacy_proof_field(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    server = sdk_fixture["server"]
    runtime = sdk_fixture["runtime"]
    sdk.register_network_message_listener(AckNetworkListener())
    sdk.register_group_message_listener(RecordingGroupListener())
    assert (
        await runtime.deliver_group_config(group_payload())
        is NetworkMessageAction.ACK
    )

    with pytest.raises(AgentSdkError) as caught:
        await server.a2a(
            {
                "message_id": "m1",
                "group_id": "g1",
                "src_agent_id": PEER_ID,
                "dst_agent_id": "did:example:agent-a",
                "type": "text",
                "task_id": "task-patrol",
                "timestamp": "2026-08-21T09:00:00Z",
                "payload": {"hello": "world"},
                "proof": {"jws": "legacy"},
            }
        )

    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    assert caught.value.field == "proof"
