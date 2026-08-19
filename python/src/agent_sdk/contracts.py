from __future__ import annotations

from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Protocol

from .models import NetworkMessageAction, NetworkMessageType, OffloadingSession


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
    async def connect(self) -> None: ...

    async def register_endpoint(
        self, local_ip: str, tcp_port: int, udp_port: int
    ) -> str: ...

    async def request(
        self, method: str, path: str, body: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

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
        physical_ip: str,
        agent_ip: str,
        tcp_port: int,
        udp_port: int,
        on_group_config: Callable[[Mapping[str, Any]], Awaitable[NetworkMessageAction]],
        on_group_invitation: Callable[
            [Mapping[str, Any]], Awaitable[NetworkMessageAction]
        ],
        on_a2a_message: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> None: ...

    async def close(self) -> None: ...


class PeerMessenger(Protocol):
    async def send(
        self, ip: str, port: int, body: Mapping[str, Any], timeout: float
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


class MediaOffloadAdapter(Protocol):
    """Platform WebRTC adapter; implementations own camera and PeerConnections."""

    async def connect(
        self,
        session: OffloadingSession,
        signaling: Mapping[str, Any],
        timeout_seconds: float,
    ) -> None: ...

    async def start_video_upload(
        self,
        session: OffloadingSession,
        *,
        camera_id: int,
        width: int,
        height: int,
        fps: int,
        bitrate_kbps: int,
    ) -> VideoUploadHandle: ...

    async def get_processed_video_stream(
        self,
        session: OffloadingSession,
        timeout_seconds: float,
    ) -> RemoteVideoStream: ...

    async def close(self) -> None: ...
