from __future__ import annotations

from agent_sdk import NetworkMessageAction

from conftest import AckNetworkListener, PEER_ID, group_payload


class RecordingGroupListener:
    def __init__(self) -> None:
        self.messages = []

    async def on_group_message(self, group_id, sender_agent_id, payload):
        self.messages.append((group_id, sender_agent_id, payload))


async def test_a2a_signature_key_comes_from_committed_member(sdk_fixture):
    sdk = sdk_fixture["sdk"]
    server = sdk_fixture["server"]
    sdk.register_network_message_listener(AckNetworkListener())
    listener = RecordingGroupListener()
    sdk.register_group_message_listener(listener)
    assert await server.group_config(group_payload()) is NetworkMessageAction.ACK

    await server.a2a(
        {
            "message_id": "m1",
            "group_id": "g1",
            "sender_agent_id": PEER_ID,
            "target_agent_id": "did:example:agent-a",
            "payload": {"hello": "world"},
            "proof": {"jws": "example"},
        }
    )

    assert sdk_fixture["signature_verifier"].keys == ["did:key:peer"]
    assert listener.messages == [("g1", PEER_ID, {"hello": "world"})]

