from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

from aioquic.asyncio.client import connect
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.h3.connection import H3_ALPN, H3Connection, Setting
from aioquic.h3.events import DatagramReceived, HeadersReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import ConnectionTerminated, QuicEvent

from .errors import AgentSdkError, ErrorCode
from .identity import ClientTlsIdentityStore
from .logging_utils import log_event


@asynccontextmanager
async def _connect_from_address(
    host: str,
    port: int,
    *,
    local_address: str,
    configuration: QuicConfiguration,
    logger: logging.Logger,
):
    """aioquic client context with an exact physical source address."""
    loop = asyncio.get_running_loop()
    parsed = ip_address(local_address)
    family = socket.AF_INET if parsed.version == 4 else socket.AF_INET6
    infos = await loop.getaddrinfo(host, port, family=family, type=socket.SOCK_DGRAM)
    if not infos:
        raise OSError(f"cannot resolve {host} for the local address family")
    remote_address = infos[0][4]
    quic = QuicConnection(configuration=configuration)
    sock = socket.socket(family, socket.SOCK_DGRAM)
    completed = False
    try:
        sock.bind((local_address, 0) if family == socket.AF_INET else (local_address, 0, 0, 0))
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: ConnectIpQuicProtocol(quic, logger=logger), sock=sock
        )
        completed = True
        try:
            protocol.connect(remote_address)
            await protocol.wait_connected()
            yield protocol
        finally:
            protocol.close()
            await protocol.wait_closed()
            transport.close()
    finally:
        if not completed:
            sock.close()


class ConnectIpQuicProtocol(QuicConnectionProtocol):
    def __init__(
        self,
        *args: Any,
        logger: logging.Logger | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._logger = logger or logging.getLogger(__name__)
        self.http = H3Connection(self._quic, enable_webtransport=True)
        self.response: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self.packets: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1024)
        self.connect_stream_id: int | None = None

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, ConnectionTerminated):
            log_event(
                self._logger,
                logging.WARNING,
                "connect_ip_connection_closed",
                error_code=event.error_code,
                reason_phrase=event.reason_phrase,
            )
            if not self.response.done():
                self.response.set_exception(
                    ConnectionError(f"QUIC closed: {event.error_code}")
                )
            try:
                self.packets.put_nowait(None)
            except asyncio.QueueFull:
                pass
            return
        for http_event in self.http.handle_event(event):
            if isinstance(http_event, HeadersReceived):
                decoded_headers = {
                    name.decode("utf-8", "replace"): value.decode("utf-8", "replace")
                    for name, value in http_event.headers
                }
                status = next(
                    (
                        int(value)
                        for name, value in http_event.headers
                        if name == b":status"
                    ),
                    0,
                )
                log_event(
                    self._logger,
                    logging.INFO if 200 <= status < 300 else logging.ERROR,
                    "http_response",
                    direction="inbound",
                    peer="MASQUE Proxy",
                    protocol="HTTP/3 CONNECT-IP",
                    stream_id=http_event.stream_id,
                    status_code=status,
                    headers=decoded_headers,
                    body=None,
                )
                if not self.response.done():
                    self.response.set_result(status)
            elif isinstance(http_event, DatagramReceived):
                if http_event.stream_id != self.connect_stream_id:
                    continue
                data = http_event.data
                if not data or data[0] != 0:
                    continue
                try:
                    self.packets.put_nowait(data[1:])
                except asyncio.QueueFull:
                    pass

    def open_connect_ip(
        self, *, authority: str, path: str, authorization: str | None
    ) -> None:
        stream_id = self._quic.get_next_available_stream_id()
        self.connect_stream_id = stream_id
        headers = [
            (b":method", b"CONNECT"),
            (b":scheme", b"https"),
            (b":authority", authority.encode()),
            (b":path", path.encode()),
            (b":protocol", b"connect-ip"),
            (b"capsule-protocol", b"?1"),
        ]
        if authorization:
            headers.append((b"authorization", authorization.encode()))
        log_event(
            self._logger,
            logging.INFO,
            "http_request",
            direction="outbound",
            peer="MASQUE Proxy",
            protocol="HTTP/3 CONNECT-IP",
            stream_id=stream_id,
            method="CONNECT",
            url=f"https://{authority}{path}",
            headers={
                name.decode("utf-8", "replace"): value.decode("utf-8", "replace")
                for name, value in headers
            },
            body=None,
        )
        self.http.send_headers(stream_id, headers, end_stream=False)
        self.transmit()

    def send_ip_packet(self, packet: bytes) -> None:
        if self.connect_stream_id is None:
            raise RuntimeError("CONNECT-IP stream is not open")
        self.http.send_datagram(self.connect_stream_id, b"\x00" + packet)
        self.transmit()


