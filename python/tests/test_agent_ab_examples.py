from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_sdk import (
    AgentLifecycleState,
    ComputeRequestType,
    NetworkMessageAction,
    NetworkMessageType,
)


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
            "--masque-url",
            "https://192.168.3.10:4433/.well-known/masque/ip",
        ]
    )


def test_agent_b_defaults_to_the_bundled_local_video():
    module = _load_example("agent_b_test")
    args = _base_arguments(module)

    assert args.video_source == "file"
    assert args.loop_video is True
    assert args.capability == "dog-vision"
    assert args.media_timeout == 120.0
    assert "local_vlan_ip" not in vars(args)
    assert Path(args.video_file).resolve().is_file()


def test_linux_ab_defaults_use_the_android_dog_vision_capability():
    agent_a = _base_arguments(_load_example("agent_a_test"))
    agent_b = _base_arguments(_load_example("agent_b_test"))

    assert agent_a.target_capability == "dog-vision"
    assert agent_a.compute_capability_id == "dog-vision"
    assert agent_a.media_timeout == 120.0
    assert agent_b.capability == "dog-vision"


async def test_agent_a_runs_acn_and_computing_consumer_flow():
    module = _load_example("agent_a_test")
    args = _base_arguments(module)
    assert args.prompt is False
    assert args.dnn == "internet"
    assert args.fresh_registration is False
    assert args.force_registration is False
    args.fresh_registration = True
    args.deregister_on_exit = True
    previous_profile = SimpleNamespace(
        agent_id="did:example:a-old",
        agent_name="Agent-A",
        identity_vc={"id": "vc0-a-old"},
    )
    profile = SimpleNamespace(
        agent_id="did:example:a",
        agent_name="Agent-A",
        identity_vc={"id": "vc0-a"},
    )
    ability = SimpleNamespace(
        abilities=("agent_discovery",),
        ability_vc={"id": "vc1-a"},
        valid_until=None,
    )
    target = SimpleNamespace(
        agent_id="did:example:b",
        service_endpoints="http://agent-b:4001/A2A/message",
        skills=("dog-vision",),
        priority=1,
    )
    member = SimpleNamespace(
        agent_ip="10.60.0.3",
        service_endpoint="http://agent-b:4001/A2A/message",
    )
    snapshot = SimpleNamespace(
        members_by_agent_id={target.agent_id: member}, generation=1
    )
    create_status = SimpleNamespace(
        compute_service_session_id="compute-session-1",
        status="ACCEPTED",
        status_revision="1",
        cause="",
    )
    query_status = SimpleNamespace(
        compute_service_session_id="compute-session-1",
        status="READY",
        status_revision="2",
        cause="",
    )
    release_status = SimpleNamespace(
        compute_service_session_id="compute-session-1",
        status="RELEASED",
        status_revision="3",
        cause="",
    )
    stream = SimpleNamespace(
        recv=AsyncMock(return_value=b"processed-frame"),
        close=AsyncMock(),
    )
    sdk = SimpleNamespace(
        agent_lifecycle_state=AgentLifecycleState.CARD_PUBLISHED,
        local_profile=previous_profile,
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
        deregister_identity=AsyncMock(
            return_value=SimpleNamespace(success=True, message="")
        ),
        discover_agents=AsyncMock(return_value=[target]),
        create_group=AsyncMock(
            return_value=SimpleNamespace(group_id="group-a-b")
        ),
        get_group_snapshot=AsyncMock(return_value=snapshot),
        send_message=AsyncMock(
            side_effect=[
                SimpleNamespace(delivered=True, message_id="message-a-b"),
                SimpleNamespace(delivered=True, message_id="compute-message-a-b"),
            ]
        ),
        create_computing_session=AsyncMock(return_value=create_status),
        query_computing_session=AsyncMock(return_value=query_status),
        cancel_computing_session=AsyncMock(),
        release_computing_session=AsyncMock(return_value=release_status),
        await_computing_session_closed=AsyncMock(),
        get_processed_video_stream=AsyncMock(return_value=stream),
        close=AsyncMock(),
    )

    result = await module.run_agent_a(args, sdk=sdk)

    assert result == {
        "agent_id": "did:example:a",
        "target_agent_id": "did:example:b",
        "group_id": "group-a-b",
        "message_id": "message-a-b",
        "compute_message_id": "compute-message-a-b",
        "compute_service_session_id": "compute-session-1",
        "received_frames": 1,
        "terminal_status": release_status,
    }
    assert "capabilities" not in sdk.register_capabilities.await_args.kwargs
    assert sdk.register_capabilities.await_args.kwargs["credentials"] == [
        ability.ability_vc
    ]
    assert "task_id" not in sdk.discover_agents.await_args.kwargs
    assert sdk.discover_agents.await_args.kwargs["required_skills"] == [
        "dog-vision"
    ]
    assert sdk.create_group.await_args.args[1] == ["did:example:b"]
    assert sdk.create_group.await_args.kwargs["dnn"] == "internet"
    assert sdk.send_message.await_args_list[0].args[:2] == (
        "group-a-b",
        "did:example:b",
    )
    create_request = sdk.create_computing_session.await_args.args[0]
    assert create_request.request_type is ComputeRequestType.CREATE
    assert create_request.acn_context.group_id == "group-a-b"
    assert create_request.acn_context.requester_agent_id == "did:example:a"
    assert create_request.acn_context.target_agent_id == "did:example:b"
    assert create_request.constraints.capability_id == "dog-vision"
    assert create_request.constraints.resources.cpu_millicores == 2000
    assert create_request.constraints.resources.memory_mib == 4096
    assert sdk.send_message.await_args_list[1].args[2] == {
        "compute_service_session_id": "compute-session-1"
    }
    assert sdk.send_message.await_args_list[1].kwargs == {
        "timeout_seconds": 10.0,
        "message_type": "computing_video_session",
        "task_id": "computing:compute-session-1",
    }
    query_request = sdk.query_computing_session.await_args.args[0]
    assert query_request.request_type is ComputeRequestType.QUERY
    assert query_request.compute_service_session_id == "compute-session-1"
    sdk.get_processed_video_stream.assert_awaited_once_with(
        "compute-session-1", timeout_seconds=120.0
    )
    stream.recv.assert_awaited_once()
    stream.close.assert_awaited_once()
    release_request = sdk.release_computing_session.await_args.args[0]
    assert release_request.request_type is ComputeRequestType.RELEASE
    assert release_request.compute_service_session_id == "compute-session-1"
    sdk.await_computing_session_closed.assert_awaited_once_with(
        "compute-session-1", timeout_seconds=30.0
    )
    sdk.cancel_computing_session.assert_not_awaited()
    assert sdk.deregister_identity.await_args_list[0].args == (
        "did:example:a-old",
    )
    assert sdk.deregister_identity.await_args_list[0].kwargs == {
        "reason": "replaced"
    }
    assert sdk.deregister_identity.await_args_list[1].args == (
        "did:example:a",
    )
    assert sdk.deregister_identity.await_args_list[1].kwargs == {
        "reason": "retired"
    }
    sdk.close.assert_awaited_once()


