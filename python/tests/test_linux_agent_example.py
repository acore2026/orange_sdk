from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


EXAMPLE_PATH = Path(__file__).parents[1] / "examples" / "linux_agent.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("linux_agent", EXAMPLE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_linux_agent_contains_every_public_sdk_call():
    tree = ast.parse(EXAMPLE_PATH.read_text(encoding="utf-8"))
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    expected = {
        "init",
        "register_network_message_listener",
        "register_group_message_listener",
        "apply_identity",
        "set_local_profile_for_restore",
        "deregister_identity",
        "get_network_ability",
        "register_capabilities",
        "update_capabilities",
        "discover_agents",
        "create_group",
        "get_group_snapshot",
        "send_message",
        "create_offloading_session",
        "start_video_upload",
        "get_processed_video_stream",
        "close",
    }
    assert expected <= called_attributes


def test_linux_agent_parser_accepts_full_flow_values():
    module = _load_example()
    arguments = module.parser().parse_args(
        [
            "--runtime-ip",
            "192.168.3.10",
            "--local-vlan-ip",
            "192.168.1.10",
            "--agent-name",
            "Agent A",
            "--owner",
            "owner-a",
            "--masque-url",
            "https://192.168.3.10:4433/.well-known/masque/ip",
            "--message",
            '{"type":"text","content":"hello"}',
            "--test-capability",
            "robot-control",
            "--test-capability",
            "voice",
        ]
    )
    assert arguments.message == {"type": "text", "content": "hello"}
    assert arguments.test_capability == ["robot-control", "voice"]
    assert arguments.dnn == "internet"
    assert arguments.keep_identity is False


def test_linux_agent_rejects_non_object_message():
    module = _load_example()
    with pytest.raises(Exception, match="JSON object"):
        module._message("[1, 2, 3]")


async def test_linux_agent_full_flow_executes_every_business_api():
    module = _load_example()
    args = module.parser().parse_args(
        [
            "--runtime-ip",
            "192.168.3.10",
            "--local-vlan-ip",
            "192.168.1.10",
            "--agent-name",
            "Agent A",
            "--owner",
            "owner-a",
            "--masque-url",
            "https://192.168.3.10:4433/.well-known/masque/ip",
            "--test-capability",
            "robot-control",
        ]
    )
    profile = SimpleNamespace(
        agent_id="did:example:a", agent_name="Agent A", identity_vc={"id": "vc0"}
    )
    ability = SimpleNamespace(abilities=("text",), ability_vc={"id": "vc1"})
    target = SimpleNamespace(agent_id="did:example:b")
    group = SimpleNamespace(group_id="g1")
    snapshot = SimpleNamespace(
        generation=1, members_by_agent_id={target.agent_id: target}
    )
    upload = SimpleNamespace(
        track_id="track-1",
        state="RUNNING",
        pause=AsyncMock(),
        resume=AsyncMock(),
        stop=AsyncMock(),
    )
    stream = SimpleNamespace(recv=AsyncMock(return_value={"frame": "processed"}))
    sdk = SimpleNamespace(
        init=AsyncMock(return_value=SimpleNamespace(agent_tun_cidr="8.8.8.7/32")),
        apply_identity=AsyncMock(return_value=profile),
        set_local_profile_for_restore=Mock(),
        get_network_ability=AsyncMock(return_value=ability),
        register_capabilities=AsyncMock(return_value=SimpleNamespace(success=True)),
        update_capabilities=AsyncMock(return_value=SimpleNamespace(success=True)),
        discover_agents=AsyncMock(return_value=[target]),
        create_group=AsyncMock(return_value=group),
        get_group_snapshot=AsyncMock(return_value=snapshot),
        send_message=AsyncMock(
            return_value=SimpleNamespace(message_id="message-1", delivered=True)
        ),
        create_offloading_session=AsyncMock(
            return_value=SimpleNamespace(session_id="session-1", state="CONNECTED")
        ),
        start_video_upload=AsyncMock(return_value=upload),
        get_processed_video_stream=AsyncMock(return_value=stream),
        deregister_identity=AsyncMock(return_value=SimpleNamespace(success=True)),
    )
    steps = []

    async def before_step(interface_name, description):
        steps.append((interface_name, description))

    await module.run_full_flow(sdk, args, before_step=before_step)

    sdk.set_local_profile_for_restore.assert_called_once_with(profile)
    assert sdk.register_capabilities.await_args.kwargs == {
        "priority": 1,
        "credentials": [ability.ability_vc],
        "capabilities": ["robot-control"],
        "test_vc_private_key_path": None,
    }
    assert sdk.create_group.await_args.kwargs["dnn"] == "internet"
    assert (
        sdk.create_offloading_session.await_args.kwargs["workload_type"]
        == "video_rendering"
    )
    for method_name in (
        "init",
        "apply_identity",
        "get_network_ability",
        "register_capabilities",
        "update_capabilities",
        "discover_agents",
        "create_group",
        "get_group_snapshot",
        "send_message",
        "create_offloading_session",
        "start_video_upload",
        "get_processed_video_stream",
        "deregister_identity",
    ):
        getattr(sdk, method_name).assert_awaited()
    upload.pause.assert_awaited_once()
    upload.resume.assert_awaited_once()
    upload.stop.assert_awaited_once()
    stream.recv.assert_awaited_once()
    assert [name for name, _ in steps] == [
        "sdk.init",
        "sdk.apply_identity",
        "sdk.set_local_profile_for_restore",
        "sdk.get_network_ability",
        "sdk.register_capabilities",
        "sdk.update_capabilities",
        "sdk.discover_agents",
        "sdk.create_group",
        "sdk.get_group_snapshot",
        "sdk.send_message",
        "sdk.create_offloading_session",
        "sdk.start_video_upload",
        "upload.pause",
        "upload.resume",
        "sdk.get_processed_video_stream",
        "stream.recv",
        "upload.stop",
        "sdk.deregister_identity",
    ]
    assert all(description for _, description in steps)
