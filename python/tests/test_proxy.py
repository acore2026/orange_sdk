from __future__ import annotations

import asyncio
import socket

import pytest

from agent_sdk.proxy import (
    ConnectIpProxySession,
    ProxySessionPolicy,
    TokenSessionResolver,
)


def ipv4_packet(source: str, destination: str, payload: bytes = b"data") -> bytes:
    total_length = 20 + len(payload)
    return (
        b"\x45\x00"
        + total_length.to_bytes(2, "big")
        + b"\x00" * 4
        + b"\x40\x11\x00\x00"
        + socket.inet_aton(source)
        + socket.inet_aton(destination)
        + payload
    )


class MemoryUeAdapter:
    def __init__(self):
        self.uplink = []
        self.downlink = asyncio.Queue()
        self.opened = False

    async def open(self):
        self.opened = True

    async def send_to_ue(self, packet):
        self.uplink.append(packet)

    async def receive_from_ue(self):
        return await self.downlink.get()

    async def close(self):
        self.opened = False


async def test_proxy_session_enforces_agent_ip_and_forwards_both_directions():
    adapter = MemoryUeAdapter()
    received = []
    policy = ProxySessionPolicy(
        agent_ip="8.8.8.7",
        allowed_peer_cidrs=("8.8.8.8/32",),
        mtu=1280,
        adapter_factory=lambda: adapter,
    )
    session = ConnectIpProxySession(policy, adapter, received.append)
    await session.start()
    uplink = ipv4_packet("8.8.8.7", "8.8.8.8")
    await session.receive_uplink(uplink)
    await adapter.downlink.put(ipv4_packet("8.8.8.8", "8.8.8.7"))
    await asyncio.sleep(0)

    assert adapter.uplink == [uplink]
    assert len(received) == 1

    with pytest.raises(ValueError):
        await session.receive_uplink(ipv4_packet("8.8.8.9", "8.8.8.8"))
    with pytest.raises(ValueError):
        await session.receive_uplink(ipv4_packet("8.8.8.7", "8.8.8.99"))
    await session.close()


def test_token_resolver_requires_exact_bearer_token():
    policy = ProxySessionPolicy("8.8.8.7", ("8.8.8.8/32",), 1280, MemoryUeAdapter)
    resolver = TokenSessionResolver({"secret-a": policy})

    assert resolver.resolve({b"authorization": b"Bearer secret-a"}) is policy
    assert resolver.resolve({b"authorization": b"Bearer wrong"}) is None
    assert resolver.resolve({}) is None
