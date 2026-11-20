"""Real AgentSdk + local QUIC; only core/TUN/MASQUE boundaries are simulated."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_sdk import AGWConfig, AgentSdk
from agent_sdk.routes import MemoryRouteBackend


spec = importlib.util.spec_from_file_location("agw_terminal_example", Path(__file__).parents[1] / "agw_terminal.py")
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


class Tun:
    mtu = 1280

    def __init__(self, ip):
        self.name, self.cidr = "test-agw", f"{ip}/32"

    async def read(self):
        await asyncio.Future()

    async def write(self, packet):
        pass

    async def close(self):
        pass


class Masque:
    connected = True

    async def start(self, on_packet):
        pass

    async def send_packet(self, packet):
        pass

    async def close(self):
        pass


class Server:
    async def start(self, **kwargs):
        pass

    async def close(self):
        pass


class Core:
    def __init__(self, relay, owner):
        self.relay, self.owner = relay, owner
        self.runtimes, self.profiles, self.cards = {}, {}, {}
        self.members = []
        self.version = 1
        self.lock = asyncio.Lock()
        self.fail_profile = False

    def config(self):
        return {"notification_type": "acf_group_config", "version": "1.0.0", "group_id": "g-test",
                "group_version": self.version, "timestamp": datetime.now(timezone.utc).isoformat(), "proof": {},
                "members": {role: {"agent_id": f"did:example:{role}", "agent_name": self.profiles[role],
                    "agent_ip": self.runtimes[role].ip, "service_endpoints": f"http://{self.runtimes[role].ip}:4001/A2A/message",
                    "skills": self.cards[role]} for role in self.members}}

    async def configurations(self):
        responses = await asyncio.gather(*(self.runtimes[role].handler("ACN_AGENT_GROUPING_NOTIFICATION", 2, self.config()) for role in self.members))
        assert all(response["result"] == "ACK" for response in responses)

    async def request(self, runtime, path, body):
        role = runtime.role
        if path == "/idm/v1/identity-applications":
            self.profiles[role] = body["name"]
            return {"result": "success", "agent_id": f"did:example:{role}",
                    "vc0": {"id": f"vc0-{role}", "claims": {"agent_name": body["name"], "agent_id": f"did:example:{role}"}}}
        if path == "/idm/v1/network-ability":
            return {"vc1": {"id": "vc1", "claims": {"network_abilities": ["agent_discovery"]}}}
        if path == "/arf/v1/agent-cards":
            if self.fail_profile:
                raise RuntimeError("Profile authorization rejected")
            self.cards[role] = [vc["claims"]["skill_name"] for vc in body["vc_list"] if "skill_name" in vc.get("claims", {})]
            assert body["vc_list"][0]["id"] == f"vc0-{role}"
            return {"success": True}
        if path == "/arf/v1/agent-discoveries":
            return {"result": [{"priority": 10, "agent_card": {"agent_id": f"did:example:{candidate}",
                    "service_endpoints": "http://localhost/A2A/message", "skills": skills}}
                    for candidate, skills in self.cards.items() if set(body["required_skills"]).issubset(skills)]}
        if path == "/idm/v1/network-ability":
            return {"vc1": {"id": "vc1", "claims": {"network_abilities": ["agent_discovery"]}}}
        if path == "/acf/v1/agents-grouping":
            assert set(body["target_agents"]) == {"did:example:b", "did:example:c"}
            for target in ("b", "c"):
                response = await self.runtimes[target].handler("ACN_AGENT_GROUPING_INVITATION", 1,
                    {"group_info": {"group_id": "g-test", "group_name": body["group_config"]["group_name"]}})
                assert response["result"] == "ACCEPT"
            self.members = ["a", "b", "c"]
            self.relay.apply(self.owner, "g-test", 1, "PREPARED", [f"did:example:{r}" for r in self.members])
            await self.configurations()
            self.relay.apply(self.owner, "g-test", 1, "ACTIVE", [f"did:example:{r}" for r in self.members])
            return {"status": "grouped", "group_id": "g-test"}
        if path == "/acn-agent/v1/agent-deletions":
            async with self.lock:
                if role in self.members:
                    self.members.remove(role)
                    self.version += 1
                    if self.members:
                        ids = [f"did:example:{r}" for r in self.members]
                        self.relay.apply(self.owner, "g-test", self.version, "PREPARED", ids)
                        await self.configurations()
                        self.relay.apply(self.owner, "g-test", self.version, "ACTIVE", ids)
                    else:
                        self.relay.delete(self.owner, "g-test", self.version)
                self.profiles.pop(role, None)
                self.cards.pop(role, None)
            return {"success": True}
        raise AssertionError(f"unexpected public SDK request: {path}")


class Runtime:
    def __init__(self, core, role):
        self.core, self.role = core, role
        self.ip = f"127.0.0.{'abc'.index(role) + 2}"
        self.closed = False
        core.runtimes[role] = self

    async def get_ue_info(self):
        return {"identity": {"supi": f"imsi-00101000000000{'abc'.index(self.role) + 1}"},
                "nas": {"registered": True, "state": "session_ready", "security_context": True},
                "pdu_sessions": [{"pdu_session_id": 1, "state": "active", "type": "IPv4", "dnn": "internet",
                    "snssai": {"sst": 1, "sd": "010203"}, "ipv4": self.ip, "default_route": True}]}

    async def start_downlink(self, handler, on_reconnected=None):
        self.handler = handler

    async def request(self, method, path, body):
        return await self.core.request(self, path, body)

    async def close(self):
        self.closed = True


def arguments(role):
    return example.parser(role).parse_args(["--run-id", "test-run", "--runtime-ip", "127.0.0.1",
        "--masque-url", "https://127.0.0.1:8443/.well-known/masque/ip", "--relay-ipv4", "127.0.0.1",
        "--timeout", "12", "--message-timeout", "3", "--message-interval", ".01"])


def client(core, role, tmp_path, port):
    runtime = Runtime(core, role)
    async def create_tun(name, cidr, mtu):
        return Tun(runtime.ip)
    return AgentSdk(agw_config=AGWConfig("127.0.0.1", port), agent_state_directory=tmp_path / role,
        tun_factory=create_tun, masque_factory=lambda config: Masque(),
        runtime_factory=lambda host, service_port: runtime, server_factory=Server,
        route_backend_factory=lambda config, tun: MemoryRouteBackend())


async def relay_and_core():
    pytest.importorskip("moq.acn")
    from moq.acn.relay import AcnRelay
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    relay = AcnRelay("127.0.0.1", port)
    await relay.start()
    owner = object()
    relay.open_management(owner)
    return relay, Core(relay, owner), port


async def test_three_real_sdks_run_full_scenario_with_quic_and_cleanup(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "identities"))
    relay, core, port = await relay_and_core()
    clients = {role: client(core, role, tmp_path, port) for role in "abc"}
    try:
        async def run(role):
            args = arguments(role)
            args.log_file = str(tmp_path / f"{role}.log")
            await example.run(args, sdk=clients[role])
        await asyncio.gather(*(run(role) for role in "bca"))
        records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith('{"event":')]
        assert {item["role"] for item in records if item["event"] == "SCENARIO_PASSED"} == set("abc")
        confirmations = [item for item in records if item["event"] == "MULTICAST_CONFIRMED"]
        assert len(confirmations) == 3
        assert all(item["receivers"] == ["did:example:b", "did:example:c"] for item in confirmations)
        for message in confirmations:
            copies = [item for item in records if item["event"] == "MULTICAST_RECEIVED" and item["payload"]["message_id"] == message["message_id"]]
            assert {item["role"] for item in copies} == {"b", "c"}
        assert not core.profiles and not core.cards and not core.members
        assert all(runtime.closed for runtime in core.runtimes.values())
        assert not relay.bindings
    finally:
        for sdk in clients.values():
            await sdk.close()
        await relay.stop()


async def test_profile_failure_still_deregisters_identity_and_closes_sdk(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "identities"))
    relay, core, port = await relay_and_core()
    core.fail_profile = True
    sdk = client(core, "b", tmp_path, port)
    try:
        with pytest.raises(RuntimeError, match="Profile authorization rejected"):
            args = arguments("b")
            args.log_file = str(tmp_path / "b.log")
            await example.run(args, sdk=sdk)
        assert not core.profiles and core.runtimes["b"].closed
        assert sdk.local_profile is None
    finally:
        await sdk.close()
        await relay.stop()


def test_old_public_names_are_removed():
    import inspect
    import agent_sdk
    assert not hasattr(agent_sdk, "L5Config")
    assert "agw_config" in inspect.signature(AgentSdk).parameters
    assert "l5_config" not in inspect.signature(AgentSdk).parameters
    assert not any("l5" in name.lower() for name in vars(AgentSdk))


@pytest.mark.parametrize("option,value", [("--timeout", "nan"), ("--message-interval", "0"), ("--messages", "0"), ("--run-id", "../bad")])
def test_bad_batch_and_deadlines_fail_before_network(option, value):
    with pytest.raises(SystemExit):
        example.parser("a").parse_args(["--runtime-ip", "127.0.0.1", "--run-id", "ok", "--relay-ipv4", "127.0.0.1",
            "--masque-url", "https://127.0.0.1:8443/masque", option, value])