class AioquicConnectIpTransport:
    def __init__(
        self,
        *,
        server_url: str,
        server_name: str | None = None,
        ca_certificate_pem: bytes | None = None,
        authorization: str | None = None,
        local_address: str | None = None,
        connect_timeout: float = 10.0,
        logger: logging.Logger | None = None,
        identity_store: ClientTlsIdentityStore | None = None,
    ) -> None:
        self._url = urlparse(server_url)
        self._server_name = server_name
        self._ca = ca_certificate_pem
        self._authorization = authorization
        self._local_address = local_address
        self._connect_timeout = connect_timeout
        self._context: Any = None
        self._protocol: ConnectIpQuicProtocol | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._connected = False
        self._logger = logger or logging.getLogger(__name__)
        self._identity_store = identity_store or ClientTlsIdentityStore()

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(
        self, on_packet: Callable[[bytes], Awaitable[None]]
    ) -> None:
        host = self._url.hostname
        if host is None:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT, "MASQUE URL has no hostname"
            )
        port = self._url.port or 443
        identity = self._identity_store.ensure()
        configuration = QuicConfiguration(
            is_client=True,
            alpn_protocols=H3_ALPN,
            server_name=self._server_name or host,
            max_datagram_frame_size=65536,
            verify_mode=ssl.CERT_NONE,
        )
        configuration.load_cert_chain(
            str(identity.certificate_path), str(identity.private_key_path)
        )
        log_event(
            self._logger,
            logging.WARNING,
            "masque_server_certificate_verification_disabled",
            security_profile="internal-test-only",
            supplied_ca_ignored=self._ca is not None,
        )
        log_event(
            self._logger,
            logging.INFO,
            "masque_tls_identity_ready",
            client_public_key_sha256=identity.public_key_sha256,
            server_name=self._server_name or host,
            server_certificate_verification="disabled",
        )
        try:
            if self._local_address is None:
                self._context = connect(
                    host,
                    port,
                    configuration=configuration,
                    create_protocol=lambda *args, **kwargs: ConnectIpQuicProtocol(
                        *args, logger=self._logger, **kwargs
                    ),
                )
            else:
                self._context = _connect_from_address(
                    host,
                    port,
                    local_address=self._local_address,
                    configuration=configuration,
                    logger=self._logger,
                )
            protocol = await asyncio.wait_for(
                self._context.__aenter__(), self._connect_timeout
            )
            assert isinstance(protocol, ConnectIpQuicProtocol)
            self._protocol = protocol
            authority = host if port == 443 else f"{host}:{port}"
            protocol.open_connect_ip(
                authority=authority,
                path=self._url.path or "/",
                authorization=self._authorization,
            )
            status = await asyncio.wait_for(protocol.response, self._connect_timeout)
            if status < 200 or status >= 300:
                raise AgentSdkError(
                    ErrorCode.CONNECT_IP_NEGOTIATION_FAILED,
                    f"CONNECT-IP returned HTTP {status}",
                )
            settings = protocol.http.received_settings or {}
            if settings.get(Setting.H3_DATAGRAM) != 1:
                raise AgentSdkError(
                    ErrorCode.CONNECT_IP_NEGOTIATION_FAILED,
                    "peer did not negotiate HTTP/3 Datagram",
                )
            self._connected = True
            self._receive_task = asyncio.create_task(
                self._receive_loop(on_packet), name="connect-ip-receive"
            )
        except AgentSdkError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise AgentSdkError(
                ErrorCode.MASQUE_CONNECT_FAILED,
                f"failed to connect MASQUE proxy: {exc}",
                retryable=True,
            ) from exc

    async def _receive_loop(
        self, on_packet: Callable[[bytes], Awaitable[None]]
    ) -> None:
        assert self._protocol is not None
        while True:
            packet = await self._protocol.packets.get()
            if packet is None:
                return
            await on_packet(packet)

    async def send_packet(self, packet: bytes) -> None:
        if not self._connected or self._protocol is None:
            raise AgentSdkError(
                ErrorCode.MASQUE_CONNECT_FAILED,
                "CONNECT-IP transport is not connected",
                retryable=True,
            )
        self._protocol.send_ip_packet(packet)

    async def close(self) -> None:
        self._connected = False
        if self._receive_task is not None:
            self._receive_task.cancel()
            await asyncio.gather(self._receive_task, return_exceptions=True)
            self._receive_task = None
        if self._context is not None:
            await self._context.__aexit__(None, None, None)
            self._context = None
        self._protocol = None
