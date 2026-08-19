from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from typing import Any, Protocol

from aioquic.asyncio import serve
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import DatagramReceived, HeadersReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import ConnectionTerminated, QuicEvent

from .tun import validate_ip_packet


class UePacketAdapter(Protocol):
    async def open(self) -> None: ...

    async def send_to_ue(self, packet: bytes) -> None: ...

    async def receive_from_ue(self) -> bytes: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProxySessionPolicy:
    agent_ip: str
    allowed_peer_cidrs: tuple[str, ...]
    mtu: int
    adapter_factory: Callable[[], UePacketAdapter]

    def __post_init__(self) -> None:
        ip_address(self.agent_ip)
        if not 576 <= self.mtu <= 65535:
            raise ValueError("MTU must be in 576..65535")
        for cidr in self.allowed_peer_cidrs:
            ip_network(cidr, strict=False)

    def peer_allowed(self, value: str) -> bool:
        address = ip_address(value)
        return any(
            address in ip_network(cidr, strict=False)
            for cidr in self.allowed_peer_cidrs
        )


class TokenSessionResolver:
    """Resolves an Authorization bearer token to an immutable UE policy."""

    def __init__(self, policies_by_token: Mapping[str, ProxySessionPolicy]) -> None:
        self._policies = dict(policies_by_token)

    def resolve(self, headers: Mapping[bytes, bytes]) -> ProxySessionPolicy | None:
        raw = headers.get(b"authorization", b"").decode("utf-8", "strict")
        scheme, separator, token = raw.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token:
            return None
        return self._policies.get(token)


class ConnectIpProxySession:
    def __init__(
        self,
        policy: ProxySessionPolicy,
        adapter: UePacketAdapter,
        send_downlink: Callable[[bytes], None],
    ) -> None:
        self.policy = policy
        self.adapter = adapter
        self._send_downlink = send_downlink
        self._downlink_task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        await self.adapter.open()
        self._downlink_task = asyncio.create_task(
            self._downlink_loop(), name=f"connect-ip-{self.policy.agent_ip}"
        )

    async def receive_uplink(self, packet: bytes) -> None:
        source, destination = validate_ip_packet(packet, self.policy.mtu)
        if source != str(ip_address(self.policy.agent_ip)):
            raise ValueError("inner source does not match the authenticated Agent IP")
        if not self.policy.peer_allowed(destination):
            raise ValueError("inner destination is outside the allowed peer ranges")
        await self.adapter.send_to_ue(packet)

    async def _downlink_loop(self) -> None:
        while not self._closed:
            try:
                packet = await self.adapter.receive_from_ue()
                source, destination = validate_ip_packet(packet, self.policy.mtu)
            except ValueError:
                continue
            if destination != str(ip_address(self.policy.agent_ip)):
                continue
            if not self.policy.peer_allowed(source):
                continue
            self._send_downlink(packet)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._downlink_task is not None:
            self._downlink_task.cancel()
            await asyncio.gather(self._downlink_task, return_exceptions=True)
            self._downlink_task = None
        await self.adapter.close()


class ConnectIpProxyProtocol(QuicConnectionProtocol):
    def __init__(
        self,
        *args: Any,
        session_resolver: TokenSessionResolver,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.http = H3Connection(self._quic, enable_webtransport=True)
        self._resolver = session_resolver
        self._sessions: dict[int, ConnectIpProxySession] = {}
        self._pending_streams: set[int] = set()

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, ConnectionTerminated):
            for session in tuple(self._sessions.values()):
                asyncio.create_task(session.close())
            self._sessions.clear()
            return
        for http_event in self.http.handle_event(event):
            if isinstance(http_event, HeadersReceived):
                self._handle_headers(http_event)
            elif isinstance(http_event, DatagramReceived):
                session = self._sessions.get(http_event.stream_id)
                if session is None or not http_event.data or http_event.data[0] != 0:
                    continue
                asyncio.create_task(
                    self._deliver_uplink(session, http_event.data[1:])
                )

    def _handle_headers(self, event: HeadersReceived) -> None:
        headers = dict(event.headers)
        valid_connect = (
            headers.get(b":method") == b"CONNECT"
            and headers.get(b":protocol") == b"connect-ip"
            and headers.get(b"capsule-protocol", b"").lower() == b"?1"
        )
        policy = self._resolver.resolve(headers) if valid_connect else None
        if policy is None or event.stream_id in self._pending_streams:
            self.http.send_headers(
                event.stream_id,
                [(b":status", b"403" if valid_connect else b"400")],
                end_stream=True,
            )
            self.transmit()
            return
        self._pending_streams.add(event.stream_id)
        asyncio.create_task(self._open_session(event.stream_id, policy))

    async def _open_session(
        self, stream_id: int, policy: ProxySessionPolicy
    ) -> None:
        session = ConnectIpProxySession(
            policy,
            policy.adapter_factory(),
            lambda packet: self._send_downlink(stream_id, packet),
        )
        try:
            await session.start()
        except Exception:
            self.http.send_headers(stream_id, [(b":status", b"502")], end_stream=True)
            self.transmit()
        else:
            self._sessions[stream_id] = session
            self.http.send_headers(
                stream_id,
                [(b":status", b"200"), (b"capsule-protocol", b"?1")],
                end_stream=False,
            )
            self.transmit()
        finally:
            self._pending_streams.discard(stream_id)

    async def _deliver_uplink(
        self, session: ConnectIpProxySession, packet: bytes
    ) -> None:
        try:
            await session.receive_uplink(packet)
        except (ValueError, OSError):
            return

    def _send_downlink(self, stream_id: int, packet: bytes) -> None:
        if stream_id not in self._sessions:
            return
        self.http.send_datagram(stream_id, b"\x00" + packet)
        self.transmit()


