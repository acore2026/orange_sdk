"""Agent B: publish a capability and run the producer side of video offload.

Outbound SDK operations run continuously by default. Use ``--prompt`` for
manual stepping. Network-initiated group messages are handled immediately; an
invitation is accepted without prompting. The A2A callback queues a computing
session ID and returns immediately, while the main task performs WebRTC setup.
The bundled local MP4 is the default source; a V4L2 camera remains optional.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
from pathlib import Path
from typing import Any, NamedTuple

from agent_sdk import (
    AgentLifecycleState,
    AgentSdk,
    NetworkMessageAction,
    NetworkMessageType,
)
from interactive_linux_agent import EnterStepGate, InteractiveDemoAborted


COMPUTE_SESSION_ID_FIELD = "compute_service_session_id"
DEFAULT_TEST_VIDEO = Path(__file__).with_name("assets") / "video-offload-test.mp4"


class ComputeNotification(NamedTuple):
    group_id: str
    sender_agent_id: str
    compute_service_session_id: str


def _emit(event: str, **fields: Any) -> None:
    print(
        json.dumps(
            {"role": "B", "event": event, **fields},
            ensure_ascii=False,
            default=str,
        ),
        flush=True,
    )


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be greater than or equal to zero")
    return parsed


async def _before_step(
    gate: EnterStepGate | None, interface_name: str, description: str
) -> None:
    if gate is None:
        _emit("STEP_AUTO", interface=interface_name, description=description)
        return
    await gate(interface_name, description)


class AgentBNetworkListener:
    async def on_network_message(self, message_type, payload):
        if message_type is NetworkMessageType.GROUP_INVITATION:
            _emit("GROUP_INVITATION_ACCEPTED", payload=payload)
            return NetworkMessageAction.ACCEPT
        if message_type is NetworkMessageType.GROUP_CONFIG:
            _emit(
                "GROUP_CONFIG_APPLIED",
                group_id=payload.get("group_id"),
                payload=payload,
            )
            return NetworkMessageAction.ACK
        _emit("UNKNOWN_NETWORK_MESSAGE_REJECTED", payload=payload)
        return NetworkMessageAction.REJECT


class AgentBGroupListener:
    def __init__(
        self, session_notifications: asyncio.Queue[ComputeNotification]
    ) -> None:
        self.session_notifications = session_notifications
        self.last_message: dict[str, Any] | None = None
        self._queued_notifications: set[tuple[str, str, str]] = set()

    async def on_group_message(self, group_id, sender_agent_id, payload):
        self.last_message = {
            "group_id": group_id,
            "sender_agent_id": sender_agent_id,
            "payload": dict(payload),
        }
        _emit("B_MESSAGE_RECEIVED", **self.last_message)
        if COMPUTE_SESSION_ID_FIELD not in payload:
            return
        session_id = payload.get(COMPUTE_SESSION_ID_FIELD)
        if not isinstance(session_id, str) or not session_id:
            _emit(
                "COMPUTING_SESSION_NOTIFICATION_REJECTED",
                group_id=group_id,
                sender_agent_id=sender_agent_id,
                cause="missing-or-invalid-compute-service-session-id",
            )
            return
        key = (group_id, sender_agent_id, session_id)
        if key in self._queued_notifications:
            _emit(
                "COMPUTING_SESSION_NOTIFICATION_DUPLICATE",
                group_id=group_id,
                sender_agent_id=sender_agent_id,
                compute_service_session_id=session_id,
            )
            return
        self._queued_notifications.add(key)
        self.session_notifications.put_nowait(
            ComputeNotification(group_id, sender_agent_id, session_id)
        )
        _emit(
            "COMPUTING_SESSION_NOTIFICATION_QUEUED",
            group_id=group_id,
            sender_agent_id=sender_agent_id,
            compute_service_session_id=session_id,
        )


async def _next_notification(
    notifications: asyncio.Queue[ComputeNotification],
    *,
    stop_event: asyncio.Event | None,
    timeout: float,
) -> ComputeNotification | None:
    if stop_event is not None and stop_event.is_set():
        return None
    notification_task = asyncio.create_task(notifications.get())
    stop_task = (
        asyncio.create_task(stop_event.wait()) if stop_event is not None else None
    )
    waiters = {notification_task}
    if stop_task is not None:
        waiters.add(stop_task)
    try:
        done, _ = await asyncio.wait(
            waiters,
            timeout=timeout if timeout > 0 else None,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise TimeoutError("timed out waiting for a computing session notification")
        if notification_task in done:
            return notification_task.result()
        return None
    finally:
        for task in waiters:
            if not task.done():
                task.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


async def _wait_for_upload_close(
    upload: Any,
    *,
    stop_event: asyncio.Event | None,
    timeout: float,
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout if timeout > 0 else None
    while upload.state != "STOPPED":
        if stop_event is not None and stop_event.is_set():
            return False
        if deadline is not None and asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("timed out waiting for COMPUTE_SESSION_CLOSE (C-05)")
        await asyncio.sleep(0.2)
    return True


async def run_agent_b(
    args: argparse.Namespace,
    *,
    sdk: AgentSdk | None = None,
    gate: EnterStepGate | None = None,
    stop_event: asyncio.Event | None = None,
) -> dict[str, Any]:
    video_file: Path | None = None
    if sdk is not None:
        client = sdk
    elif args.video_source == "file":
        from agent_sdk.webrtc import AiortcMediaOffloadAdapter

        video_file = Path(args.video_file).expanduser().resolve()
        if not video_file.is_file():
            raise RuntimeError(f"local test video does not exist: {video_file}")
        client = AgentSdk(
            _media_offload_adapter=AiortcMediaOffloadAdapter(
                video_file_path=video_file,
                loop_video_file=args.loop_video,
            )
        )
    else:
        client = AgentSdk()
    session_notifications: asyncio.Queue[ComputeNotification] = asyncio.Queue()
    message_listener = AgentBGroupListener(session_notifications)
    unregister_network = lambda: None
    unregister_group = lambda: None
    profile = None
    active_upload = None
    completed_sessions: list[str] = []

    try:
        await _before_step(
            gate,
            "sdk.register_network_message_listener",
            "注册建组邀请和群组配置监听器；邀请到达时自动 ACCEPT。",
        )
        unregister_network = client.register_network_message_listener(
            AgentBNetworkListener()
        )
        await _before_step(
            gate,
            "sdk.register_group_message_listener",
            "注册 A2A 消息监听器；收到 A 的消息时打印 B_MESSAGE_RECEIVED。",
        )
        unregister_group = client.register_group_message_listener(
            message_listener
        )

        await _before_step(
            gate,
            "sdk.init",
            "GET /v1/ue/info，并建立 WebSocket、TUN、MASQUE 和本地消息服务。",
        )
        initialized = await client.init(
            args.runtime_ip,
            args.runtime_port,
            local_tcp_port=args.tcp_port,
            local_udp_port=args.udp_port,
            masque_server_url=args.masque_url,
            masque_authorization=(
                f"Bearer {args.masque_token}" if args.masque_token else None
            ),
            tun_name=args.tun_name,
            tun_mtu=args.tun_mtu,
            log_file_path=args.log_file,
            log_level=args.log_level,
        )
        _emit(
            "SDK_INITIALIZED",
            agent_tun_cidr=initialized.agent_tun_cidr,
            agent_tcp_endpoint=initialized.agent_tcp_endpoint,
            masque_endpoint=initialized.masque_proxy_endpoint,
        )

        lifecycle_state = client.agent_lifecycle_state
        profile = client.local_profile
        _emit(
            "AGENT_STATE_RESTORED",
            agent_lifecycle_state=lifecycle_state.value,
            agent_id=profile.agent_id if profile else None,
        )
        if args.force_registration:
            previous_agent_id = profile.agent_id if profile else None
            await _before_step(
                gate,
                "sdk.reset_agent",
                "Force 模式：只清除本地 Profile/Card 状态，不向网侧发送去注册请求。",
            )
            reset = await client.reset_agent()
            if not reset.success:
                raise RuntimeError(
                    f"Agent B local state reset failed: {reset.message}"
                )
            _emit(
                "LOCAL_AGENT_STATE_RESET",
                previous_agent_id=previous_agent_id,
                network_deregistration_sent=False,
            )
            lifecycle_state = AgentLifecycleState.NO_IDENTITY
            profile = None
        elif (
            args.fresh_registration
            and lifecycle_state is not AgentLifecycleState.NO_IDENTITY
        ):
            if profile is None:
                raise RuntimeError("persisted Agent state has no local profile")
            previous_agent_id = profile.agent_id
            await _before_step(
                gate,
                "sdk.deregister_identity",
                "Fresh 模式：先注销上次测试遗留身份，再从状态1重新注册。",
            )
            deregistered = await client.deregister_identity(
                previous_agent_id, reason="replaced"
            )
            if not deregistered.success:
                raise RuntimeError(
                    "Agent B previous identity deregistration failed: "
                    f"{deregistered.message}"
                )
            _emit(
                "PREVIOUS_IDENTITY_DEREGISTERED",
                agent_id=previous_agent_id,
            )
            lifecycle_state = AgentLifecycleState.NO_IDENTITY
            profile = None

        if lifecycle_state is AgentLifecycleState.NO_IDENTITY:
            await _before_step(
                gate,
                "sdk.apply_identity",
                "状态1：POST /idm/v1/identity-applications，为 Agent B 申请数字身份。",
            )
            profile = await client.apply_identity(
                owner=args.owner,
                name=args.agent_name,
                description=args.description,
                metadata={
                    "region": args.region,
                    "os": "Linux",
                    "version": "0.17.5",
                },
            )
            lifecycle_state = AgentLifecycleState.IDENTITY_READY
            _emit("IDENTITY_READY", agent_id=profile.agent_id)
        else:
            if profile is None:
                raise RuntimeError("persisted Agent state has no local profile")
            _emit("IDENTITY_REUSED", agent_id=profile.agent_id)

        if lifecycle_state is AgentLifecycleState.IDENTITY_READY:
            await _before_step(
                gate,
                "sdk.get_network_ability",
                "状态2：POST /idm/v1/network-ability，取得运营商网络能力凭证。",
            )
            ability = await client.get_network_ability(profile.agent_id)
            _emit(
                "NETWORK_ABILITY_READY",
                network_abilities=ability.abilities,
                valid_until=(
                    ability.valid_until.isoformat() if ability.valid_until else None
                ),
            )

            await _before_step(
                gate,
                "sdk.register_capabilities",
                "状态2：POST /arf/v1/agent-cards，发布供 Agent A 发现的 B 能力。",
            )
            registration = await client.register_capabilities(
                profile.agent_id,
                priority=args.priority,
                credentials=[ability.ability_vc],
                capabilities=[args.capability],
                test_vc_private_key_path=args.third_party_private_key,
            )
            if not registration.success:
                raise RuntimeError(
                    f"Agent B capability registration failed: {registration.message}"
                )
            lifecycle_state = AgentLifecycleState.CARD_PUBLISHED
            _emit("PROFILE_REGISTERED")
        else:
            _emit(
                "PROFILE_REUSED",
                agent_id=profile.agent_id,
                reason="Agent Card is already published; skip register_capabilities",
            )
        _emit(
            "B_READY",
            agent_id=profile.agent_id,
            capability=args.capability,
            video_source=args.video_source,
            video_file=str(video_file) if video_file is not None else None,
            agent_tun_cidr=initialized.agent_tun_cidr,
            listen_endpoint=initialized.agent_tcp_endpoint,
        )
        print(
            "Agent B 已就绪：现在启动 Agent A；B 会自动接受邀请，收到 session ID "
            "后上传摄像头视频。",
            flush=True,
        )

        while args.max_sessions == 0 or len(completed_sessions) < args.max_sessions:
            notification = await _next_notification(
                session_notifications,
                stop_event=stop_event,
                timeout=args.wait_timeout,
            )
            if notification is None:
                _emit("STOP_EVENT_RECEIVED")
                break
            snapshot = await client.get_group_snapshot(notification.group_id)
            if snapshot is None:
                raise RuntimeError(
                    "computing session notification references an unknown group: "
                    f"{notification.group_id}"
                )
            if notification.sender_agent_id not in snapshot.members_by_agent_id:
                raise RuntimeError(
                    "computing session notification sender is not in the group: "
                    f"{notification.sender_agent_id}"
                )
            if profile.agent_id not in snapshot.members_by_agent_id:
                raise RuntimeError(
                    "local Agent B is absent from the computing notification group: "
                    f"{profile.agent_id}"
                )

            session_id = notification.compute_service_session_id
            await _before_step(
                gate,
                "sdk.start_video_upload",
                "等待 SDK 内部 producer C-02，并用缓存的 Sandbox 端点完成 WebRTC "
                f"协商；session_id={session_id}。",
            )
            active_upload = await client.start_video_upload(
                session_id,
                camera_id=args.camera_id,
                width=args.video_width,
                height=args.video_height,
                fps=args.video_fps,
                bitrate_kbps=args.video_bitrate_kbps,
                timeout_seconds=args.media_timeout,
            )
            _emit(
                "VIDEO_UPLOAD_STARTED",
                compute_service_session_id=session_id,
                track_id=active_upload.track_id,
                state=active_upload.state,
                video_source=args.video_source,
                video_file=str(video_file) if video_file is not None else None,
                camera_id=args.camera_id,
                width=args.video_width,
                height=args.video_height,
                fps=args.video_fps,
                bitrate_kbps=args.video_bitrate_kbps,
            )

            remotely_closed = await _wait_for_upload_close(
                active_upload,
                stop_event=stop_event,
                timeout=args.session_close_timeout,
            )
            if not remotely_closed:
                _emit(
                    "VIDEO_UPLOAD_STOP_REQUESTED",
                    compute_service_session_id=session_id,
                )
                break
            completed_sessions.append(session_id)
            _emit(
                "COMPUTING_SESSION_CLOSED",
                compute_service_session_id=session_id,
                completed_session_count=len(completed_sessions),
            )
            active_upload = None

        return {
            "agent_id": profile.agent_id,
            "capability": args.capability,
            "last_message": message_listener.last_message,
            "completed_sessions": completed_sessions,
        }
    finally:
        unregister_group()
        unregister_network()
        try:
            if active_upload is not None and active_upload.state != "STOPPED":
                try:
                    await active_upload.stop()
                    _emit(
                        "VIDEO_UPLOAD_STOPPED_DURING_CLEANUP",
                        track_id=active_upload.track_id,
                    )
                except Exception as exc:
                    _emit(
                        "VIDEO_UPLOAD_CLEANUP_FAILED",
                        error=str(exc) or repr(exc),
                    )
            if args.deregister_on_exit and profile is not None:
                deregistered = await client.deregister_identity(
                    profile.agent_id, reason="retired"
                )
                if not deregistered.success:
                    raise RuntimeError(
                        "Agent B identity deregistration failed: "
                        f"{deregistered.message}"
                    )
                _emit("IDENTITY_DEREGISTERED", agent_id=profile.agent_id)
        finally:
            await client.close()
            _emit("SDK_CLOSED")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Agent B publishes a capability, accepts Agent A's group and uploads "
            "local video for a computing session."
        )
    )
    value.add_argument("--runtime-ip", required=True)
    value.add_argument("--runtime-port", type=int, default=8080)
    value.add_argument("--tcp-port", type=int, default=4001)
    value.add_argument("--udp-port", type=int, default=28443)
    value.add_argument("--masque-url", required=True)
    value.add_argument("--masque-token")
    value.add_argument("--tun-name", default="agent_tun_b")
    value.add_argument("--tun-mtu", type=int, default=1280)
    value.add_argument("--agent-name", default="Agent-B")
    value.add_argument("--owner", default="ab-test-owner-b")
    value.add_argument("--description", default="Agent B video offload producer test")
    value.add_argument("--region", default="CN")
    value.add_argument("--capability", default="video_rendering")
    value.add_argument("--priority", type=int, default=1)
    value.add_argument(
        "--third-party-private-key",
        default=None,
        help=(
            "optional lab issuer private-key override; by default use the "
            "private key packaged in the SDK"
        ),
    )
    value.add_argument("--log-file", default="./logs/agent-b-test.log")
    value.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default="INFO",
    )
    value.add_argument(
        "--prompt",
        action="store_true",
        help="wait for Enter before each outbound SDK setup operation",
    )
    value.add_argument(
        "--wait-timeout",
        type=float,
        default=0,
        help="seconds to wait for each computing session ID; 0 waits indefinitely",
    )
    value.add_argument(
        "--video-source",
        choices=("file", "camera"),
        default="file",
        help="use the bundled local MP4 by default, or a Linux V4L2 camera",
    )
    value.add_argument(
        "--video-file",
        default=str(DEFAULT_TEST_VIDEO),
        help="local video used when --video-source=file",
    )
    value.add_argument(
        "--loop-video",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="loop the local video until the computing session is closed",
    )
    value.add_argument("--camera-id", type=int, default=0)
    value.add_argument("--video-width", type=int, default=1280)
    value.add_argument("--video-height", type=int, default=720)
    value.add_argument("--video-fps", type=int, default=30)
    value.add_argument("--video-bitrate-kbps", type=int, default=2500)
    value.add_argument("--media-timeout", type=float, default=30.0)
    value.add_argument(
        "--session-close-timeout",
        type=float,
        default=60.0,
        help="seconds to wait for C-05 after upload starts; 0 waits indefinitely",
    )
    value.add_argument(
        "--max-sessions",
        type=_non_negative_int,
        default=1,
        help="number of remotely closed sessions required for success; 0 keeps serving",
    )
    registration_mode = value.add_mutually_exclusive_group()
    registration_mode.add_argument(
        "--fresh-registration",
        action="store_true",
        help=(
            "deregister any persisted identity during startup, then always "
            "apply and publish a new identity"
        ),
    )
    registration_mode.add_argument(
        "--force-registration",
        action="store_true",
        help=(
            "clear the persisted local Profile/Card state without network "
            "deregistration, then apply and publish a new identity"
        ),
    )
    value.add_argument("--deregister-on-exit", action="store_true")
    return value


async def main(args: argparse.Namespace) -> None:
    _emit(
        "TEST_STARTING",
        interactive=args.prompt,
        fresh_registration=args.fresh_registration,
        force_registration=args.force_registration,
        deregister_on_exit=args.deregister_on_exit,
    )
    gate = EnterStepGate() if args.prompt else None
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    terminate_requested = False

    def request_termination() -> None:
        nonlocal terminate_requested
        terminate_requested = True
        if task is not None:
            task.cancel()

    signal_handler_installed = False
    try:
        loop.add_signal_handler(signal.SIGTERM, request_termination)
        signal_handler_installed = True
        await run_agent_b(args, gate=gate)
    except asyncio.CancelledError:
        if not terminate_requested:
            raise
        _emit("TEST_TERMINATED", signal="SIGTERM")
    finally:
        if signal_handler_installed:
            loop.remove_signal_handler(signal.SIGTERM)


if __name__ == "__main__":
    arguments = parser().parse_args()
    try:
        asyncio.run(main(arguments))
    except InteractiveDemoAborted as exc:
        print(f"[已终止] {exc}")
    except KeyboardInterrupt:
        print("[已终止] 收到 Ctrl+C")
    except Exception as exc:
        _emit(
            "TEST_FAILED",
            error_type=type(exc).__name__,
            error=str(exc) or repr(exc),
            error_code=getattr(getattr(exc, "code", None), "value", None),
            runtime=f"http://{arguments.runtime_ip}:{arguments.runtime_port}",
            masque_url=arguments.masque_url,
            sdk_log_file=arguments.log_file,
        )
        raise
