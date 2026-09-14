from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Protocol

from .models import (
    ComputingSession,
    NetworkMessageAction,
    NetworkMessageType,
)


@dataclass(frozen=True, slots=True)
class RuntimeHttpResponse:
    status_code: int
    body: Mapping[str, Any]


class ProofVerifier(Protocol):
    async def verify_group_config(self, payload: Mapping[str, Any]) -> None: ...


class MessageSigner(Protocol):
    async def sign_a2a(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


class MessageSignatureVerifier(Protocol):
    async def verify_a2a(
        self, payload: Mapping[str, Any], expected_did_key: str
    ) -> None: ...


class ControlRequestAuthenticator(Protocol):
    async def authenticate(
        self, path: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


class RuntimeTransport(Protocol):
    async def get_ue_info(self) -> Mapping[str, Any]: ...

    async def get_acn_status(self) -> Mapping[str, Any]: ...

    async def start_downlink(
        self,
        handler: Callable[
            [str, int, Mapping[str, Any]], Awaitable[Mapping[str, Any] | None]
        ],
        on_reconnected: Callable[[], Awaitable[None]] | None = None,
    ) -> None: ...

    async def request(
        self, method: str, path: str, body: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    async def request_with_status(
        self, method: str, path: str, body: Mapping[str, Any]
    ) -> RuntimeHttpResponse: ...

    async def close(self) -> None: ...


class SandboxTransport(Protocol):
    """HTTP transport for C-02 Sandbox user-plane endpoints."""

    async def request_with_status(
        self,
        method: str,
        url: str,
        body: Mapping[str, Any] | None,
        timeout_seconds: float,
        source_ipv4: str,
    ) -> RuntimeHttpResponse: ...

    async def close(self) -> None: ...


class TunDevice(Protocol):
    name: str
    cidr: str
    mtu: int

    async def read(self) -> bytes: ...

    async def write(self, packet: bytes) -> None: ...

    async def close(self) -> None: ...


PacketHandler = Callable[[bytes], Awaitable[None]]


class ConnectIpTransport(Protocol):
    @property
    def connected(self) -> bool: ...

    async def start(self, on_packet: PacketHandler) -> None: ...

    async def send_packet(self, packet: bytes) -> None: ...

    async def close(self) -> None: ...


class LocalServer(Protocol):
    async def start(
        self,
        *,
        agent_ip: str,
        tcp_port: int,
        udp_port: int,
        on_a2a_message: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> None: ...

    async def close(self) -> None: ...


class PeerMessenger(Protocol):
    async def send(
        self, endpoint: str, body: Mapping[str, Any], timeout: float
    ) -> Mapping[str, Any]: ...


class NetworkMessageListener(Protocol):
    async def on_network_message(
        self, message_type: NetworkMessageType, payload: Mapping[str, Any]
    ) -> NetworkMessageAction: ...


class GroupMessageListener(Protocol):
    async def on_group_message(
        self, group_id: str, sender_agent_id: str, payload: Mapping[str, Any]
    ) -> None: ...


class VideoUploadHandle(Protocol):
    track_id: str
    state: str

    async def pause(self) -> None: ...

    async def resume(self) -> None: ...

    async def stop(self) -> None: ...


class RemoteVideoStream(Protocol):
    def __aiter__(self) -> AsyncIterator[Any]: ...

    async def recv(self) -> Any: ...

    async def close(self) -> None: ...


class PreparedVideoUpload(Protocol):
    """A local send-only PeerConnection whose non-Trickle Offer is ready."""

    offer_sdp: str

    async def apply_answer(
        self, answer_sdp: str, timeout_seconds: float
    ) -> VideoUploadHandle: ...

    async def abort(self) -> None: ...


class PreparedProcessedVideo(Protocol):
    """A local receive-only PeerConnection whose non-Trickle Offer is ready."""

    offer_sdp: str

    async def apply_answer(
        self, answer_sdp: str, timeout_seconds: float
    ) -> RemoteVideoStream: ...

    async def abort(self) -> None: ...


class MediaOffloadAdapter(Protocol):
    """Platform WebRTC adapter; HTTP signaling is owned by AgentSdk."""

    def supports_video_codec(self, codec: str) -> bool: ...

    async def prepare_video_upload(
        self,
        session: ComputingSession,
        *,
        camera_id: int,
        width: int,
        height: int,
        fps: int,
        bitrate_kbps: int,
    ) -> PreparedVideoUpload: ...

    async def prepare_processed_video(
        self,
        session: ComputingSession,
    ) -> PreparedProcessedVideo: ...

    async def close(self) -> None: ...
