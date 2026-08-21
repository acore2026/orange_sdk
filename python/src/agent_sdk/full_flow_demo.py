"""Run the complete Agent SDK API flow without external infrastructure.

This program uses the real :class:`agent_sdk.AgentSdk` orchestration and replaces
only its operating-system, AgentRuntime, MASQUE, peer, and media boundaries with
deterministic in-memory adapters. It is an installation self-check, not a
production security or networking configuration. It is exposed by the wheel as
the ``agent-sdk-self-check`` command.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from agent_sdk import (
    AgentSdk,
    NetworkMessageAction,
    NetworkMessageType,
)
from agent_sdk.routes import MemoryRouteBackend
from agent_sdk.security import (
    DemoAcceptAllProofVerifier,
    DemoControlRequestAuthenticator,
    DemoMessageSignatureVerifier,
    DemoMessageSigner,
)

LOCAL_AGENT_ID = "did:example:agent-a"
PEER_AGENT_ID = "did:example:agent-b"


class DemoTun:
    name = "agent_tun0"
    cidr = "8.8.8.7/32"
    mtu = 1280

    def __init__(self) -> None:
        self._packets: asyncio.Queue[bytes] = asyncio.Queue()

    async def read(self) -> bytes:
        return await self._packets.get()

    async def write(self, packet: bytes) -> None:
        del packet

    async def close(self) -> None:
        return None


class DemoMasque:
    connected = False

    async def start(self, on_packet) -> None:
        self._on_packet = on_packet
        self.connected = True

    async def send_packet(self, packet: bytes) -> None:
        del packet

    async def close(self) -> None:
        self.connected = False


class DemoRuntime:
    """Returns the original unmodified AgentRuntime wire contracts."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Mapping[str, Any]]] = []
        self.downlink_handler = None

    async def get_ue_agent_ip(self) -> str:
        return "8.8.8.7"

    async def start_downlink(self, handler) -> None:
        self.downlink_handler = handler

    async def push_invitation(self) -> NetworkMessageAction:
        assert self.downlink_handler is not None
        return await self.downlink_handler(
            "ACN_AGENT_GROUPING_INVITATION",
            48,
            {"notification_type": "group_invitation", "group_id": "g-demo"},
        )

    async def push_group_config(self) -> NetworkMessageAction:
        assert self.downlink_handler is not None
        return await self.downlink_handler(
            "ACN_AGENT_GROUP_CONFIG",
            49,
            {
                "notification_type": "acf_group_config",
                "version": "1.0.0",
                "timestamp": _now(),
                "group_id": "g-demo",
                "members": {
                    "agent1": {
                        "agent_id": LOCAL_AGENT_ID,
                        "agent_name": "Agent A",
                        "capabilities": ["text", "camera"],
                        "agent_ip": "8.8.8.7",
                        "tcp_port": "4001",
                        "udp_port": "28443",
                        "did_key": "did:key:local-demo",
                    },
                    "agent2": {
                        "agent_id": PEER_AGENT_ID,
                        "agent_name": "Agent B",
                        "capabilities": ["text", "camera"],
                        "agent_ip": "8.8.8.8",
                        "tcp_port": "4001",
                        "udp_port": "28443",
                        "did_key": "did:key:peer-demo",
                    },
                },
                "proof": {"jws": "demo-group-proof"},
            },
        )

    async def request(
        self, method: str, path: str, body: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.requests.append((method, path, dict(body)))
        if path == "/idm/v1/identity-applications":
            return {
                "result": "success",
                "agent_id": LOCAL_AGENT_ID,
                "vc0": {
                    "id": "vc0-demo",
                    "claims": {
                        "agent_id": LOCAL_AGENT_ID,
                        "agent_name": "Agent A",
                    },
                },
            }
        if path == "/idm/v1/network-ability":
            return {
                "timestamp": _now(),
                "vc1": {
                    "id": "vc1-demo",
                    "valid_until": (
                        datetime.now(timezone.utc) + timedelta(days=1)
                    ).isoformat().replace("+00:00", "Z"),
                    "claims": {
                        "agent_id": LOCAL_AGENT_ID,
                        "abilities": ["agent_discovery", "compute_offloading"],
                    },
                },
            }
        if path == "/arf/v1/agent-discoveries":
            return {
                "task_id": body["task_id"],
                "result": [
                    {
                        "agent_card": {
                            "agent_id": PEER_AGENT_ID,
                            "agent_ip": "8.8.8.8",
                            "tcp_port": "4001",
                            "udp_port": "28443",
                            "skills": ["text", "camera"],
                        },
                        "priority": 1,
                    }
                ],
                "timestamp": _now(),
            }
        if path == "/acf/v1/agents-grouping":
            return {"status": "grouped", "group_id": "g-demo"}
        if path == "/compute/v1/offloading-sessions":
            return {
                "session_id": "session-demo",
                "sandbox_id": "sandbox-demo",
                "state": "CONNECTING",
                "sdp_answer": "demo-sdp-answer",
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=30)
                ).isoformat().replace("+00:00", "Z"),
            }
        return {"success": True, "operation_id": "operation-demo"}

    async def close(self) -> None:
        return None


