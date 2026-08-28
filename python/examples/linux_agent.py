from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

from agent_sdk import AgentSdk, NetworkMessageAction, NetworkMessageType


StepHook = Callable[[str, str], Awaitable[None]]


async def _before_step(
    hook: StepHook | None, interface_name: str, description: str
) -> None:
    if hook is not None:
        await hook(interface_name, description)


class NetworkListener:
    async def on_network_message(self, message_type, payload):
        if message_type is NetworkMessageType.GROUP_INVITATION:
            print("[callback] group invitation:", payload)
            return NetworkMessageAction.ACCEPT
        if message_type is NetworkMessageType.GROUP_CONFIG:
            print("[callback] group config committed:", payload["group_id"])
            return NetworkMessageAction.ACK
        return NetworkMessageAction.REJECT


class GroupListener:
    async def on_group_message(self, group_id, sender_agent_id, payload):
        print(f"[callback] A2A {group_id=} {sender_agent_id=}: {payload}")


class ExampleVideoUploadHandle:
    """Example-only media handle; replace with the platform WebRTC adapter."""

    def __init__(self) -> None:
        self.track_id = "linux-example-video-track"
        self.state = "RUNNING"

    async def pause(self) -> None:
        self.state = "PAUSED"

    async def resume(self) -> None:
        self.state = "RUNNING"

    async def stop(self) -> None:
        self.state = "STOPPED"


class ExampleRemoteVideoStream(AsyncIterator[Any]):
    def __init__(self) -> None:
        self._delivered = False

    def __aiter__(self) -> "ExampleRemoteVideoStream":
        return self

    async def __anext__(self) -> Any:
        if self._delivered:
            raise StopAsyncIteration
        self._delivered = True
        return await self.recv()

    async def recv(self) -> Any:
        return {"example": True, "frame": "replace-with-real-decoded-frame"}


class ExampleMediaOffloadAdapter:
    """Exercises every media SDK call without claiming to upload camera data."""

    async def connect(self, session, signaling, timeout_seconds) -> None:
        del signaling, timeout_seconds
        print(f"[media] example adapter attached to {session.session_id}")

    async def start_video_upload(
        self,
        session,
        *,
        camera_id,
        width,
        height,
        fps,
        bitrate_kbps,
    ) -> ExampleVideoUploadHandle:
        print(
            "[media] example upload:",
            session.session_id,
            camera_id,
            f"{width}x{height}@{fps}",
            f"{bitrate_kbps}kbps",
        )
        return ExampleVideoUploadHandle()

    async def get_processed_video_stream(
        self, session, timeout_seconds
    ) -> ExampleRemoteVideoStream:
        del timeout_seconds
        print(f"[media] example processed stream: {session.session_id}")
        return ExampleRemoteVideoStream()

    async def close(self) -> None:
        return None