async def test_agent_b_publishes_capability_and_can_stop_before_session():
    module = _load_example("agent_b_test")
    args = _base_arguments(module)
    assert args.prompt is False
    assert args.fresh_registration is False
    assert args.force_registration is False
    args.deregister_on_exit = True
    profile = SimpleNamespace(
        agent_id="did:example:b",
        agent_name="Agent-B",
        identity_vc={"id": "vc0-b"},
    )
    ability = SimpleNamespace(
        abilities=("agent_discovery",),
        ability_vc={"id": "vc1-b"},
        valid_until=None,
    )
    sdk = SimpleNamespace(
        agent_lifecycle_state=AgentLifecycleState.NO_IDENTITY,
        local_profile=None,
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
        deregister_identity=AsyncMock(
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
    assert result["capability"] == "dog-vision"
    assert result["completed_sessions"] == []
    assert sdk.register_capabilities.await_args.kwargs["capabilities"] == [
        "dog-vision"
    ]
    assert sdk.register_capabilities.await_args.kwargs["credentials"] == [
        ability.ability_vc
    ]
    assert (
        sdk.register_capabilities.await_args.kwargs["test_vc_private_key_path"]
        is None
    )
    sdk.deregister_identity.assert_awaited_once_with(
        "did:example:b", reason="retired"
    )
    sdk.close.assert_awaited_once()


async def test_agent_b_reuses_published_profile_without_registering_again():
    module = _load_example("agent_b_test")
    args = _base_arguments(module)
    profile = SimpleNamespace(
        agent_id="did:example:b",
        agent_name="Agent-B",
        identity_vc={"id": "vc0-b"},
    )
    sdk = SimpleNamespace(
        agent_lifecycle_state=AgentLifecycleState.CARD_PUBLISHED,
        local_profile=profile,
        register_network_message_listener=MagicMock(return_value=lambda: None),
        register_group_message_listener=MagicMock(return_value=lambda: None),
        init=AsyncMock(
            return_value=SimpleNamespace(
                agent_tun_cidr="10.60.0.3/32",
                agent_tcp_endpoint="10.60.0.3:4001",
                masque_proxy_endpoint=args.masque_url,
            )
        ),
        apply_identity=AsyncMock(),
        get_network_ability=AsyncMock(),
        register_capabilities=AsyncMock(),
        close=AsyncMock(),
    )
    stop_event = asyncio.Event()
    stop_event.set()

    result = await module.run_agent_b(args, sdk=sdk, stop_event=stop_event)

    assert result["agent_id"] == profile.agent_id
    sdk.apply_identity.assert_not_awaited()
    sdk.get_network_ability.assert_not_awaited()
    sdk.register_capabilities.assert_not_awaited()
    sdk.close.assert_awaited_once()


async def test_agent_b_fresh_registration_replaces_persisted_profile():
    module = _load_example("agent_b_test")
    args = _base_arguments(module)
    args.fresh_registration = True
    previous_profile = SimpleNamespace(
        agent_id="did:example:b-old",
        agent_name="Agent-B",
        identity_vc={"id": "vc0-b-old"},
    )
    profile = SimpleNamespace(
        agent_id="did:example:b-new",
        agent_name="Agent-B",
        identity_vc={"id": "vc0-b-new"},
    )
    ability = SimpleNamespace(
        abilities=("agent_discovery",),
        ability_vc={"id": "vc1-b-new"},
        valid_until=None,
    )
    sdk = SimpleNamespace(
        agent_lifecycle_state=AgentLifecycleState.CARD_PUBLISHED,
        local_profile=previous_profile,
        register_network_message_listener=MagicMock(return_value=lambda: None),
        register_group_message_listener=MagicMock(return_value=lambda: None),
        init=AsyncMock(
            return_value=SimpleNamespace(
                agent_tun_cidr="10.60.0.3/32",
                agent_tcp_endpoint="10.60.0.3:4001",
                masque_proxy_endpoint=args.masque_url,
            )
        ),
        deregister_identity=AsyncMock(
            return_value=SimpleNamespace(success=True, message="")
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

    result = await module.run_agent_b(args, sdk=sdk, stop_event=stop_event)

    assert result["agent_id"] == profile.agent_id
    sdk.deregister_identity.assert_awaited_once_with(
        previous_profile.agent_id, reason="replaced"
    )
    sdk.apply_identity.assert_awaited_once()
    sdk.get_network_ability.assert_awaited_once_with(profile.agent_id)
    sdk.register_capabilities.assert_awaited_once()
    sdk.close.assert_awaited_once()


async def test_agent_b_force_registration_resets_only_local_state():
    module = _load_example("agent_b_test")
    args = _base_arguments(module)
    args.force_registration = True
    previous_profile = SimpleNamespace(
        agent_id="did:example:b-old",
        agent_name="Agent-B",
        identity_vc={"id": "vc0-b-old"},
    )
    profile = SimpleNamespace(
        agent_id="did:example:b-new",
        agent_name="Agent-B",
        identity_vc={"id": "vc0-b-new"},
    )
    ability = SimpleNamespace(
        abilities=("agent_discovery",),
        ability_vc={"id": "vc1-b-new"},
        valid_until=None,
    )
    sdk = SimpleNamespace(
        agent_lifecycle_state=AgentLifecycleState.CARD_PUBLISHED,
        local_profile=previous_profile,
        register_network_message_listener=MagicMock(return_value=lambda: None),
        register_group_message_listener=MagicMock(return_value=lambda: None),
        init=AsyncMock(
            return_value=SimpleNamespace(
                agent_tun_cidr="10.60.0.3/32",
                agent_tcp_endpoint="10.60.0.3:4001",
                masque_proxy_endpoint=args.masque_url,
            )
        ),
        reset_agent=AsyncMock(
            return_value=SimpleNamespace(success=True, message="local state reset")
        ),
        deregister_identity=AsyncMock(),
        apply_identity=AsyncMock(return_value=profile),
        get_network_ability=AsyncMock(return_value=ability),
        register_capabilities=AsyncMock(
            return_value=SimpleNamespace(success=True, message="")
        ),
        close=AsyncMock(),
    )
    stop_event = asyncio.Event()
    stop_event.set()

    result = await module.run_agent_b(args, sdk=sdk, stop_event=stop_event)

    assert result["agent_id"] == profile.agent_id
    sdk.reset_agent.assert_awaited_once_with()
    sdk.deregister_identity.assert_not_awaited()
    sdk.apply_identity.assert_awaited_once()
    sdk.get_network_ability.assert_awaited_once_with(profile.agent_id)
    sdk.register_capabilities.assert_awaited_once()
    sdk.close.assert_awaited_once()


@pytest.mark.parametrize("example_name", ["agent_a_test", "agent_b_test"])
def test_registration_modes_are_mutually_exclusive(example_name):
    module = _load_example(example_name)
    with pytest.raises(SystemExit):
        module.parser().parse_args(
            [
                "--runtime-ip",
                "192.168.3.10",
                "--masque-url",
                "https://192.168.3.10:4433/.well-known/masque/ip",
                "--fresh-registration",
                "--force-registration",
            ]
        )


async def test_agent_b_fresh_registration_fails_closed_when_cleanup_is_rejected():
    module = _load_example("agent_b_test")
    args = _base_arguments(module)
    args.fresh_registration = True
    previous_profile = SimpleNamespace(
        agent_id="did:example:b-old",
        agent_name="Agent-B",
        identity_vc={"id": "vc0-b-old"},
    )
    sdk = SimpleNamespace(
        agent_lifecycle_state=AgentLifecycleState.CARD_PUBLISHED,
        local_profile=previous_profile,
        register_network_message_listener=MagicMock(return_value=lambda: None),
        register_group_message_listener=MagicMock(return_value=lambda: None),
        init=AsyncMock(
            return_value=SimpleNamespace(
                agent_tun_cidr="10.60.0.3/32",
                agent_tcp_endpoint="10.60.0.3:4001",
                masque_proxy_endpoint=args.masque_url,
            )
        ),
        deregister_identity=AsyncMock(
            return_value=SimpleNamespace(
                success=False, message="network rejected cleanup"
            )
        ),
        apply_identity=AsyncMock(),
        get_network_ability=AsyncMock(),
        register_capabilities=AsyncMock(),
        close=AsyncMock(),
    )

    with pytest.raises(RuntimeError, match="network rejected cleanup"):
        await module.run_agent_b(args, sdk=sdk)

    sdk.apply_identity.assert_not_awaited()
    sdk.register_capabilities.assert_not_awaited()
    sdk.close.assert_awaited_once()


async def test_agent_b_callbacks_accept_group_and_record_a2a_message(capsys):
    module = _load_example("agent_b_test")
    network_listener = module.AgentBNetworkListener()

    action = await network_listener.on_network_message(
        NetworkMessageType.GROUP_INVITATION,
        {"group_id": "group-a-b"},
    )

    notifications = asyncio.Queue()
    group_listener = module.AgentBGroupListener(notifications)
    await group_listener.on_group_message(
        "group-a-b", "did:example:a", {"content": "hello B"}
    )
    assert notifications.empty()
    await group_listener.on_group_message(
        "group-a-b",
        "did:example:a",
        {"compute_service_session_id": "compute-session-1"},
    )
    await group_listener.on_group_message(
        "group-a-b",
        "did:example:a",
        {"compute_service_session_id": "compute-session-1"},
    )

    assert action is NetworkMessageAction.ACCEPT
    assert group_listener.last_message == {
        "group_id": "group-a-b",
        "sender_agent_id": "did:example:a",
        "payload": {"compute_service_session_id": "compute-session-1"},
    }
    assert await notifications.get() == module.ComputeNotification(
        "group-a-b", "did:example:a", "compute-session-1"
    )
    assert notifications.empty()
    assert '"event": "B_MESSAGE_RECEIVED"' in capsys.readouterr().out


async def test_agent_b_queues_session_then_starts_video_outside_callback():
    module = _load_example("agent_b_test")
    args = _base_arguments(module)
    profile = SimpleNamespace(
        agent_id="did:example:b",
        agent_name="Agent-B",
        identity_vc={"id": "vc0-b"},
    )
    snapshot = SimpleNamespace(
        members_by_agent_id={
            "did:example:a": SimpleNamespace(agent_id="did:example:a"),
            "did:example:b": SimpleNamespace(agent_id="did:example:b"),
        }
    )
    upload = SimpleNamespace(
        track_id="camera-track-1",
        state="STOPPED",
        stop=AsyncMock(),
    )
    registered = {}

    def register_group_listener(listener):
        registered["group"] = listener
        return lambda: None

    sdk = SimpleNamespace(
        agent_lifecycle_state=AgentLifecycleState.CARD_PUBLISHED,
        local_profile=profile,
        register_network_message_listener=MagicMock(return_value=lambda: None),
        register_group_message_listener=MagicMock(side_effect=register_group_listener),
        init=AsyncMock(
            return_value=SimpleNamespace(
                agent_tun_cidr="10.60.0.3/32",
                agent_tcp_endpoint="10.60.0.3:4001",
                masque_proxy_endpoint=args.masque_url,
            )
        ),
        apply_identity=AsyncMock(),
        get_network_ability=AsyncMock(),
        register_capabilities=AsyncMock(),
        get_group_snapshot=AsyncMock(return_value=snapshot),
        start_video_upload=AsyncMock(return_value=upload),
        close=AsyncMock(),
    )

    run_task = asyncio.create_task(module.run_agent_b(args, sdk=sdk))
    while "group" not in registered:
        await asyncio.sleep(0)
    await registered["group"].on_group_message(
        "group-a-b",
        "did:example:a",
        {"compute_service_session_id": "compute-session-1"},
    )
    # The callback only queues the ID, so its acknowledgement is independent
    # from camera access and WebRTC negotiation.
    sdk.start_video_upload.assert_not_awaited()

    result = await run_task

    assert result["completed_sessions"] == ["compute-session-1"]
    sdk.get_group_snapshot.assert_awaited_once_with("group-a-b")
    sdk.start_video_upload.assert_awaited_once_with(
        "compute-session-1",
        camera_id=0,
        width=1280,
        height=720,
        fps=30,
        bitrate_kbps=2500,
        timeout_seconds=120.0,
    )
    upload.stop.assert_not_awaited()
    sdk.close.assert_awaited_once()


def test_agent_a_builds_cancel_request_and_only_notifies_session_id():
    module = _load_example("agent_a_test")

    request = module._compute_control_request(
        ComputeRequestType.CANCEL, "compute-session-1"
    )

    assert request.request_type is ComputeRequestType.CANCEL
    assert request.compute_service_session_id == "compute-session-1"
    assert request.target_request_id is None
    assert module._compute_session_message("compute-session-1") == {
        "compute_service_session_id": "compute-session-1"
    }