class DemoLocalServer:
    def __init__(self) -> None:
        self.a2a_handler = None

    async def start(self, **kwargs) -> None:
        self.a2a_handler = kwargs["on_a2a_message"]

    async def push_a2a_message(self) -> None:
        assert self.a2a_handler is not None
        await self.a2a_handler(
            {
                "message_id": "message-from-peer",
                "group_id": "g-demo",
                "sender_agent_id": PEER_AGENT_ID,
                "target_agent_id": LOCAL_AGENT_ID,
                "timestamp": _now(),
                "payload": {"text": "hello from Agent B"},
                "proof": {"jws": "demo-message-proof"},
            }
        )

    async def close(self) -> None:
        return None


class DemoPeerMessenger:
    def __init__(self) -> None:
        self.last_endpoint: tuple[str, int] | None = None

    async def send(self, ip, port, body, timeout):
        del body, timeout
        self.last_endpoint = (ip, port)
        return {"ack": True}


class DemoNetworkListener:
    async def on_network_message(self, message_type, payload):
        del payload
        if message_type is NetworkMessageType.GROUP_INVITATION:
            return NetworkMessageAction.ACCEPT
        return NetworkMessageAction.ACK


class DemoGroupListener:
    def __init__(self) -> None:
        self.received: list[tuple[str, str, Mapping[str, Any]]] = []

    async def on_group_message(self, group_id, sender_agent_id, payload):
        self.received.append((group_id, sender_agent_id, payload))


class DemoVideoUpload:
    track_id = "camera-demo"
    state = "RUNNING"

    async def pause(self) -> None:
        self.state = "PAUSED"

    async def resume(self) -> None:
        self.state = "RUNNING"

    async def stop(self) -> None:
        self.state = "STOPPED"


class DemoVideoStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()

    async def recv(self):
        return b"processed-demo-frame"


