from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest

from agent_sdk import AgentSdk, NetworkMessageAction
from agent_sdk.models import AgentProfile, NetworkMessageType
from agent_sdk.routes import MemoryRouteBackend


LOCAL_ID = "did:example:agent-a"
PEER_ID = "did:example:agent-b"


class FakeTun:
    name = "agent_tun0"
    cidr = "8.8.8.7/32"
    mtu = 1280

    def __init__(self) -> None:
        self.read_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.writes: list[bytes] = []
        self.closed = False

    async def read(self) -> bytes:
        return await self.read_queue.get()

    async def write(self, packet: bytes) -> None:
        self.writes.append(packet)

    async def close(self) -> None:
        self.closed = True


class FakeMasque:
    connected = False

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.on_packet = None

    async def start(self, on_packet) -> None:
        self.on_packet = on_packet
        self.connected = True

    async def send_packet(self, packet: bytes) -> None:
        self.sent.append(packet)

    async def close(self) -> None:
        self.connected = False


class FakeRuntime:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Mapping[str, Any]]] = []
        self.ue_info_requests = 0
        self.closed = False
        self.downlink_handler = None

    async def get_ue_agent_ip(self) -> str:
        self.ue_info_requests += 1
        return "8.8.8.7"

    async def start_downlink(self, handler) -> None:
        self.downlink_handler = handler

    async def deliver_downlink(
        self,
        message_type: str,
        payload: Mapping[str, Any],
        transaction_id: int = 49,
    ) -> NetworkMessageAction:
        assert self.downlink_handler is not None
        return await self.downlink_handler(message_type, transaction_id, payload)

    async def deliver_group_config(
        self, payload: Mapping[str, Any]
    ) -> NetworkMessageAction:
        return await self.deliver_downlink(
            "ACN_AGENT_GROUPING_NOTIFICATION", payload
        )

    async def request(self, method: str, path: str, body: Mapping[str, Any]):
        self.requests.append((method, path, body))
        if path == "/idm/v1/identity-applications":
            return {
                "result": "success",
                "agent_id": LOCAL_ID,
                "vc0": {
                    "id": "vc-a",
                    "claims": {"agent_id": LOCAL_ID, "agent_name": "Agent A"},
                },
            }
        if path == "/idm/v1/network-ability":
            return {
                "timestamp": "2026-08-19T00:00:01Z",
                "vc1": {
                    "id": "vc-network-a",
                    "valid_until": "2027-08-19T00:00:00Z",
                    "claims": {
                        "agent_id": LOCAL_ID,
                        "network_abilities": [
                            "compute_offloading",
                            "agent_discovery",
                        ],
                    },
                },
            }
        if path == "/arf/v1/agent-discoveries":
            return {
                "task_description": "Patrol Area A",
                "result": [
                    {
                        "agent_card": {
                            "agent_id": PEER_ID,
                            "service_endpoints": "http://agent-b:4001/A2A/message",
                            "skills": ["camera"],
                        },
                        "priority": 1,
                    }
                ],
                "timestamp": "2026-08-19T00:00:01Z",
            }
        if path == "/acf/v1/agents-grouping":
            return {"status": "grouped", "group_id": "g1"}
        if path == "/compute/v1/offloading-sessions":
            return {
                "session_id": "session-1",
                "sandbox_id": "sandbox-edge-1",
                "state": "CONNECTING",
                "sdp_answer": "test-answer",
                "expires_at": "2026-08-18T12:00:00Z",
            }
        return {"success": True, "operation_id": "op-1"}

    async def close(self) -> None:
        self.closed = True


class FakeServer:
    def __init__(self) -> None:
        self.started = False
        self.a2a = None
        self.arguments = None

    async def start(self, **kwargs) -> None:
        self.started = True
        self.arguments = kwargs
        self.a2a = kwargs["on_a2a_message"]

    async def close(self) -> None:
        self.started = False


class FakeProofVerifier:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def verify_group_config(self, payload: Mapping[str, Any]) -> None:
        self.calls += 1
        if self.fail:
            raise ValueError("bad proof")


class AckNetworkListener:
    def __init__(self, action: NetworkMessageAction = NetworkMessageAction.ACK) -> None:
        self.action = action
        self.messages: list[tuple[NetworkMessageType, Mapping[str, Any]]] = []

    async def on_network_message(self, message_type, payload):
        self.messages.append((message_type, payload))
        return self.action


class FakePeerMessenger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any], float]] = []

    async def send(self, endpoint, body, timeout):
        self.calls.append((endpoint, body, timeout))
        return {"status": "OK"}


class FakeSignatureVerifier:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def verify_a2a(self, payload, expected_did_key):
        self.keys.append(expected_did_key)