def _message(value: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON message: {exc}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("message must be a JSON object")
    return parsed


async def _wait_for_group(sdk: AgentSdk, group_id: str, timeout: float):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        snapshot = await sdk.get_group_snapshot(group_id)
        if snapshot is not None:
            return snapshot
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(
                f"timed out waiting for acf_group_config for group {group_id}"
            )
        await asyncio.sleep(0.2)


async def run_full_flow(
    sdk: AgentSdk, args, *, before_step: StepHook | None = None
) -> None:
    await _before_step(
        before_step,
        "sdk.init",
        "GET /v1/ue/info，建立下行 WebSocket、Agent TUN、消息服务和 MASQUE；"
        f"Runtime=http://{args.runtime_ip}:{args.runtime_port}，"
        f"local_vlan_ip={args.local_vlan_ip}，MASQUE={args.masque_url}",
    )
    initialized = await sdk.init(
        args.runtime_ip,
        args.runtime_port,
        args.local_vlan_ip,
        args.tcp_port,
        args.udp_port,
        masque_server_url=args.masque_url,
        masque_authorization=(
            f"Bearer {args.masque_token}" if args.masque_token else None
        ),
        tun_name=args.tun_name,
        tun_mtu=args.tun_mtu,
        log_file_path=args.log_file,
        log_level=args.log_level,
        log_max_bytes=args.log_max_bytes,
        log_backup_count=args.log_backup_count,
    )
    print("[1 init]", initialized)

    await _before_step(
        before_step,
        "sdk.apply_identity",
        "POST /idm/v1/identity-applications 申请 Agent 数字身份和 vc0；"
        f"owner={args.owner!r}，name={args.agent_name!r}",
    )
    profile = await sdk.apply_identity(
        owner=args.owner,
        name=args.agent_name,
        description=args.description,
        metadata={
            "region": args.region,
            "os": "Linux",
            "version": "0.14.0",
        },
    )
    print("[2 apply_identity]", profile.agent_id)
    if args.agent_id and profile.agent_id != args.agent_id:
        raise RuntimeError(
            f"AgentRuntime returned {profile.agent_id}, expected {args.agent_id}"
        )

    # Normally used after loading a verified profile on process restart. Applying
    # the just-issued profile here keeps this full API example executable.
    await _before_step(
        before_step,
        "sdk.set_local_profile_for_restore",
        "将已验证的 AgentProfile 写入 SDK 本地状态；该步骤不发送 HTTP；"
        f"agent_id={profile.agent_id}",
    )
    sdk.set_local_profile_for_restore(profile)
    print("[3 set_local_profile_for_restore]", profile.agent_id)

    await _before_step(
        before_step,
        "sdk.get_network_ability",
        "POST /idm/v1/network-ability 获取运营商网络能力凭证 vc1；"
        f"agent_id={profile.agent_id}",
    )
    ability = await sdk.get_network_ability(profile.agent_id)
    print("[4 get_network_ability]", ability.abilities)

    await _before_step(
        before_step,
        "sdk.register_capabilities",
        "POST /arf/v1/agent-cards 发布 Agent 能力凭证；"
        f"priority={args.priority}，test_capabilities={args.test_capability or []}",
    )
    registered = await sdk.register_capabilities(
        profile.agent_id,
        priority=args.priority,
        credentials=[ability.ability_vc],
        capabilities=args.test_capability,
        test_vc_private_key_path=args.test_third_party_private_key,
    )
    print("[5 register_capabilities]", registered.success)

    ability_vc_id = ability.ability_vc.get("id")
    if not isinstance(ability_vc_id, str) or not ability_vc_id:
        raise RuntimeError("network ability vc1 does not contain a non-empty id")
    await _before_step(
        before_step,
        "sdk.update_capabilities",
        "POST /arf/v1/agent-cards-update 更新 Agent 能力属性；"
        f"update_skill={args.update_skill!r}",
    )
    updated = await sdk.update_capabilities(
        profile.agent_id,
        update_items=[
            {
                "update_type": "add_skill",
                "skill_name": args.update_skill,
                "reference_vc_id": ability_vc_id,
            }
        ],
        credentials=[ability.ability_vc],
    )
    print("[6 update_capabilities]", updated.success)

    await _before_step(
        before_step,
        "sdk.discover_agents",
        "POST /arf/v1/agent-discoveries 发现满足技能条件的 Agent；"
        f"required_skills={args.required_skill or ['text']}",
    )
    discovered = await sdk.discover_agents(
        agent_id=profile.agent_id,
        task_description=args.task_description,
        required_skills=args.required_skill or ["text"],
        discovery_scope=args.discovery_scope,
        max_results=args.max_results,
    )
    print("[7 discover_agents]", [item.agent_id for item in discovered])
    target_agent_id = args.target_agent_id
    if target_agent_id is None:
        if not discovered:
            raise RuntimeError(
                "Agent discovery returned no target; provide --target-agent-id or fix discovery"
            )
        target_agent_id = discovered[0].agent_id

    await _before_step(
        before_step,
        "sdk.create_group",
        "POST /acf/v1/agents-grouping 邀请目标 Agent 并创建群组；"
        f"target_agent_id={target_agent_id}，group_name={args.group_name!r}",
    )
    group = await sdk.create_group(
        profile.agent_id,
        [target_agent_id],
        group_name=args.group_name,
        scope=args.group_scope,
        max_members=args.max_members,
    )
    print("[8 create_group]", group.group_id)

    await _before_step(
        before_step,
        "sdk.get_group_snapshot",
        "等待 WebSocket 群组配置通知并读取 SDK 群组缓存；"
        f"group_id={group.group_id}，timeout={args.group_timeout}s",
    )
    snapshot = await _wait_for_group(sdk, group.group_id, args.group_timeout)
    if target_agent_id not in snapshot.members_by_agent_id:
        raise RuntimeError(
            f"target {target_agent_id} is absent from group {group.group_id}"
        )
    print("[9 get_group_snapshot]", snapshot.generation)

    await _before_step(
        before_step,
        "sdk.send_message",
        "从群组缓存解析目标 IP/端口并 POST /A2A/message；"
        f"group_id={group.group_id}，target_agent_id={target_agent_id}，"
        f"message={args.message}",
    )
    receipt = await sdk.send_message(
        group.group_id,
        target_agent_id,
        args.message,
        timeout_seconds=args.message_timeout,
        message_type=args.message_type,
        task_id=args.task_id,
    )
    print("[10 send_message]", receipt.message_id, receipt.delivered)

    await _before_step(
        before_step,
        "sdk.create_offloading_session",
        "POST /compute/v1/offloading-sessions 创建算力卸载会话；"
        f"workload_type={args.offloading_workload_type!r}，"
        f"sandbox_id={args.sandbox_id!r}",
    )
    session = await sdk.create_offloading_session(
        profile.agent_id,
        workload_type=args.offloading_workload_type,
        sandbox_id=args.sandbox_id,
        timeout_seconds=args.offloading_timeout,
    )
    print("[11 create_offloading_session]", session.session_id, session.state)

    await _before_step(
        before_step,
        "sdk.start_video_upload",
        "通过媒体适配器启动视频上传；"
        f"camera_id={args.camera_id}，{args.video_width}x{args.video_height}"
        f"@{args.video_fps}，bitrate={args.video_bitrate_kbps}kbps",
    )
    upload = await sdk.start_video_upload(
        session.session_id,
        camera_id=args.camera_id,
        width=args.video_width,
        height=args.video_height,
        fps=args.video_fps,
        bitrate_kbps=args.video_bitrate_kbps,
    )
    await _before_step(
        before_step,
        "upload.pause",
        "暂停当前视频上传句柄",
    )
    await upload.pause()
    print("[12a upload.pause]", upload.track_id, upload.state)
    await _before_step(
        before_step,
        "upload.resume",
        "恢复当前视频上传句柄",
    )
    await upload.resume()
    print("[12b upload.resume]", upload.track_id, upload.state)

    await _before_step(
        before_step,
        "sdk.get_processed_video_stream",
        "获取算力侧处理后的视频流；"
        f"session_id={session.session_id}，timeout={args.processed_stream_timeout}s",
    )
    stream = await sdk.get_processed_video_stream(
        session.session_id, timeout_seconds=args.processed_stream_timeout
    )
    await _before_step(
        before_step,
        "stream.recv",
        "从处理后视频流读取一帧",
    )
    frame = await stream.recv()
    print("[13a stream.recv]", frame)
    await _before_step(
        before_step,
        "upload.stop",
        "停止视频上传句柄",
    )
    await upload.stop()
    print("[13b upload.stop]", upload.track_id, upload.state)

    if args.stay_running:
        print("[14 stay_running] press Ctrl+C to close the SDK")
        await asyncio.Event().wait()

    if not args.keep_identity:
        await _before_step(
            before_step,
            "sdk.deregister_identity",
            "POST /acn-agent/v1/agent-deletions 注销本次申请的身份；"
            f"agent_id={profile.agent_id}，reason={args.deregister_reason}",
        )
        deregistered = await sdk.deregister_identity(
            profile.agent_id, reason=args.deregister_reason
        )
        print("[15 deregister_identity]", deregistered.success)
    else:
        print(
            "[15 deregister_identity] skipped by --keep-identity; retain the "
            "verified profile in secure storage before the next startup"
        )


async def main(args) -> None:
    print("SDK uses its persistent device key and embedded core-network public key.")
    sdk = AgentSdk(
        media_offload_adapter=ExampleMediaOffloadAdapter(),
    )
    unregister_network = sdk.register_network_message_listener(NetworkListener())
    unregister_group = sdk.register_group_message_listener(GroupListener())
    try:
        await run_full_flow(sdk, args)
    finally:
        unregister_group()
        unregister_network()
        await sdk.close()
        print("[16 close] SDK closed")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Run every Agent SDK northbound function against a real deployment."
    )
    value.add_argument("--runtime-ip", required=True)
    value.add_argument("--runtime-port", type=int, default=8080)
    value.add_argument("--local-vlan-ip", required=True)
    value.add_argument("--tcp-port", type=int, default=4001)
    value.add_argument("--udp-port", type=int, default=28443)
    value.add_argument("--agent-id", help="optional expected ID returned by apply_identity")
    value.add_argument("--agent-name", required=True)
    value.add_argument("--owner", required=True)
    value.add_argument("--region", default="CN")
    value.add_argument("--description", default="Linux Agent SDK full-flow example")
    value.add_argument("--masque-url", required=True)
    value.add_argument("--masque-token")
    value.add_argument("--tun-name", default="agent_tun0")
    value.add_argument("--tun-mtu", type=int, default=1280)
    value.add_argument("--priority", type=int, default=1)
    value.add_argument(
        "--test-capability",
        action="append",
        help=(
            "lab only: issue and publish one third-party VC for this raw "
            "capability; repeat the option for multiple capabilities"
        ),
    )
    value.add_argument(
        "--test-third-party-private-key",
        default=None,
        help=(
            "optional lab-only P-256 issuer private-key override; the SDK "
            "package resource is used by default"
        ),
    )
    value.add_argument("--update-skill", default="camera")
    value.add_argument("--task-id", default="linux-sdk-full-flow")
    value.add_argument("--task-description", default="find a text-capable peer")
    value.add_argument("--required-skill", action="append")
    value.add_argument("--discovery-scope", default="intra_plmn")
    value.add_argument("--max-results", type=int, default=10)
    value.add_argument("--target-agent-id")
    value.add_argument("--group-name", default="linux-sdk-example")
    value.add_argument("--group-scope", default="private")
    value.add_argument("--max-members", type=int, default=2)
    value.add_argument("--group-timeout", type=float, default=30.0)
    value.add_argument(
        "--message",
        type=_message,
        default={"type": "text", "content": "hello from linux_agent.py"},
    )
    value.add_argument("--message-timeout", type=float, default=5.0)
    value.add_argument("--message-type", default="application/json")
    value.add_argument("--offloading-workload-type", default="video_rendering")
    value.add_argument("--sandbox-id")
    value.add_argument("--offloading-timeout", type=float, default=30.0)
    value.add_argument("--camera-id", type=int, default=0)
    value.add_argument("--video-width", type=int, default=1280)
    value.add_argument("--video-height", type=int, default=720)
    value.add_argument("--video-fps", type=int, default=30)
    value.add_argument("--video-bitrate-kbps", type=int, default=2500)
    value.add_argument("--processed-stream-timeout", type=float, default=10.0)
    value.add_argument("--stay-running", action="store_true")
    value.add_argument(
        "--keep-identity",
        action="store_true",
        help="do not call deregister_identity for the identity created by this run",
    )
    value.add_argument(
        "--deregister-reason",
        choices=(
            "normal",
            "uninstalled",
            "replaced",
            "user_request",
            "security_event",
            "retired",
            "other",
        ),
        default="retired",
    )
    value.add_argument("--log-file", default="./logs/agent-sdk.log")
    value.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default="INFO",
    )
    value.add_argument("--log-max-bytes", type=int, default=10 * 1024 * 1024)
    value.add_argument("--log-backup-count", type=int, default=5)
    return value


if __name__ == "__main__":
    asyncio.run(main(parser().parse_args()))
