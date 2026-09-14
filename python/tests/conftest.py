from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest

from agent_sdk import AgentSdk, NetworkMessageAction, RuntimeHttpResponse
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

    async def get_ue_info(self) -> Mapping[str, Any]:
        self.ue_info_requests += 1
        return {
            "identity": {"supi": "imsi-001010000000001"},
            "nas": {
                "registered": True,
                "state": "session_ready",
                "security_context": True,
            },
            "pdu_sessions": [{
                "pdu_session_id": 1,
                "state": "active",
                "type": "IPv4",
                "dnn": "internet",
                "snssai": {"sst": 1, "sd": "010203"},
                "ipv4": "8.8.8.7",
                "default_route": True,
            }],
            "data_plane_accesses": [{
                "access_type": "HTTP3_CONNECT_IP",
                "endpoint_template": "https://runtime.example/masque/{pdu_session_id}",
                "session_selection": "EXACT_PDU_SESSION_ID",
            }],
        }

    async def get_acn_status(self) -> Mapping[str, Any]:
        return {"ready": True}

    async def start_downlink(self, handler, on_reconnected=None) -> None:
        self.downlink_handler = handler
        self.on_reconnected = on_reconnected

    async def deliver_downlink(
        self,
        message_type: str,
        payload: Mapping[str, Any],
        transaction_id: int = 49,
    ) -> Mapping[str, Any] | None:
        assert self.downlink_handler is not None
        return await self.downlink_handler(message_type, transaction_id, payload)

    async def deliver_group_config(
        self, payload: Mapping[str, Any]
    ) -> NetworkMessageAction:
        response = await self.deliver_downlink(
            "ACN_AGENT_GROUPING_NOTIFICATION", payload
        )
        assert response is not None
        return NetworkMessageAction(response["result"])

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
        return {"success": True, "operation_id": "op-1"}

    async def request_with_status(self, method, path, body):
        if path == "/v1/computing/session-requests":
            self.requests.append((method, path, body))
            status_code = 202 if body["request_type"] == "CREATE" else 200
            return RuntimeHttpResponse(status_code, {
                "message_type": "COMPUTE_SESSION_STATUS",
                "request_id": body["request_id"],
                "compute_service_session_id": "css-001",
                "status_revision": "1",
                "status": "ACCEPTED",
                "cause": "",
            })
        return RuntimeHttpResponse(200, await self.request(method, path, body))

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
    closed = False

    async def recv(self):
        return b"frame"

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()

    async def close(self):
        self.closed = True


class FakePreparedMedia:
    def __init__(self, role, result):
        direction = "sendonly" if role == "producer" else "recvonly"
        self.offer_sdp = (
            "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
            f"a={direction}\r\n"
            "a=candidate:1 1 UDP 1 8.8.8.7 50000 typ host\r\n"
            "a=end-of-candidates\r\n"
        )
        self.result = result
        self.answer_sdp = None
        self.aborted = False

    async def apply_answer(self, answer_sdp, timeout_seconds):
        self.answer_sdp = answer_sdp
        return self.result

    async def abort(self):
        self.aborted = True


class FakeMediaAdapter:
    def __init__(self):
        self.upload_args = None
        self.stream_args = None
        self.closed = False
        self.upload = FakeVideoUpload()
        self.stream = FakeRemoteVideoStream()

    def supports_video_codec(self, codec):
        return codec.upper() in {"H264", "VP8"}

    async def prepare_video_upload(self, session, **kwargs):
        self.upload_args = (
            session.compute_service_session_id,
            kwargs,
        )
        self.prepared = FakePreparedMedia("producer", self.upload)
        return self.prepared

    async def prepare_processed_video(self, session):
        self.stream_args = (
            session.compute_service_session_id,
        )
        self.prepared = FakePreparedMedia("consumer", self.stream)
        return self.prepared

    async def close(self):
        self.closed = True