class DemoMediaAdapter:
    def __init__(self) -> None:
        self.upload = DemoVideoUpload()
        self.stream = DemoVideoStream()

    async def connect(self, session, signaling, timeout_seconds) -> None:
        del session, signaling, timeout_seconds

    async def start_video_upload(self, session, **kwargs):
        del session, kwargs
        return self.upload

    async def get_processed_video_stream(self, session, timeout_seconds):
        del session, timeout_seconds
        return self.stream

    async def close(self) -> None:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def run_demo(
    *,
    verbose: bool = True,
    log_file_path: str = "./logs/agent-sdk-self-check.log",
) -> dict[str, Any]:
    """Run every primary northbound API and return verification details."""

    tun = DemoTun()
    masque = DemoMasque()
    runtime = DemoRuntime()
    local_server = DemoLocalServer()
    peer_messenger = DemoPeerMessenger()
    route_backend = MemoryRouteBackend()
    media = DemoMediaAdapter()
    group_listener = DemoGroupListener()

    async def tun_factory(name: str, cidr: str, mtu: int) -> DemoTun:
        tun.name, tun.cidr, tun.mtu = name, cidr, mtu
        return tun

    sdk = AgentSdk(
        _proof_verifier=DemoAcceptAllProofVerifier(),
        _control_request_authenticator=DemoControlRequestAuthenticator(),
        _message_signer=DemoMessageSigner(),
        _message_signature_verifier=DemoMessageSignatureVerifier(),
        peer_messenger=peer_messenger,
        tun_factory=tun_factory,
        masque_factory=lambda config: masque,
        runtime_factory=lambda host, port: runtime,
        server_factory=lambda: local_server,
        route_backend_factory=lambda config, device: route_backend,
        media_offload_adapter=media,
    )
    sdk.register_network_message_listener(DemoNetworkListener())
    sdk.register_group_message_listener(group_listener)

    def show(step: str, value: Any) -> None:
        if verbose:
            print(f"[{step}] {value}")

    try:
        initialized = await sdk.init(
            "192.168.3.10",
            8080,
            "192.168.1.10",
            4001,
            28443,
            masque_server_url="https://192.168.3.10:4433",
            masque_authorization="Bearer demo-device-a-token",
            log_file_path=log_file_path,
        )
        show("1 init", initialized)

        profile = await sdk.apply_identity(
            owner="demo-owner",
            name="Agent A",
            description="wheel installation self-check",
            metadata={"platform": "Linux"},
        )
        show("2 apply_identity", profile.agent_id)

        ability = await sdk.get_network_ability(profile.agent_id)
        show("3 get_network_ability", ability.abilities)

        registration = await sdk.register_capabilities(
            profile.agent_id,
            priority=1,
            credentials=[profile.identity_vc, ability.ability_vc],
        )
        show("4 register_capabilities", registration.success)

        update = await sdk.update_capabilities(
            profile.agent_id,
            update_items=[
                {
                    "update_type": "add_skill",
                    "skill_name": "camera",
                    "reference_vc_id": ability.ability_vc["id"],
                }
            ],
            credentials=[ability.ability_vc],
        )
        show("5 update_capabilities", update.success)

        discovered = await sdk.discover_agents(
            task_id="task-demo",
            agent_id=profile.agent_id,
            task_description="send a demo message",
            required_skills=["text"],
            max_results=5,
        )
        show("6 discover_agents", [item.agent_id for item in discovered])

        group = await sdk.create_group(
            profile.agent_id,
            [discovered[0].agent_id],
            group_name="demo-group",
            max_members=2,
        )
        show("7 create_group", group.group_id)

        invitation_action = await runtime.push_invitation()
        config_action = await runtime.push_group_config()
        snapshot = await sdk.get_group_snapshot(group.group_id)
        assert snapshot is not None
        show("8 group callback", f"{config_action.value}, generation={snapshot.generation}")

        receipt = await sdk.send_message(
            group.group_id,
            discovered[0].agent_id,
            {"type": "text", "content": "hello Agent B"},
        )
        show("9 send_message", receipt.delivered)

        await local_server.push_a2a_message()
        show("10 receive message", group_listener.received[-1][2])

        session = await sdk.create_offloading_session(
            profile.agent_id,
            task_type="video_rendering",
            sandbox_id="sandbox-demo",
        )
        upload = await sdk.start_video_upload(
            session.session_id,
            width=1280,
            height=720,
            fps=30,
            bitrate_kbps=2500,
        )
        await upload.pause()
        await upload.resume()
        stream = await sdk.get_processed_video_stream(session.session_id)
        frame = await stream.recv()
        await upload.stop()
        show("11 media offload", f"{session.state}, frame={frame!r}")

        deregistration = await sdk.deregister_identity(profile.agent_id)
        show("12 deregister_identity", deregistration.success)

        summary = {
            "runtime_request_count": len(runtime.requests),
            "group_id": group.group_id,
            "peer_endpoint": peer_messenger.last_endpoint,
            "installed_route": "8.8.8.8/32" in route_backend.routes,
            "received_message_count": len(group_listener.received),
            "invitation_action": invitation_action.value,
            "message_delivered": receipt.delivered,
            "media_state": upload.state,
        }
        assert summary == {
            "runtime_request_count": 8,
            "group_id": "g-demo",
            "peer_endpoint": ("8.8.8.8", 4001),
            "installed_route": True,
            "received_message_count": 1,
            "invitation_action": "ACCEPT",
            "message_delivered": True,
            "media_state": "STOPPED",
        }
        if verbose:
            print("FULL FLOW DEMO PASSED")
        return summary
    finally:
        await sdk.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run an offline full-flow Agent SDK installation self-check."
    )
    parser.parse_args()
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()
