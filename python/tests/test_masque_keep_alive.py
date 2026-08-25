from __future__ import annotations

import asyncio
import logging

import pytest

from agent_sdk.masque import AioquicConnectIpTransport


class _FakeProtocol:
    def __init__(self) -> None:
        self.ping_count = 0
        self.ping_sent = asyncio.Event()

    def send_keep_alive(self) -> None:
        self.ping_count += 1
        self.ping_sent.set()


def test_keep_alive_interval_must_be_positive() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        AioquicConnectIpTransport(
            server_url="https://192.168.3.10:8443/.well-known/masque/ip",
            keep_alive_interval=0,
        )


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