class FakeSandboxTransport:
    def __init__(self):
        self.requests = []
        self.closed = False
        self.recognition_target = None
        self.control_action = None

    async def request_with_status(
        self, method, url, body, timeout_seconds, source_ipv4
    ):
        self.requests.append((method, url, body, timeout_seconds, source_ipv4))
        if method == "DELETE":
            return RuntimeHttpResponse(204, {})
        if "/v1/recognition-targets/" in url:
            if method == "PUT":
                context = body["computing_context"]
                self.recognition_target = {
                    "request_id": body["request_id"],
                    "computing_context": context,
                    "status": "APPLIED",
                    "target_revision": "1",
                    "target": {"label": "红色玩偶", "prompt": "red toy"},
                }
                return RuntimeHttpResponse(200, self.recognition_target)
            if method == "GET" and self.recognition_target is not None:
                return RuntimeHttpResponse(200, self.recognition_target)
            return RuntimeHttpResponse(
                404,
                {"error": {"code": "recognition-target-not-set", "message": "not set"}},
            )
        if "/v1/control-actions" in url:
            if method == "POST":
                context = body["computing_context"]
                if body["input"]["type"] == "TEXT":
                    normalized_action = "search_object"
                    normalized_parameters = {"query": "cup"}
                else:
                    normalized_action = body["action"]
                    normalized_parameters = body["parameters"]
                self.control_action = {
                    "request_id": body["request_id"],
                    "action_id": "action-001",
                    "computing_context": context,
                    "normalized_action": normalized_action,
                    "normalized_parameters": normalized_parameters,
                    "status": "RUNNING",
                    "result": {"phase": "started"},
                    "cause": "",
                }
                return RuntimeHttpResponse(202, self.control_action)
            if method == "GET" and self.control_action is not None:
                result = dict(self.control_action)
                result.pop("computing_context")
                result["status"] = "COMPLETED"
                return RuntimeHttpResponse(200, result)
            return RuntimeHttpResponse(
                404,
                {"error": {"code": "action-not-found", "message": "not found"}},
            )
        context = body["computing_context"]
        return RuntimeHttpResponse(201, {
            "request_id": body["request_id"],
            "computing_context": context,
            "media_connection_id": f"media-{context['role']}-001",
            "answer": {
                "type": "answer",
                "sdp": (
                    "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
                    f"a={'recvonly' if context['role'] == 'producer' else 'sendonly'}\r\n"
                    "a=candidate:2 1 UDP 1 8.8.8.10 51000 typ host\r\n"
                    "a=end-of-candidates\r\n"
                ),
            },
        })

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


async def _create_sdk_fixture(
    tmp_path,
    *,
    restore_profile: bool,
):
    tun = FakeTun()
    masque = FakeMasque()
    runtime = FakeRuntime()
    runtime_targets = []
    server = FakeServer()
    backend = MemoryRouteBackend()
    proof = FakeProofVerifier()
    messenger = FakePeerMessenger()
    signature_verifier = FakeSignatureVerifier()
    media = FakeMediaAdapter()
    sandbox = FakeSandboxTransport()

    async def tun_factory(name, cidr, mtu):
        tun.name = name
        tun.cidr = cidr
        tun.mtu = mtu
        return tun

    def runtime_factory(host, port):
        runtime_targets.append((host, port))
        return runtime

    sdk = AgentSdk(
        _proof_verifier=proof,
        _control_request_authenticator=FakeControlRequestAuthenticator(),
        peer_messenger=messenger,
        _message_signature_verifier=signature_verifier,
        _message_signer=FakeMessageSigner(),
        tun_factory=tun_factory,
        masque_factory=lambda config: masque,
        runtime_factory=runtime_factory,
        server_factory=lambda: server,
        route_backend_factory=lambda config, tun_device: backend,
        _media_offload_adapter=media,
        _sandbox_transport=sandbox,
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
        "runtime_targets": runtime_targets,
        "server": server,
        "backend": backend,
        "proof": proof,
        "messenger": messenger,
        "signature_verifier": signature_verifier,
        "media": media,
        "sandbox": sandbox,
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
