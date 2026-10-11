from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
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
from .tun import packet_too_big_response


# Reserve space for a short QUIC header, packet number, AEAD tag, DATAGRAM
# frame and HTTP Datagram / CONNECT-IP context IDs. Keep the inner MTU intact.
QUIC_ENCAPSULATION_RESERVE = 120
QUIC_DATAGRAM_OVERHEAD = 64
MAX_PENDING_IP_DATAGRAMS = 1024
MAX_PENDING_MEDIA_DATAGRAMS = 64
MEDIA_DATAGRAM_MAX_AGE = 0.75


def _is_video_rtp(packet: bytes) -> bool:
    """Identify RTP while keeping STUN, DTLS, RTCP and TCP in the control lane."""
    if not packet:
        return False
    version = packet[0] >> 4
    if version == 4 and len(packet) >= 20:
        if packet[9] != 17 or int.from_bytes(packet[6:8], "big") & 0x3FFF:
            return False
        offset = (packet[0] & 15) * 4
    elif version == 6 and len(packet) >= 40 and packet[6] == 17:
        offset = 40
    else:
        return False
    payload = packet[offset + 8:]
    return len(payload) >= 12 and payload[0] >> 6 == 2 and not 192 <= payload[1] <= 223


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
        ip_packet_mtu: int = 1280,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._logger = logger or logging.getLogger(__name__)
        self.http = H3Connection(self._quic, enable_webtransport=True)
        self.response: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self.settings: asyncio.Future[dict[int, int]] = (
            asyncio.get_running_loop().create_future()
        )
        self.response.add_done_callback(self._consume_future_exception)
        self.settings.add_done_callback(self._consume_future_exception)
        self.packets: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1024)
        self.connect_stream_id: int | None = None
        self._keep_alive_uid = 0
        self._ip_packet_mtu = ip_packet_mtu
        self._oversized_packets = 0
        self._queue_dropped_packets = 0
        self._pending_media_times: dict[int, float] = {}
        self._stale_media_dropped_packets = 0

    def _prune_media_queue(self) -> None:
        pending = self._quic._datagrams_pending
        now = time.monotonic()
        kept = [item for item in pending if id(item) not in self._pending_media_times or now - self._pending_media_times[id(item)] <= MEDIA_DATAGRAM_MAX_AGE]
        expired = len(pending) - len(kept)
        if expired:
            pending.clear()
            pending.extend(kept)
            old_count = self._stale_media_dropped_packets
            self._stale_media_dropped_packets += expired
            if old_count.bit_length() != self._stale_media_dropped_packets.bit_length():
                log_event(self._logger, logging.WARNING, "masque_stale_media_dropped", count=self._stale_media_dropped_packets)
        alive = {id(item) for item in pending}
        self._pending_media_times = {key: value for key, value in self._pending_media_times.items() if key in alive}

    def transmit(self) -> None:
        # aioquic calls transmit again as ACKs open the congestion window.
        # Expire stale media on that path as well as on new TUN packets.
        if hasattr(self, "_pending_media_times"):
            self._prune_media_queue()
        super().transmit()

    async def wait_connected(self) -> None:
        try:
            await super().wait_connected()
        except asyncio.CancelledError:
            # aioquic shields its internal handshake future. When our outer
            # timeout cancels this coroutine, the future later receives the
            # connection-close exception and Python 3.14 otherwise reports
            # "exception in shielded future" as an unhandled secondary error.
            waiter = self._connected_waiter
            if waiter is not None:
                waiter.add_done_callback(self._consume_future_exception)
            raise

    @staticmethod
    def _consume_future_exception(future: asyncio.Future[Any]) -> None:
        if future.cancelled():
            return
        try:
            future.exception()
        except Exception:
            return

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
            if not self.settings.done():
                self.settings.set_exception(
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
        self._publish_received_settings()

    def _publish_received_settings(self) -> None:
        received_settings = self.http.received_settings
        if received_settings is not None and not self.settings.done():
            self.settings.set_result(dict(received_settings))

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

    def send_ip_packet(self, packet: bytes) -> bytes | None:
        if self.connect_stream_id is None:
            raise RuntimeError("CONNECT-IP stream is not open")
        # aioquic leaves a DATAGRAM which cannot fit at the head of its queue.
        # Check capacity before enqueueing so one oversized IP packet cannot
        # block subsequent TCP segments, RTP packets or control traffic.
        maximum = min(
            self._ip_packet_mtu,
            self._quic.configuration.max_datagram_size - QUIC_DATAGRAM_OVERHEAD,
        )
        peer_limit = self._quic._remote_max_datagram_frame_size
        if peer_limit is not None:
            maximum = min(maximum, peer_limit - 20)
        if len(packet) > maximum:
            self._oversized_packets += 1
            count = self._oversized_packets
            if count == 1 or count & (count - 1) == 0:
                log_event(
                    self._logger, logging.WARNING, "masque_ip_packet_too_large",
                    packet_bytes=len(packet), max_ip_packet_bytes=maximum,
                    tun_mtu=self._ip_packet_mtu, count=count,
                )
            return packet_too_big_response(packet, max(0, maximum))
        self._prune_media_queue()
        media = _is_video_rtp(packet)
        pending = self._quic._datagrams_pending
        full = len(pending) >= MAX_PENDING_IP_DATAGRAMS
        if media and (full or len(self._pending_media_times) >= MAX_PENDING_MEDIA_DATAGRAMS):
            self._queue_dropped_packets += 1
            count = self._queue_dropped_packets
            if count == 1 or count & (count - 1) == 0:
                log_event(
                    self._logger, logging.WARNING, "masque_ip_queue_full",
                    count=count, max_pending=MAX_PENDING_MEDIA_DATAGRAMS,
                )
            return None
        if full:
            # Reserve capacity for control traffic by evicting media first.
            victim_index = next((i for i in range(len(pending) - 1, -1, -1) if id(pending[i]) in self._pending_media_times), None)
            if victim_index is None:
                self._queue_dropped_packets += 1
                return None
            victim = pending[victim_index]
            del pending[victim_index]
            self._pending_media_times.pop(id(victim), None)
        self.http.send_datagram(self.connect_stream_id, b"\x00" + packet)
        datagram = pending[-1]
        if media:
            self._pending_media_times[id(datagram)] = time.monotonic()
        else:
            # Preserve control ordering, but put it ahead of queued RTP.
            pending.pop()
            index = next((i for i, item in enumerate(pending) if id(item) in self._pending_media_times), len(pending))
            pending.insert(index, datagram)
        self.transmit()
        return None

    def send_keep_alive(self) -> None:
        self._keep_alive_uid += 1
        self._quic.send_ping(self._keep_alive_uid)
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
        keep_alive_interval: float = 15.0,
        logger: logging.Logger | None = None,
        identity_store: ClientTlsIdentityStore | None = None,
        ip_packet_mtu: int = 1280,
    ) -> None:
        if keep_alive_interval <= 0:
            raise ValueError("keep_alive_interval must be greater than zero")
        if not 576 <= ip_packet_mtu <= 65507 - QUIC_ENCAPSULATION_RESERVE:
            raise ValueError("IP packet MTU does not fit in an encapsulated UDP datagram")
        self._ip_packet_mtu = ip_packet_mtu
        self._on_packet: Callable[[bytes], Awaitable[None]] | None = None
        self._url = urlparse(server_url)
        self._server_name = server_name
        self._ca = ca_certificate_pem
        self._authorization = authorization
        self._local_address = local_address
        self._connect_timeout = connect_timeout
        self._keep_alive_interval = keep_alive_interval
        self._context: Any = None
        self._protocol: ConnectIpQuicProtocol | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._keep_alive_task: asyncio.Task[None] | None = None
        self._recovery_task: asyncio.Task[None] | None = None
        self._connected = False
        self._logger = logger or logging.getLogger(__name__)
        self._identity_store = identity_store or ClientTlsIdentityStore()
        self._on_packet_callback: Callable[[bytes], Awaitable[None]] | None = None
        self._reconnect_lock = asyncio.Lock()
        self._explicitly_closed = True

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(
        self, on_packet: Callable[[bytes], Awaitable[None]]
    ) -> None:
        self._on_packet_callback = on_packet
        self._explicitly_closed = False
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
            max_datagram_size=max(1200, self._ip_packet_mtu + QUIC_ENCAPSULATION_RESERVE),
            verify_mode=ssl.CERT_NONE,
        )
        configuration.load_cert_chain(
            str(identity.certificate_path), str(identity.private_key_path)
        )
        log_event(
            self._logger, logging.INFO, "masque_packet_capacity",
            tun_mtu=self._ip_packet_mtu,
            max_datagram_size=configuration.max_datagram_size,
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
        phase = "QUIC handshake"
        log_event(
            self._logger,
            logging.INFO,
            "masque_connect_start",
            server=f"{host}:{port}",
            local_address=self._local_address or "OS-selected",
            timeout_seconds=self._connect_timeout,
            phase=phase,
        )
        try:
            if self._local_address is None:
                self._context = connect(
                    host,
                    port,
                    configuration=configuration,
                    create_protocol=lambda *args, **kwargs: ConnectIpQuicProtocol(
                        *args, logger=self._logger, ip_packet_mtu=self._ip_packet_mtu, **kwargs
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
            protocol._ip_packet_mtu = self._ip_packet_mtu
            authority = host if port == 443 else f"{host}:{port}"
            protocol.open_connect_ip(
                authority=authority,
                path=self._url.path or "/",
                authorization=self._authorization,
            )
            phase = "CONNECT-IP response"
            status = await asyncio.wait_for(protocol.response, self._connect_timeout)
            if status < 200 or status >= 300:
                raise AgentSdkError(
                    ErrorCode.CONNECT_IP_NEGOTIATION_FAILED,
                    f"CONNECT-IP returned HTTP {status}",
                )
            phase = "HTTP/3 SETTINGS"
            settings = await asyncio.wait_for(
                protocol.settings,
                self._connect_timeout,
            )
            log_event(
                self._logger,
                logging.INFO,
                "masque_http3_settings_received",
                h3_datagram=settings.get(Setting.H3_DATAGRAM),
                settings={str(int(key)): value for key, value in settings.items()},
            )
            if settings.get(Setting.H3_DATAGRAM) != 1:
                raise AgentSdkError(
                    ErrorCode.CONNECT_IP_NEGOTIATION_FAILED,
                    "peer did not negotiate HTTP/3 Datagram",
                )
            self._connected = True
            self._on_packet = on_packet
            self._receive_task = asyncio.create_task(
                self._receive_loop(on_packet), name="connect-ip-receive"
            )
            self._keep_alive_task = asyncio.create_task(
                self._keep_alive_loop(), name="connect-ip-keep-alive"
            )
            log_event(
                self._logger,
                logging.INFO,
                "masque_keep_alive_started",
                interval_seconds=self._keep_alive_interval,
            )
        except AgentSdkError:
            await self._close_connection()
            raise
        except asyncio.TimeoutError as exc:
            await self._close_connection()
            source = self._local_address or "OS-selected"
            raise AgentSdkError(
                ErrorCode.MASQUE_CONNECT_FAILED,
                f"MASQUE {phase} timed out after {self._connect_timeout:g}s: "
                f"server={host}:{port}, local_address={source}; verify the "
                "MASQUE UDP port, firewall, server process, and source-IP route",
                retryable=True,
                details={
                    "phase": phase,
                    "server": f"{host}:{port}",
                    "local_address": source,
                    "timeout_seconds": self._connect_timeout,
                },
            ) from exc
        except Exception as exc:
            await self._close_connection()
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
                self._connected = False
                # A peer can close an idle CONNECT-IP session while the agent
                # has no uplink traffic. Start recovery immediately so the
                # next A2A or media packet does not hit a stale tunnel.
                self._schedule_recovery()
                return
            await on_packet(packet)

    async def _keep_alive_loop(self) -> None:
        while self._connected:
            await asyncio.sleep(self._keep_alive_interval)
            protocol = self._protocol
            if not self._connected or protocol is None:
                return
            try:
                protocol.send_keep_alive()
            except Exception as exc:
                self._connected = False
                log_event(
                    self._logger,
                    logging.WARNING,
                    "masque_keep_alive_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                self._schedule_recovery()
                return

    def _schedule_recovery(self) -> None:
        if not self._explicitly_closed and (
            self._recovery_task is None or self._recovery_task.done()
        ):
            self._recovery_task = asyncio.create_task(
                self._reconnect(), name="connect-ip-reconnect"
            )

    async def send_packet(self, packet: bytes) -> None:
        if not self._connected and not self._explicitly_closed:
            await self._reconnect()
        if not self._connected or self._protocol is None:
            raise AgentSdkError(
                ErrorCode.MASQUE_CONNECT_FAILED,
                "CONNECT-IP transport is not connected",
                retryable=True,
            )
        feedback = self._protocol.send_ip_packet(packet)
        if feedback is not None and self._on_packet is not None:
            await self._on_packet(feedback)

    async def _reconnect(self) -> None:
        """Re-establish CONNECT-IP after an idle timeout or QUIC loss.

        The TUN and its packet callback remain owned by AgentSdk; only the
        QUIC/HTTP3 transport is replaced. A lock prevents concurrent uplink
        packets from creating multiple tunnels.
        """
        async with self._reconnect_lock:
            if self._connected or self._explicitly_closed:
                return
            callback = self._on_packet_callback
            if callback is None:
                return
            self._logger.warning("masque_reconnect_start")
            # Close the old aioquic context and tasks without changing the
            # explicit-close flag or losing the packet callback.
            await self._close_connection()
            delay = 1.0
            for attempt in range(1, 6):
                if self._explicitly_closed:
                    return
                try:
                    await self.start(callback)
                except Exception as exc:
                    self._logger.warning(
                        "masque_reconnect_failed attempt=%s error_type=%s error=%s",
                        attempt, type(exc).__name__, exc,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 15.0)
                else:
                    self._logger.info("masque_reconnect_succeeded attempt=%s", attempt)
                    return
            self._logger.error("masque_reconnect_exhausted attempts=5")

    async def _close_connection(self) -> None:
        self._connected = False
        current = asyncio.current_task()
        for task_name in ("_keep_alive_task", "_receive_task"):
            task = getattr(self, task_name)
            if task is not None and task is not current:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            setattr(self, task_name, None)
        if self._context is not None:
            await self._context.__aexit__(None, None, None)
            self._context = None
        self._protocol = None

    async def close(self) -> None:
        self._explicitly_closed = True
        recovery = self._recovery_task
        self._recovery_task = None
        if recovery is not None and recovery is not asyncio.current_task():
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
        await self._close_connection()
        self._on_packet = None
        self._on_packet_callback = None
