from __future__ import annotations

import importlib.util
import json
import logging
import socket
from pathlib import Path

import httpx


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
            "--runtime-ip",
            "192.168.3.10",
            "--runtime-port",
            "8081" if role == "A" else "8082",
            "--local-vlan-ip",
            "192.168.1.10" if role == "A" else "192.168.2.10",
            "--masque-url",
            f"https://192.168.3.10:{4433 if role == 'A' else 4434}/masque",
            "--state-dir",
            str(tmp_path / f"state-{role.lower()}"),
            "--log-file",
            str(tmp_path / f"interactive-{role.lower()}.log"),
            *extra,
        ]
    )


def _logger() -> logging.Logger:
    logger = logging.getLogger(f"masque-interactive-test-{id(object())}")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _ipv4_packet(source: str, destination: str) -> bytes:
    packet = bytearray(20)
    packet[0] = 0x45
    packet[2:4] = (20).to_bytes(2, "big")
    packet[8] = 64
    packet[9] = 6
    packet[12:16] = socket.inet_aton(source)
    packet[16:20] = socket.inet_aton(destination)
    return bytes(packet)


class FakeRouteBackend:
    def __init__(self) -> None:
        self.routes: set[str] = set()
        self.operations: list[tuple[str, str]] = []

    async def add(self, cidr: str) -> None:
        self.routes.add(cidr)
        self.operations.append(("add", cidr))

    async def remove(self, cidr: str) -> None:
        self.routes.discard(cidr)
        self.operations.append(("remove", cidr))


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


def test_parser_uses_runtime_query_and_role_specific_control_ports(tmp_path):
    module = _load_example()
    a = module._apply_role_defaults(_arguments(module, tmp_path, "A"))
    b = module._apply_role_defaults(_arguments(module, tmp_path, "B"))

    assert (a.runtime_port, b.runtime_port) == (8081, 8082)
    assert (a.control_port, b.control_port) == (18081, 18082)
    assert a.message_port == b.message_port == 4001
    assert not hasattr(a, "local_agent_ip")
    assert not hasattr(a, "peer_agent_ip")
    assert not hasattr(a, "target_agent_id")


async def test_query_agent_ip_uses_get_ue_info_and_logs_result(tmp_path):
    module = _load_example()
    created = []

    class FakeRuntime:
        def __init__(self, host, port, *, logger):
            created.append((host, port, logger))
            self.closed = False

        async def get_ue_agent_ip(self):
            return "8.8.8.7"

        async def close(self):
            self.closed = True

    logger = module._configure_logger("A", str(tmp_path / "query.log"))
    result = await module._query_agent_ip(
        runtime_ip="192.168.3.10",
        runtime_port=8081,
        role="A",
        logger=logger,
        runtime_factory=FakeRuntime,
    )

    assert result == "8.8.8.7"
    assert created[0][:2] == ("192.168.3.10", 8081)
    text = (tmp_path / "query.log").read_text(encoding="utf-8")
    assert '"event": "UE_INFO_AGENT_TUN_IP"' in text
    assert '"url": "http://192.168.3.10:8081/v1/ue/info"' in text
    assert '"agent_tun_ip": "8.8.8.7"' in text


async def test_peer_configuration_installs_and_replaces_host_route():
    module = _load_example()
    routes = FakeRouteBackend()
    state = module.PeerState(
        local_agent_ip="8.8.8.7",
        route_backend=routes,
        role="A",
        logger=_logger(),
    )

    await state.configure("8.8.8.8")
    await state.configure("8.8.8.9")

    assert state.peer_agent_ip == "8.8.8.9"
    assert routes.routes == {"8.8.8.9/32"}
    assert routes.operations == [
        ("add", "8.8.8.8/32"),
        ("add", "8.8.8.9/32"),
        ("remove", "8.8.8.8/32"),
    ]


async def test_packet_pumps_use_curl_configured_peer_ip():
    module = _load_example()
    routes = FakeRouteBackend()
    state_a = module.PeerState(
        local_agent_ip="8.8.8.7",
        route_backend=routes,
        role="A",
        logger=_logger(),
    )
    await state_a.configure("8.8.8.8")
    valid_up = _ipv4_packet("8.8.8.7", "8.8.8.8")
    invalid_up = _ipv4_packet("8.8.8.7", "8.8.8.9")
    tun_a = FakeTun([invalid_up, valid_up])
    masque = FakeMasque()
    await module._pump_uplink(
        tun=tun_a,
        masque=masque,
        peer_state=state_a,
        mtu=1280,
        logger=_logger(),
        role="A",
    )
    assert masque.sent == [valid_up]

    state_b = module.PeerState(
        local_agent_ip="8.8.8.8",
        route_backend=FakeRouteBackend(),
        role="B",
        logger=_logger(),
    )
    await state_b.configure("8.8.8.7")
    tun_b = FakeTun()
    await module._write_downlink(
        valid_up,
        tun=tun_b,
        peer_state=state_b,
        mtu=1280,
        logger=_logger(),
        role="B",
    )
    assert tun_b.writes == [valid_up]


async def test_control_curl_configures_peer_then_triggers_exact_message_url(tmp_path):
    module = _load_example()
    control_port = _free_port()
    routes = FakeRouteBackend()
    state = module.PeerState(
        local_agent_ip="8.8.8.7",
        route_backend=routes,
        role="A",
        logger=_logger(),
    )

    async def peer_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://8.8.8.8:4001/message"
        assert json.loads(request.content) == {"content": "hello B"}
        return httpx.Response(200, json={"status": "OK"})

    server = module.ControlServer(
        role="A",
        host="127.0.0.1",
        port=control_port,
        peer_state=state,
        message_port=4001,
        message_timeout=5.0,
        logger=_logger(),
        http_transport=httpx.MockTransport(peer_handler),
    )
    await server.start()
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            configured = await client.post(
                f"http://127.0.0.1:{control_port}/test/peer",
                json={"peer_agent_ip": "8.8.8.8"},
            )
            sent = await client.post(
                f"http://127.0.0.1:{control_port}/test/send",
                json={"content": "hello B"},
            )
        assert configured.json()["peer_agent_ip"] == "8.8.8.8"
        assert sent.json() == {
            "status": "OK",
            "peer_agent_ip": "8.8.8.8",
            "peer_response": {"status": "OK"},
        }
        assert routes.routes == {"8.8.8.8/32"}
    finally:
        await server.close()


async def test_message_endpoint_logs_received_json_from_configured_peer(tmp_path):
    module = _load_example()
    port = _free_port()
    state = module.PeerState(
        local_agent_ip="127.0.0.1",
        route_backend=FakeRouteBackend(),
        role="B",
        logger=_logger(),
    )
    state.peer_agent_ip = "127.0.0.1"
    logger = module._configure_logger("B", str(tmp_path / "receiver.log"))
    server = module.MessageServer(
        role="B",
        peer_state=state,
        port=port,
        logger=logger,
    )
    await server.start()
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/message",
                json={"content": "hello B"},
            )
        assert response.json() == {"status": "OK"}
        assert server.last_message == {"content": "hello B"}
    finally:
        await server.close()

    log_text = (tmp_path / "receiver.log").read_text(encoding="utf-8")
    assert '"event": "MESSAGE_RECEIVED"' in log_text
    assert '"path": "/message"' in log_text
    assert '"payload": {"content": "hello B"}' in log_text
