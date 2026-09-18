from __future__ import annotations

import asyncio
import logging

import pytest
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.h3.connection import Setting

import agent_sdk.masque as masque_module
from agent_sdk.errors import AgentSdkError, ErrorCode
from agent_sdk.identity import ClientTlsIdentityStore
from agent_sdk.masque import AioquicConnectIpTransport, ConnectIpQuicProtocol


class _FakeProtocol:
    def __init__(self) -> None:
        self.ping_count = 0
        self.ping_sent = asyncio.Event()

    def send_keep_alive(self) -> None:
        self.ping_count += 1
        self.ping_sent.set()


async def test_default_tun_packet_fits_quic_datagram(monkeypatch, tmp_path) -> None:
    from aioquic.quic.crypto import CryptoPair
    from aioquic.quic.packet import QuicPacketType
    from aioquic.quic.packet_builder import QuicPacketBuilder
    from aioquic.quic.packet import QuicFrameType

    class CapturedConfiguration(BaseException):
        pass

    def capture(*args, **kwargs):
        config = kwargs["configuration"]
        connection = QuicConnection(configuration=config)
        crypto = CryptoPair()
        crypto.setup_initial(cid=b"12345678", is_client=True, version=1)
        builder = QuicPacketBuilder(
            host_cid=b"12345678", peer_cid=b"87654321", version=1,
            is_client=True, max_datagram_size=config.max_datagram_size,
            packet_number=0, peer_token=b"", spin_bit=False,
        )
        builder.start_packet(QuicPacketType.ONE_RTT, crypto)
        # Quarter stream ID and CONNECT-IP context ID precede the IP packet.
        payload = b"\x00\x00" + bytes(1280)
        assert connection._write_datagram_frame(
            builder, payload, QuicFrameType.DATAGRAM_WITH_LENGTH
        )
        datagrams, _ = builder.flush()
        assert len(datagrams) == 1
        assert len(datagrams[0]) <= config.max_datagram_size
        raise CapturedConfiguration

    monkeypatch.setattr(masque_module, "connect", capture)
    transport = AioquicConnectIpTransport(
        server_url="https://127.0.0.1:8443/.well-known/masque/ip",
        identity_store=ClientTlsIdentityStore(tmp_path / "tls"),
    )
    async def on_packet(packet):
        pass

    with pytest.raises(CapturedConfiguration):
        await transport.start(on_packet)


def test_keep_alive_interval_must_be_positive() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        AioquicConnectIpTransport(
            server_url="https://192.168.3.10:8443/.well-known/masque/ip",
            keep_alive_interval=0,
        )


async def test_connect_timeout_reports_phase_endpoint_and_source(
    monkeypatch, tmp_path
) -> None:
    class _SlowContext:
        async def __aenter__(self):
            await asyncio.sleep(60)

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    monkeypatch.setattr(
        masque_module,
        "_connect_from_address",
        lambda *args, **kwargs: _SlowContext(),
    )
    transport = AioquicConnectIpTransport(
        server_url="https://192.168.3.10:8444/.well-known/masque/ip",
        local_address="192.168.2.10",
        connect_timeout=20.0,
        identity_store=ClientTlsIdentityStore(tmp_path / "tls"),
    )
    # Keep the public constructor contract at >=20s while shortening this unit
    # test's injected slow handshake.
    transport._connect_timeout = 0.01

    async def on_packet(packet: bytes) -> None:
        raise AssertionError(f"unexpected packet: {packet!r}")

    with pytest.raises(AgentSdkError, match="QUIC handshake timed out") as caught:
        await transport.start(on_packet)

    assert caught.value.code is ErrorCode.MASQUE_CONNECT_FAILED
    assert caught.value.details == {
        "phase": "QUIC handshake",
        "server": "192.168.3.10:8444",
        "local_address": "192.168.2.10",
        "timeout_seconds": 0.01,
    }


async def test_cancelled_handshake_consumes_late_aioquic_error() -> None:
    protocol = ConnectIpQuicProtocol(
        QuicConnection(configuration=QuicConfiguration(is_client=True))
    )
    task = asyncio.create_task(protocol.wait_connected())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    waiter = protocol._connected_waiter
    assert waiter is not None
    waiter.set_exception(ConnectionError("late QUIC close"))
    await asyncio.sleep(0)

    assert waiter._log_traceback is False


async def test_http3_settings_future_handles_settings_after_connect_response() -> None:
    protocol = ConnectIpQuicProtocol(
        QuicConnection(configuration=QuicConfiguration(is_client=True))
    )
    assert protocol.settings.done() is False

    protocol.http._received_settings = {Setting.H3_DATAGRAM: 1}
    protocol._publish_received_settings()

    assert await protocol.settings == {Setting.H3_DATAGRAM: 1}


async def test_keep_alive_sends_quic_ping_while_connected() -> None:
    transport = AioquicConnectIpTransport(
        server_url="https://192.168.3.10:8443/.well-known/masque/ip",
        keep_alive_interval=0.01,
        logger=logging.getLogger("test-masque-keep-alive"),
    )
    protocol = _FakeProtocol()
    transport._protocol = protocol  # type: ignore[assignment]
    transport._connected = True

    task = asyncio.create_task(transport._keep_alive_loop())
    try:
        await asyncio.wait_for(protocol.ping_sent.wait(), timeout=0.5)
        assert protocol.ping_count >= 1
    finally:
        transport._connected = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_receive_loop_marks_transport_disconnected_on_quic_close() -> None:
    transport = AioquicConnectIpTransport(
        server_url="https://192.168.3.10:8443/.well-known/masque/ip"
    )

    class _ClosedProtocol:
        def __init__(self) -> None:
            self.packets: asyncio.Queue[bytes | None] = asyncio.Queue()

    protocol = _ClosedProtocol()
    protocol.packets.put_nowait(None)
    transport._protocol = protocol  # type: ignore[assignment]
    transport._connected = True

    async def on_packet(packet: bytes) -> None:
        raise AssertionError(f"unexpected packet: {packet!r}")

    await transport._receive_loop(on_packet)

    assert transport.connected is False


async def test_keep_alive_failure_marks_transport_disconnected() -> None:
    transport = AioquicConnectIpTransport(
        server_url="https://192.168.3.10:8443/.well-known/masque/ip",
        keep_alive_interval=0.01,
        logger=logging.getLogger("test-masque-keep-alive-failure"),
    )

    class _FailingProtocol:
        def send_keep_alive(self) -> None:
            raise ConnectionError("connection closed")

    transport._protocol = _FailingProtocol()  # type: ignore[assignment]
    transport._connected = True

    await asyncio.wait_for(transport._keep_alive_loop(), timeout=0.5)

    assert transport.connected is False