class FakeMessageSigner:
    async def sign_a2a(self, payload):
        return {"jws": "test-message-signature"}


class FakeControlRequestAuthenticator:
    async def authenticate(self, path, payload):
        del payload
        if path in {
            "/idm/v1/identity-applications",
        }:
            return {
                "timestamp": "2026-08-19T00:00:00Z",
                "signature": "test-signature",
                "signature_encoding": "base64",
            }
        return {
            "timestamp": "2026-08-19T00:00:00Z",
            "proof": {"jws": "test-proof"},
        }


class FakeVideoUpload:
    track_id = "camera-track-1"
    state = "RUNNING"

    async def pause(self):
        self.state = "PAUSED"

    async def resume(self):
        self.state = "RUNNING"

    async def stop(self):
        self.state = "STOPPED"


class FakeRemoteVideoStream:
    async def recv(self):
        return b"frame"

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()


class FakeMediaAdapter:
    def __init__(self):
        self.connected_session = None
        self.upload_args = None
        self.closed = False
        self.upload = FakeVideoUpload()
        self.stream = FakeRemoteVideoStream()

    async def connect(self, session, signaling, timeout_seconds):
        assert signaling["sdp_answer"] == "test-answer"
        self.connected_session = session.session_id

    async def start_video_upload(self, session, **kwargs):
        self.upload_args = (session.session_id, kwargs)
        return self.upload

    async def get_processed_video_stream(self, session, timeout_seconds):
        return self.stream

    async def close(self):
        self.closed = True


def group_payload(
    *,
    timestamp: datetime | None = None,
    peer_ip: str = "8.8.8.8",
    peer_tcp_port: str = "4001",
) -> dict[str, Any]:
    timestamp = timestamp or datetime.now(timezone.utc)
    return {
        "notification_type": "acf_group_config",
        "version": "1.0.0",
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "group_id": "g1",
        "members": {
            "agent1": {
                "agent_id": LOCAL_ID,
                "agent_name": "Agent A",
                "skills": ["text"],
                "agent_ip": "8.8.8.7",
                "service_endpoints": "http://agent-a.example:4001/A2A/message",
            },
            "arbitrary-label": {
                "agent_id": PEER_ID,
                "agent_name": "Agent B",
                "skills": ["text", "voice"],
                "agent_ip": peer_ip,
                "service_endpoints": (
                    f"http://agent-b.example:{peer_tcp_port}/A2A/message"
                ),
            },
        },
        "proof": {"jws": "test-proof"},
    }


async def _create_sdk_fixture(tmp_path, *, restore_profile: bool):
    tun = FakeTun()
    masque = FakeMasque()
    runtime = FakeRuntime()
    server = FakeServer()
    backend = MemoryRouteBackend()
    proof = FakeProofVerifier()
    messenger = FakePeerMessenger()
    signature_verifier = FakeSignatureVerifier()
    media = FakeMediaAdapter()

    async def tun_factory(name, cidr, mtu):
        tun.name = name
        tun.cidr = cidr
        tun.mtu = mtu
        return tun

    sdk = AgentSdk(
        _proof_verifier=proof,
        _control_request_authenticator=FakeControlRequestAuthenticator(),
        peer_messenger=messenger,
        _message_signature_verifier=signature_verifier,
        _message_signer=FakeMessageSigner(),
        tun_factory=tun_factory,
        masque_factory=lambda config: masque,
        runtime_factory=lambda host, port: runtime,
        server_factory=lambda: server,
        route_backend_factory=lambda config, tun_device: backend,
        media_offload_adapter=media,
        agent_state_directory=tmp_path / "agent-state",
    )
    result = await sdk.init(
        "192.168.3.10",
        8080,
        "192.168.1.10",
        4001,
        28443,
        masque_server_url="https://192.168.3.10:4433",
        log_file_path=str(tmp_path / "agent-sdk.log"),
    )
    if restore_profile:
        sdk.set_local_profile_for_restore(
            AgentProfile(LOCAL_ID, "Agent A", {"id": "vc-a"})
        )
    return {
        "sdk": sdk,
        "result": result,
        "tun": tun,
        "masque": masque,
        "runtime": runtime,
        "server": server,
        "backend": backend,
        "proof": proof,
        "messenger": messenger,
        "signature_verifier": signature_verifier,
        "media": media,
        "log_path": tmp_path / "agent-sdk.log",
    }


@pytest.fixture
async def sdk_fixture(tmp_path):
    fixture = await _create_sdk_fixture(tmp_path, restore_profile=True)
    yield fixture
    await fixture["sdk"].close()


@pytest.fixture
async def sdk_without_profile_fixture(tmp_path):
    fixture = await _create_sdk_fixture(tmp_path, restore_profile=False)
    yield fixture
    await fixture["sdk"].close()