class LinuxUeInterfaceAdapter:
    """L3 adapter for an existing UERANSIM TUN using Linux raw packet sockets."""

    ETH_P_ALL = 0x0003
    IPV6_HDRINCL = 36

    def __init__(self, interface_name: str) -> None:
        self.interface_name = interface_name
        self._capture: socket.socket | None = None
        self._raw_v4: socket.socket | None = None
        self._raw_v6: socket.socket | None = None

    async def open(self) -> None:
        loop = asyncio.get_running_loop()

        def create_sockets() -> tuple[socket.socket, socket.socket, socket.socket]:
            if not socket.if_nametoindex(self.interface_name):
                raise OSError(f"interface {self.interface_name} does not exist")
            opened: list[socket.socket] = []
            try:
                capture = socket.socket(
                    socket.AF_PACKET,
                    socket.SOCK_DGRAM | socket.SOCK_NONBLOCK,
                    socket.htons(self.ETH_P_ALL),
                )
                opened.append(capture)
                capture.bind((self.interface_name, socket.htons(self.ETH_P_ALL)))
                raw_v4 = socket.socket(
                    socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW
                )
                opened.append(raw_v4)
                raw_v4.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
                raw_v4.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_BINDTODEVICE,
                    self.interface_name.encode() + b"\x00",
                )
                raw_v6 = socket.socket(
                    socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_RAW
                )
                opened.append(raw_v6)
                raw_v6.setsockopt(socket.IPPROTO_IPV6, self.IPV6_HDRINCL, 1)
                raw_v6.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_BINDTODEVICE,
                    self.interface_name.encode() + b"\x00",
                )
                return capture, raw_v4, raw_v6
            except Exception:
                for opened_socket in opened:
                    opened_socket.close()
                raise

        self._capture, self._raw_v4, self._raw_v6 = await loop.run_in_executor(
            None, create_sockets
        )

    async def send_to_ue(self, packet: bytes) -> None:
        _, destination = validate_ip_packet(packet, 65535)
        parsed = ip_address(destination)
        sock = self._raw_v4 if parsed.version == 4 else self._raw_v6
        if sock is None:
            raise OSError("UE adapter is not open")
        loop = asyncio.get_running_loop()
        address = (destination, 0) if parsed.version == 4 else (destination, 0, 0, 0)
        await loop.run_in_executor(None, sock.sendto, packet, address)

    async def receive_from_ue(self) -> bytes:
        if self._capture is None:
            raise OSError("UE adapter is not open")
        loop = asyncio.get_running_loop()
        while True:
            packet, address = await loop.sock_recvfrom(self._capture, 65535)
            if len(address) > 2 and address[2] == socket.PACKET_OUTGOING:
                continue
            return packet

    async def close(self) -> None:
        for sock in (self._capture, self._raw_v4, self._raw_v6):
            if sock is not None:
                sock.close()
        self._capture = None
        self._raw_v4 = None
        self._raw_v6 = None


class MasqueProxyServer:
    def __init__(
        self,
        host: str,
        port: int,
        certificate_path: str,
        private_key_path: str,
        resolver: TokenSessionResolver,
    ) -> None:
        self._host = host
        self._port = port
        self._certificate_path = certificate_path
        self._private_key_path = private_key_path
        self._resolver = resolver
        self._server: Any = None

    @property
    def bound_port(self) -> int:
        if self._server is None:
            return 0
        address = self._server._transport.get_extra_info("sockname")
        return int(address[1])

    async def start(self) -> None:
        configuration = QuicConfiguration(
            is_client=False,
            alpn_protocols=H3_ALPN,
            max_datagram_frame_size=65536,
            verify_mode=ssl.CERT_NONE,
        )
        configuration.load_cert_chain(
            self._certificate_path, self._private_key_path
        )
        self._server = await serve(
            self._host,
            self._port,
            configuration=configuration,
            create_protocol=lambda *args, **kwargs: ConnectIpProxyProtocol(
                *args, session_resolver=self._resolver, **kwargs
            ),
            retry=True,
        )

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
