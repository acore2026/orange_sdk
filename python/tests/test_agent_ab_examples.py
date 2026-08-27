from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from agent_sdk import NetworkMessageAction, NetworkMessageType


EXAMPLE_DIR = Path(__file__).parents[1] / "examples"


def _load_example(name: str):
    path = EXAMPLE_DIR / f"{name}.py"
    sys.path.insert(0, str(EXAMPLE_DIR))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def _base_arguments(module):
    return module.parser().parse_args(
        [
            "--runtime-ip",
            "192.168.3.10",
            "--local-vlan-ip",
            "192.168.1.10",
            "--masque-url",
            "https://192.168.3.10:4433/.well-known/masque/ip",
            "--no-prompt",
        ]
    )


async def test_agent_a_discovers_b_groups_and_sends_from_group_cache():
    module = _load_example("agent_a_test")
    args = _base_arguments(module)
    profile = SimpleNamespace(
        agent_id="did:example:a",
        agent_name="Agent-A",
        identity_vc={"id": "vc0-a"},
    )
    ability = SimpleNamespace(
        abilities=("agent_discovery",), ability_vc={"id": "vc1-a"}
    )
    target = SimpleNamespace(
        agent_id="did:example:b",
        ip="10.60.0.3",
        tcp_port=4001,
        udp_port=28443,
        skills=("text",),
        priority=1,
    )
    member = SimpleNamespace(agent_ip="10.60.0.3", tcp_port=4001)
    snapshot = SimpleNamespace(
        members_by_agent_id={target.agent_id: member}, generation=1
    )
    sdk = SimpleNamespace(
        register_network_message_listener=MagicMock(return_value=lambda: None),
        register_group_message_listener=MagicMock(return_value=lambda: None),
        init=AsyncMock(
            return_value=SimpleNamespace(
                agent_tun_cidr="10.60.0.2/32",
                agent_tcp_endpoint="10.60.0.2:4001",
                masque_proxy_endpoint=args.masque_url,
            )
        ),
        apply_identity=AsyncMock(return_value=profile),
        get_network_ability=AsyncMock(return_value=ability),
        register_capabilities=AsyncMock(
            return_value=SimpleNamespace(success=True, message="")
        ),
        discover_agents=AsyncMock(return_value=[target]),
        create_group=AsyncMock(
            return_value=SimpleNamespace(group_id="group-a-b")
        ),
        get_group_snapshot=AsyncMock(return_value=snapshot),
        send_message=AsyncMock(
            return_value=SimpleNamespace(
                delivered=True, message_id="message-a-b"
            )
        ),
        close=AsyncMock(),
    )

    result = await module.run_agent_a(args, sdk=sdk)

    assert result == {
        "agent_id": "did:example:a",
        "target_agent_id": "did:example:b",
        "group_id": "group-a-b",
        "message_id": "message-a-b",
    }
    assert "capabilities" not in sdk.register_capabilities.await_args.kwargs
    assert sdk.discover_agents.await_args.kwargs["required_skills"] == ["text"]
    assert sdk.create_group.await_args.args[1] == ["did:example:b"]
    assert sdk.send_message.await_args.args[:2] == (
        "group-a-b",
        "did:example:b",
    )
    sdk.close.assert_awaited_once()


async def test_agent_b_publishes_capability_and_can_exit_after_message_event():
    module = _load_example("agent_b_test")
    args = _base_arguments(module)
    args.exit_after_message = True
    profile = SimpleNamespace(
        agent_id="did:example:b",
        agent_name="Agent-B",
        identity_vc={"id": "vc0-b"},
    )
    ability = SimpleNamespace(
        abilities=("agent_discovery",), ability_vc={"id": "vc1-b"}
    )
    sdk = SimpleNamespace(
        register_network_message_listener=MagicMock(return_value=lambda: None),
        register_group_message_listener=MagicMock(return_value=lambda: None),
        init=AsyncMock(
            return_value=SimpleNamespace(
                agent_tun_cidr="10.60.0.3/32",
                agent_tcp_endpoint="10.60.0.3:4001",
                masque_proxy_endpoint=args.masque_url,
            )
        ),
        apply_identity=AsyncMock(return_value=profile),
        get_network_ability=AsyncMock(return_value=ability),
        register_capabilities=AsyncMock(
            return_value=SimpleNamespace(success=True, message="")
        ),
        close=AsyncMock(),
    )
    stop_event = asyncio.Event()
    stop_event.set()

    result = await module.run_agent_b(
        args, sdk=sdk, stop_event=stop_event
    )

    assert result["agent_id"] == "did:example:b"
    assert result["capability"] == "text"
    assert sdk.register_capabilities.await_args.kwargs["capabilities"] == [
        "text"
    ]
    sdk.close.assert_awaited_once()


async def test_agent_b_callbacks_accept_group_and_record_a2a_message(capsys):
    module = _load_example("agent_b_test")
    network_listener = module.AgentBNetworkListener()

    action = await network_listener.on_network_message(
        NetworkMessageType.GROUP_INVITATION,
        {"group_id": "group-a-b"},
    )

    message_event = asyncio.Event()
    group_listener = module.AgentBGroupListener(message_event)
    await group_listener.on_group_message(
        "group-a-b", "did:example:a", {"content": "hello B"}
    )

    assert action is NetworkMessageAction.ACCEPT
    assert message_event.is_set()
    assert group_listener.last_message == {
        "group_id": "group-a-b",
        "sender_agent_id": "did:example:a",
        "payload": {"content": "hello B"},
    }
    assert '"event": "B_MESSAGE_RECEIVED"' in capsys.readouterr().out
