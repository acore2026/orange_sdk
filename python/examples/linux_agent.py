from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

from agent_sdk import AgentSdk, NetworkMessageAction, NetworkMessageType
from agent_sdk.security import (
    DemoAcceptAllProofVerifier,
    DemoControlRequestAuthenticator,
    DemoMessageSignatureVerifier,
    DemoMessageSigner,
)


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


async def run_full_flow(sdk: AgentSdk, args) -> None:
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

    profile = await sdk.apply_identity(
        owner=args.owner,
        name=args.agent_name,
        public_key=args.identity_public_key,
        description=args.description,
        metadata={"platform": "Linux", "example": "linux_agent.py"},
    )
    print("[2 apply_identity]", profile.agent_id)
    if args.agent_id and profile.agent_id != args.agent_id:
        raise RuntimeError(
            f"AgentRuntime returned {profile.agent_id}, expected {args.agent_id}"
        )

    # Normally used after loading a verified profile on process restart. Applying
    # the just-issued profile here keeps this full API example executable.
    sdk.set_local_profile_for_restore(profile)
    print("[3 set_local_profile_for_restore]", profile.agent_id)

    ability = await sdk.get_network_ability(profile.agent_id)
    print("[4 get_network_ability]", ability.abilities)

    registered = await sdk.register_capabilities(
        profile.agent_id,
        priority=args.priority,
        credentials=[profile.identity_vc, ability.ability_vc],
    )
    print("[5 register_capabilities]", registered.success)

    ability_vc_id = ability.ability_vc.get("id")
    if not isinstance(ability_vc_id, str) or not ability_vc_id:
        raise RuntimeError("network ability vc1 does not contain a non-empty id")
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

    discovered = await sdk.discover_agents(
        task_id=args.task_id,
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

    group = await sdk.create_group(
        profile.agent_id,
        [target_agent_id],
        group_name=args.group_name,
        scope=args.group_scope,
        max_members=args.max_members,
    )
    print("[8 create_group]", group.group_id)

    snapshot = await _wait_for_group(sdk, group.group_id, args.group_timeout)
    if target_agent_id not in snapshot.members_by_agent_id:
        raise RuntimeError(
            f"target {target_agent_id} is absent from group {group.group_id}"
        )
    print("[9 get_group_snapshot]", snapshot.generation)

    receipt = await sdk.send_message(
        group.group_id,
        target_agent_id,
        args.message,
        timeout_seconds=args.message_timeout,
    )
    print("[10 send_message]", receipt.message_id, receipt.delivered)

    session = await sdk.create_offloading_session(
        profile.agent_id,
        task_type=args.offloading_task_type,
        sandbox_id=args.sandbox_id,
        timeout_seconds=args.offloading_timeout,
    )
    print("[11 create_offloading_session]", session.session_id, session.state)

    upload = await sdk.start_video_upload(
        session.session_id,
        camera_id=args.camera_id,
        width=args.video_width,
        height=args.video_height,
        fps=args.video_fps,
        bitrate_kbps=args.video_bitrate_kbps,
    )
    await upload.pause()
    await upload.resume()
    print("[12 start_video_upload]", upload.track_id, upload.state)

    stream = await sdk.get_processed_video_stream(
        session.session_id, timeout_seconds=args.processed_stream_timeout
    )
    frame = await stream.recv()
    await upload.stop()
    print("[13 get_processed_video_stream]", frame, upload.state)

    if args.stay_running:
        print("[14 stay_running] press Ctrl+C to close the SDK")
        await asyncio.Event().wait()

    if not args.keep_identity:
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
    print(
        "WARNING: Demo proof/signature and media implementations are for API "
        "integration only; replace them before production."
    )
    sdk = AgentSdk(
        proof_verifier=DemoAcceptAllProofVerifier(),
        control_request_authenticator=DemoControlRequestAuthenticator(),
        message_signer=DemoMessageSigner(),
        message_signature_verifier=DemoMessageSignatureVerifier(),
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
    value.add_argument("--identity-public-key", required=True)
    value.add_argument("--description", default="Linux Agent SDK full-flow example")
    value.add_argument("--masque-url", required=True)
    value.add_argument("--masque-token")
    value.add_argument("--tun-name", default="agent_tun0")
    value.add_argument("--tun-mtu", type=int, default=1280)
    value.add_argument("--priority", type=int, default=1)
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
    value.add_argument("--offloading-task-type", default="video_rendering")
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
    value.add_argument("--deregister-reason", default="retired by linux_agent.py")
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
