from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    values = [
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
        "--app-log-file",
        str(tmp_path / f"app-{role.lower()}.log"),
        "--sdk-log-file",
        str(tmp_path / f"sdk-{role.lower()}.log"),
        *extra,
    ]
    return module.parser().parse_args(values)


class FakeSdk:
    def __init__(self, role: str) -> None:
        agent_ip = "8.8.8.7" if role == "A" else "8.8.8.8"
        agent_id = f"did:example:{role.lower()}"
        self.init = AsyncMock(
            return_value=SimpleNamespace(
                masque_connected=True,
                agent_tun_cidr=f"{agent_ip}/32",
            )
        )
        self.apply_identity = AsyncMock(
            return_value=SimpleNamespace(agent_id=agent_id)
        )
        self.create_group = AsyncMock(
            return_value=SimpleNamespace(group_id="g1")
        )
        self.get_group_snapshot = AsyncMock(
            return_value=SimpleNamespace(
                generation=1,
                members_by_agent_id={"did:example:b": SimpleNamespace()},
            )
        )
        self.send_message = AsyncMock(
            return_value=SimpleNamespace(message_id="message-1", delivered=True)
        )
        self.deregister_identity = AsyncMock()
        self.close = AsyncMock()
        self.network_listener = None
        self.group_listener = None

    def register_network_message_listener(self, listener):
        self.network_listener = listener
        return lambda: None

    def register_group_message_listener(self, listener):
        self.group_listener = listener
        return lambda: None


def test_parser_applies_distinct_role_defaults(tmp_path):
    module = _load_example()
    a = module._apply_role_defaults(
        _arguments(module, tmp_path, "A", "--target-agent-id", "did:example:b")
    )
    b = module._apply_role_defaults(_arguments(module, tmp_path, "B"))

    assert (a.tun_name, a.tcp_port, a.udp_port) == ("agent_tun_a", 4001, 28443)
    assert (b.tun_name, b.tcp_port, b.udp_port) == ("agent_tun_b", 4001, 28443)
    assert a.state_dir != b.state_dir
    assert a.sdk_log_file != b.sdk_log_file


def test_role_a_requires_target_agent_id(tmp_path):
    module = _load_example()
    args = module._apply_role_defaults(_arguments(module, tmp_path, "A"))
    with pytest.raises(ValueError, match="target-agent-id"):
        module._validate_args(args)


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
            "--target-agent-id",
            "did:example:b",
            "--peer-netns-id",
            str(current_netns),
        )
    )
    with pytest.raises(ValueError, match="same Linux network namespace"):
        module._validate_args(args)


async def test_role_a_creates_group_and_sends_message(tmp_path):
    module = _load_example()
    args = _arguments(
        module,
        tmp_path,
        "A",
        "--target-agent-id",
        "did:example:b",
        "--expected-agent-ip",
        "8.8.8.7",
        "--message",
        '{"content":"hello B"}',
    )
    sdk = FakeSdk("A")

    await module.run_instance(args, sdk=sdk)

    sdk.init.assert_awaited_once()
    sdk.create_group.assert_awaited_once_with(
        "did:example:a",
        ["did:example:b"],
        group_name="masque-two-instance-test",
        scope="private",
        max_members=2,
    )
    sdk.send_message.assert_awaited_once_with(
        "g1",
        "did:example:b",
        {"content": "hello B"},
        timeout_seconds=10.0,
        message_type="text",
        task_id="masque-two-instance-test",
    )
    sdk.close.assert_awaited_once()
    assert '"event": "MASQUE_CONNECTED"' in Path(args.app_log_file).read_text(
        encoding="utf-8"
    )
    assert '"event": "A2A_MESSAGE_DELIVERED"' in Path(
        args.app_log_file
    ).read_text(encoding="utf-8")


async def test_role_b_prints_and_logs_received_message(tmp_path):
    module = _load_example()
    args = _arguments(
        module,
        tmp_path,
        "B",
        "--expected-agent-ip",
        "8.8.8.8",
        "--receive-timeout",
        "2",
        "--post-receive-linger",
        "0",
    )
    sdk = FakeSdk("B")

    running = asyncio.create_task(module.run_instance(args, sdk=sdk))
    for _ in range(20):
        if sdk.group_listener is not None and sdk.apply_identity.await_count:
            break
        await asyncio.sleep(0)
    assert sdk.group_listener is not None
    await sdk.group_listener.on_group_message(
        "g1", "did:example:a", {"content": "hello B"}
    )
    await running

    log_text = Path(args.app_log_file).read_text(encoding="utf-8")
    assert '"event": "A2A_MESSAGE_RECEIVED"' in log_text
    assert '"payload": {"content": "hello B"}' in log_text
    assert '"event": "TEST_PASSED"' in log_text
    sdk.close.assert_awaited_once()
