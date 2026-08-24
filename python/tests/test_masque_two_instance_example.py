from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import socket
from pathlib import Path

import httpx
import pytest


EXAMPLE_PATH = Path(__file__).parents[1] / "examples" / "masque_two_instance_test.py"


def _load_example():
    spec = importlib.util.spec_from_file_location(
        "masque_two_instance_test", EXAMPLE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _arguments(module, tmp_path: Path, role: str, *extra: str):
    return module.parser().parse_args(
        [
            "--role",
            role,
            "--local-vlan-ip",
            "192.168.1.10" if role == "A" else "192.168.2.10",
            "--local-agent-ip",
            "8.8.8.7" if role == "A" else "8.8.8.8",
            "--peer-agent-ip",
            "8.8.8.8" if role == "A" else "8.8.8.7",
            "--masque-url",
            f"https://192.168.3.10:{4433 if role == 'A' else 4434}/masque",
            "--state-dir",
            str(tmp_path / f"state-{role.lower()}"),
            "--log-file",
            str(tmp_path / f"direct-{role.lower()}.log"),
            *extra,
        ]
    )


def _logger() -> logging.Logger:
    logger = logging.getLogger(f"masque-direct-test-{id(object())}")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def _ipv4_packet(source: str, destination: str) -> bytes:
    packet = bytearray(20)
    packet[0] = 0x45
    packet[2:4] = (20).to_bytes(2, "big")
    packet[8] = 64
    packet[9] = 6
    packet[12:16] = socket.inet_aton(source)
    packet[16:20] = socket.inet_aton(destination)
    return bytes(packet)


class FakeTun:
    def __init__(self, reads: list[bytes] | None = None) -> None:
        self._reads = list(reads or [])
        self.writes: list[bytes] = []

    async def read(self) -> bytes:
        return self._reads.pop(0) if self._reads else b""

    async def write(self, packet: bytes) -> None:
        self.writes.append(packet)


class FakeMasque:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_packet(self, packet: bytes) -> None:
        self.sent.append(packet)


def test_parser_requires_ips_but_has_no_agent_id_or_group_arguments(tmp_path):
    module = _load_example()
    a = module._apply_role_defaults(_arguments(module, tmp_path, "A"))
    b = module._apply_role_defaults(_arguments(module, tmp_path, "B"))

    assert (a.local_agent_ip, a.peer_agent_ip) == ("8.8.8.7", "8.8.8.8")
    assert (b.local_agent_ip, b.peer_agent_ip) == ("8.8.8.8", "8.8.8.7")
    assert a.message_port == b.message_port == 4001
    assert a.tun_name == "agent_tun_a"
    assert b.tun_name == "agent_tun_b"
    assert not hasattr(a, "target_agent_id")
    assert not hasattr(a, "runtime_ip")


def test_same_network_namespace_is_rejected(tmp_path):
    module = _load_example()
    current_netns = module._netns_id()
    if current_netns is None:
        pytest.skip("Linux network namespace ID is unavailable")
    args = module._apply_role_defaults(
        _arguments(
            module,
            tmp_path,
            "A",
            "--peer-netns-id",
            str(current_netns),
        )
    )
    with pytest.raises(ValueError, match="same Linux network namespace"):
        module._validate_args(args)


async def test_uplink_only_forwards_packets_from_local_to_peer():
    module = _load_example()
    valid = _ipv4_packet("8.8.8.7", "8.8.8.8")
    invalid = _ipv4_packet("8.8.8.7", "8.8.8.9")
    tun = FakeTun([invalid, valid])
    masque = FakeMasque()

    await module._pump_uplink(
        tun=tun,
        masque=masque,
        local_agent_ip="8.8.8.7",
        peer_agent_ip="8.8.8.8",
        mtu=1280,
        logger=_logger(),
        role="A",
    )

    assert masque.sent == [valid]


async def test_downlink_only_writes_packets_from_peer_to_local():
    module = _load_example()
    valid = _ipv4_packet("8.8.8.7", "8.8.8.8")
    invalid = _ipv4_packet("8.8.8.9", "8.8.8.8")
    tun = FakeTun()

    for packet in (invalid, valid):
        await module._write_downlink(
            packet,
            tun=tun,
            local_agent_ip="8.8.8.8",
            peer_agent_ip="8.8.8.7",
            mtu=1280,
            logger=_logger(),
            role="B",
        )

    assert tun.writes == [valid]


async def test_a_posts_json_to_peer_ip_port_4001_message(tmp_path):
    module = _load_example()
    args = module._apply_role_defaults(
        _arguments(
            module,
            tmp_path,
            "A",
            "--message",
            '{"content":"hello B"}',
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://8.8.8.8:4001/message"
        assert json.loads(request.content) == {"content": "hello B"}
        return httpx.Response(200, json={"status": "OK"})

    await module._post_message(
        args,
        module._configure_logger("A", str(tmp_path / "sender.log")),
        transport=httpx.MockTransport(handler),
    )

    log_text = (tmp_path / "sender.log").read_text(encoding="utf-8")
    assert '"event": "MESSAGE_SENDING"' in log_text
    assert '"event": "MESSAGE_DELIVERED"' in log_text


async def test_b_message_endpoint_prints_and_logs_received_json(tmp_path):
    module = _load_example()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    received_event = asyncio.Event()
    logger = module._configure_logger("B", str(tmp_path / "receiver.log"))
    server = module.MessageServer(
        role="B",
        local_agent_ip="127.0.0.1",
        peer_agent_ip="127.0.0.1",
        port=port,
        logger=logger,
        received_event=received_event,
    )
    await server.start()
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/message",
                json={"content": "hello B"},
            )
        assert response.json() == {"status": "OK"}
        assert received_event.is_set()
        assert server.last_message == {"content": "hello B"}
    finally:
        await server.close()

    log_text = (tmp_path / "receiver.log").read_text(encoding="utf-8")
    assert '"event": "MESSAGE_SERVER_LISTENING"' in log_text
    assert '"event": "MESSAGE_RECEIVED"' in log_text
    assert '"path": "/message"' in log_text
    assert '"payload": {"content": "hello B"}' in log_text
