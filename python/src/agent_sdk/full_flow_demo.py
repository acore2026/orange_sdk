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
from pathlib import Path
from typing import Any, Mapping

from agent_sdk import (
    AcnContext,
    AgentSdk,
    ComputeConstraints,
    ComputeInputFormat,
    ComputeRequestType,
    ComputeResources,
    ComputeSessionRequest,
    NetworkMessageAction,
    NetworkMessageType,
    RuntimeHttpResponse,
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
    """Returns the formal AgentRuntime wire contracts."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Mapping[str, Any]]] = []
        self.downlink_handler = None

    async def get_ue_info(self) -> Mapping[str, Any]:
        return {
            "identity": {"supi": "imsi-001010000000001"},
            "nas": {
                "registered": True,
                "state": "session_ready",
                "security_context": True,
            },
            "pdu_sessions": [
                {
                    "pdu_session_id": 1,
                    "state": "active",
                    "type": "IPv4",
                    "dnn": "internet",
                    "snssai": {"sst": 1, "sd": "010203"},
                    "ipv4": "8.8.8.7",
                    "default_route": True,
                }
            ],
            "data_plane_accesses": [
                {
                    "access_type": "HTTP3_CONNECT_IP",
                    "endpoint_template": (
                        "https://192.168.3.10:4433/masque/{pdu_session_id}"
                    ),
                    "session_selection": "EXACT_PDU_SESSION_ID",
                }
            ],
        }

    async def get_acn_status(self) -> Mapping[str, Any]:
        return {"ready": True}

    async def start_downlink(self, handler, on_reconnected=None) -> None:
        self.downlink_handler = handler
        self.on_reconnected = on_reconnected

    async def push_invitation(self) -> NetworkMessageAction:
        assert self.downlink_handler is not None
        response = await self.downlink_handler(
            "ACN_AGENT_GROUPING_INVITATION",
            48,
            {"notification_type": "group_invitation", "group_id": "g-demo"},
        )
        assert response is not None
        return NetworkMessageAction(response["result"])

    async def push_group_config(self) -> NetworkMessageAction:
        assert self.downlink_handler is not None
        response = await self.downlink_handler(
            "ACN_AGENT_GROUPING_NOTIFICATION",
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
                        "skills": ["text", "camera"],
                        "agent_ip": "8.8.8.7",
                        "service_endpoints": "http://agent-a:4001/A2A/message",
                    },
                    "agent2": {
                        "agent_id": PEER_AGENT_ID,
                        "agent_name": "Agent B",
                        "skills": ["text", "camera"],
                        "agent_ip": "8.8.8.8",
                        "service_endpoints": "http://agent-b:4001/A2A/message",
                    },
                },
                "proof": {"jws": "demo-group-proof"},
            },
        )
        assert response is not None
        return NetworkMessageAction(response["result"])

    async def push_compute_config(self) -> Mapping[str, Any]:
        assert self.downlink_handler is not None
        response = await self.downlink_handler(
            "COMPUTE_CONNECT_CONFIG",
            50,
            {
                "compute_service_session_id": "css-demo",
                "compute_instance_id": "ci-demo",
                "binding_ref": "binding-css-demo",
                "role": "consumer",
                "receiver_agent_id": LOCAL_AGENT_ID,
                "service_endpoint": "http://8.8.8.9:8788",
                "network_binding": {
                    "pdu_session_id": 1,
                    "dnn": "internet",
                    "snssai": {"sst": 1, "sd": "010203"},
                    "ue_ipv4": "8.8.8.7",
                    "runtime_data_plane": {
                        "access_type": "HTTP3_CONNECT_IP",
                        "session_selection": "EXACT_PDU_SESSION_ID",
                    },
                },
                "connection_parameters": {
                    "media_connections_path": "/v1/media-connections",
                    "transport": "WEBRTC",
                },
            },
        )
        assert response is not None
        return response

    async def push_compute_close(self) -> Mapping[str, Any]:
        assert self.downlink_handler is not None
        response = await self.downlink_handler(
            "COMPUTE_SESSION_CLOSE",
            51,
            {
                "compute_service_session_id": "css-demo",
                "compute_instance_id": "ci-demo",
                "binding_ref": "binding-css-demo",
                "role": "consumer",
                "receiver_agent_id": LOCAL_AGENT_ID,
                "cause": "released",
            },
        )
        assert response is not None
        return response

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
                "task_description": body["task_description"],
                "result": [
                    {
                        "agent_card": {
                            "agent_id": PEER_AGENT_ID,
                            "service_endpoints": "http://agent-b:4001/A2A/message",
                            "skills": ["text", "camera"],
                        },
                        "priority": 1,
                    }
                ],
                "timestamp": _now(),
            }
        if path == "/acf/v1/agents-grouping":
            return {"status": "grouped", "group_id": "g-demo"}
        return {"success": True, "operation_id": "operation-demo"}

    async def request_with_status(
        self, method: str, path: str, body: Mapping[str, Any]
    ) -> RuntimeHttpResponse:
        if path != "/v1/computing/session-requests":
            return RuntimeHttpResponse(200, await self.request(method, path, body))
        self.requests.append((method, path, dict(body)))
        return RuntimeHttpResponse(
            202,
            {
                "message_type": "COMPUTE_SESSION_STATUS",
                "request_id": body["request_id"],
                "compute_service_session_id": "css-demo",
                "status_revision": "1",
                "status": "ACCEPTED",
                "cause": "",
            },
        )

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
                "src_agent_id": PEER_AGENT_ID,
                "dst_agent_id": LOCAL_AGENT_ID,
                "type": "text",
                "task_id": "task-demo",
                "timestamp": _now(),
                "payload": {"text": "hello from Agent B"},
            }
        )

    async def close(self) -> None:
        return None


class DemoPeerMessenger:
    def __init__(self) -> None:
        self.last_endpoint: str | None = None

    async def send(self, endpoint, body, timeout):
        del body, timeout
        self.last_endpoint = endpoint
        return {"status": "OK"}


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

    async def close(self):
        return None


class DemoPreparedMedia:
    def __init__(self, result) -> None:
        self.offer_sdp = (
            "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
            "a=recvonly\r\n"
            "a=candidate:1 1 UDP 1 8.8.8.7 50000 typ host\r\n"
        )
        self.result = result

    async def apply_answer(self, answer_sdp, timeout_seconds):
        del answer_sdp, timeout_seconds
        return self.result

    async def abort(self):
        return None


class DemoMediaAdapter:
    def __init__(self) -> None:
        self.upload = DemoVideoUpload()
        self.stream = DemoVideoStream()

    def supports_video_codec(self, codec):
        return codec.upper() in {"H264", "VP8"}

    async def prepare_video_upload(self, session, **kwargs):
        del session, kwargs
        prepared = DemoPreparedMedia(self.upload)
        prepared.offer_sdp = prepared.offer_sdp.replace("a=recvonly", "a=sendonly")
        return prepared

    async def prepare_processed_video(self, session):
        del session
        return DemoPreparedMedia(self.stream)

    async def close(self) -> None:
        return None


class DemoSandboxTransport:
    async def request_with_status(
        self, method, url, body, timeout_seconds, source_ipv4
    ):
        del url, timeout_seconds, source_ipv4
        if method == "DELETE":
            return RuntimeHttpResponse(204, {})
        context = body["computing_context"]
        return RuntimeHttpResponse(201, {
            "request_id": body["request_id"],
            "computing_context": context,
            "media_connection_id": "media-demo",
            "answer": {
                "type": "answer",
                "sdp": (
                    "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
                    "a=sendonly\r\n"
                    "a=candidate:2 1 UDP 1 8.8.8.9 51000 typ host\r\n"
                ),
            },
        })

    async def close(self):
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
        _media_offload_adapter=media,
        _sandbox_transport=DemoSandboxTransport(),
        agent_state_directory=(
            Path(log_file_path).resolve().parent
            / f"{Path(log_file_path).stem}-agent-state"
        ),
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
            local_tcp_port=4001,
            local_udp_port=28443,
            masque_server_url="https://192.168.3.10:4433",
            masque_authorization="Bearer demo-device-a-token",
            log_file_path=log_file_path,
        )
        show("1 init", initialized)

        profile = await sdk.apply_identity(
            owner="demo-owner",
            name="Agent A",
            description="wheel installation self-check",
            metadata={"region": "CN", "os": "Linux", "version": "0.17.8"},
        )
        show("2 apply_identity", profile.agent_id)

        ability = await sdk.get_network_ability(profile.agent_id)
        show("3 get_network_ability", ability.abilities)

        registration = await sdk.register_capabilities(
            profile.agent_id,
            priority=1,
            credentials=[ability.ability_vc],
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
            dnn="internet",
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
            message_type="text",
            task_id="task-demo",
        )
        show("9 send_message", receipt.delivered)

        await local_server.push_a2a_message()
        show("10 receive message", group_listener.received[-1][2])

        status = await sdk.create_computing_session(
            ComputeSessionRequest(
                message_type="COMPUTE_SESSION_REQUEST",
                request_type=ComputeRequestType.CREATE,
                input_format=ComputeInputFormat.STRUCTURED,
                request_id="create-demo",
                acn_context=AcnContext(
                    group_id=group.group_id,
                    requester_agent_id=profile.agent_id,
                    target_agent_id=discovered[0].agent_id,
                ),
                constraints=ComputeConstraints(
                    capability_id="video_rendering",
                    resources=ComputeResources(
                        cpu_millicores=2000,
                        memory_mib=4096,
                    ),
                    dnn="internet",
                    allow_base_qos=True,
                ),
            )
        )
        assert status.compute_service_session_id == "css-demo"
        config_ack = await runtime.push_compute_config()
        assert config_ack["accepted"] is True
        await sdk.send_message(
            group.group_id,
            discovered[0].agent_id,
            {"compute_service_session_id": status.compute_service_session_id},
            message_type="computing_video_session",
            task_id="task-demo",
        )
        stream = await sdk.get_processed_video_stream(status.compute_service_session_id)
        frame = await stream.recv()
        show("11 media offload", f"{status.status}, frame={frame!r}")

        await sdk.release_computing_session(
            ComputeSessionRequest(
                message_type="COMPUTE_SESSION_REQUEST",
                request_type=ComputeRequestType.RELEASE,
                input_format=ComputeInputFormat.STRUCTURED,
                request_id="release-demo",
                compute_service_session_id=status.compute_service_session_id,
            )
        )
        close_ack = await runtime.push_compute_close()
        assert close_ack["closed"] is True
        await sdk.await_computing_session_closed(status.compute_service_session_id)
        show("12 computing session closed", True)

        deregistration = await sdk.deregister_identity(profile.agent_id)
        show("13 deregister_identity", deregistration.success)

        summary = {
            "runtime_request_count": len(runtime.requests),
            "group_id": group.group_id,
            "peer_endpoint": peer_messenger.last_endpoint,
            "installed_route": "8.8.8.8/32" in route_backend.routes,
            "received_message_count": len(group_listener.received),
            "invitation_action": invitation_action.value,
            "message_delivered": receipt.delivered,
            "media_state": "STREAM_READY",
        }
        assert summary == {
            "runtime_request_count": 9,
            "group_id": "g-demo",
            "peer_endpoint": "http://agent-b:4001/A2A/message",
            "installed_route": True,
            "received_message_count": 1,
            "invitation_action": "ACCEPT",
            "message_delivered": True,
            "media_state": "STREAM_READY",
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
