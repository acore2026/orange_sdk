"""Agent A: create an ACN group and run the consumer side of video offload.

Outbound SDK operations run continuously by default. Use ``--prompt`` when
manual step-by-step execution is needed. AgentRuntime downlink callbacks are
always handled immediately. Computing downlink is handled inside ``AgentSdk``;
this script only passes the session ID to Agent B and consumes processed frames.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import uuid
from collections.abc import Mapping
from typing import Any

from agent_sdk import (
    AcnContext,
    AgentLifecycleState,
    AgentSdk,
    ComputeConstraints,
    ComputeInputFormat,
    ComputeRequestType,
    ComputeResources,
    ComputeSessionRequest,
    NetworkMessageAction,
    NetworkMessageType,
)
from interactive_linux_agent import EnterStepGate, InteractiveDemoAborted


COMPUTE_REQUEST_MESSAGE_TYPE = "COMPUTE_SESSION_REQUEST"
COMPUTE_SESSION_MESSAGE_TYPE = "computing_video_session"
COMPUTE_SESSION_ID_FIELD = "compute_service_session_id"


def _emit(event: str, **fields: Any) -> None:
    print(
        json.dumps(
            {"role": "A", "event": event, **fields},
            ensure_ascii=False,
            default=str,
        ),
        flush=True,
    )


def _json_object(value: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON message: {exc}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("message must be a JSON object")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be greater than or equal to zero")
    return parsed


def _create_compute_request(
    args: argparse.Namespace,
    *,
    group_id: str,
    requester_agent_id: str,
    target_agent_id: str,
) -> ComputeSessionRequest:
    resources = ComputeResources(
        cpu_millicores=args.compute_cpu_millicores,
        memory_mib=args.compute_memory_mib,
        gpu_count=args.compute_gpu_count,
        gpu_model=args.compute_gpu_model,
    )
    return ComputeSessionRequest(
        message_type=COMPUTE_REQUEST_MESSAGE_TYPE,
        request_type=ComputeRequestType.CREATE,
        input_format=ComputeInputFormat.STRUCTURED,
        request_id=args.compute_request_id or str(uuid.uuid4()),
        acn_context=AcnContext(
            group_id=group_id,
            requester_agent_id=requester_agent_id,
            target_agent_id=target_agent_id,
        ),
        constraints=ComputeConstraints(
            capability_id=args.compute_capability_id,
            api_version=args.compute_api_version,
            image_id=args.compute_image_id,
            resources=resources,
            dnn=args.dnn,
            snssai=args.compute_snssai,
            allow_base_qos=args.allow_base_qos,
            max_duration_ms=args.compute_max_duration_ms,
            placement_region=args.compute_placement_region,
            data_residency_region=args.compute_data_residency_region,
        ),
        ui_locale=args.compute_ui_locale,
    )


def _compute_control_request(
    request_type: ComputeRequestType, compute_service_session_id: str
) -> ComputeSessionRequest:
    return ComputeSessionRequest(
        message_type=COMPUTE_REQUEST_MESSAGE_TYPE,
        request_type=request_type,
        input_format=ComputeInputFormat.STRUCTURED,
        request_id=str(uuid.uuid4()),
        compute_service_session_id=compute_service_session_id,
    )


def _compute_session_message(compute_service_session_id: str) -> Mapping[str, str]:
    return {COMPUTE_SESSION_ID_FIELD: compute_service_session_id}


def _frame_details(frame: Any) -> Mapping[str, Any]:
    details: dict[str, Any] = {"frame_type": type(frame).__name__}
    for field in ("width", "height", "pts", "time_base"):
        value = getattr(frame, field, None)
        if value is not None:
            details[field] = str(value) if field == "time_base" else value
    if isinstance(frame, (bytes, bytearray, memoryview)):
        details["size_bytes"] = len(frame)
    return details


async def _receive_processed_frames(
    stream: Any,
    *,
    frame_count: int,
    frame_timeout: float,
) -> int:
    received = 0
    while frame_count == 0 or received < frame_count:
        frame = await asyncio.wait_for(stream.recv(), timeout=frame_timeout)
        received += 1
        if received == 1 or received % 120 == 0 or received == frame_count:
            _emit(
                "PROCESSED_VIDEO_FRAME",
                frame_number=received,
                **_frame_details(frame),
            )
    return received


async def _before_step(
    gate: EnterStepGate | None, interface_name: str, description: str
) -> None:
    if gate is None:
        _emit("STEP_AUTO", interface=interface_name, description=description)
        return
    await gate(interface_name, description)


async def _wait_for_group(
    sdk: AgentSdk,
    group_id: str,
    target_agent_id: str,
    timeout_seconds: float,
):
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        snapshot = await sdk.get_group_snapshot(group_id)
        if (
            snapshot is not None
            and target_agent_id in snapshot.members_by_agent_id
        ):
            return snapshot
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(
                "timed out waiting for group configuration containing "
                f"target {target_agent_id} in group {group_id}"
            )
        await asyncio.sleep(0.2)


class AgentANetworkListener:
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


class AgentAGroupListener:
    async def on_group_message(self, group_id, sender_agent_id, payload):
        _emit(
            "A_MESSAGE_RECEIVED",
            group_id=group_id,
            sender_agent_id=sender_agent_id,
            payload=payload,
        )


async def run_agent_a(
    args: argparse.Namespace,
    *,
    sdk: AgentSdk | None = None,
    gate: EnterStepGate | None = None,
) -> dict[str, Any]:
    client = sdk or AgentSdk()
    unregister_network = lambda: None
    unregister_group = lambda: None
    profile = None
    completed = False
    compute_session_id: str | None = None
    processed_stream = None
    terminal_request_sent = False

    try:
        await _before_step(
            gate,
            "sdk.register_network_message_listener",
            "注册核心网邀请和群组配置监听器；不发送 HTTP。",
        )
        unregister_network = client.register_network_message_listener(
            AgentANetworkListener()
        )
        await _before_step(
            gate,
            "sdk.register_group_message_listener",
            "注册群组内 A2A 消息监听器；不发送 HTTP。",
        )
        unregister_group = client.register_group_message_listener(
            AgentAGroupListener()
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
        if (
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
                    "Agent A previous identity deregistration failed: "
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
                "状态1：POST /idm/v1/identity-applications，为 Agent A 申请数字身份。",
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
                "状态2：POST /arf/v1/agent-cards，发布 Agent A Profile。",
            )
            registration = await client.register_capabilities(
                profile.agent_id,
                priority=args.priority,
                credentials=[ability.ability_vc],
            )
            if not registration.success:
                raise RuntimeError(
                    f"Agent A Profile registration failed: {registration.message}"
                )
            lifecycle_state = AgentLifecycleState.CARD_PUBLISHED
            _emit("PROFILE_REGISTERED")
        else:
            _emit(
                "PROFILE_REUSED",
                agent_id=profile.agent_id,
                reason="Agent Card is already published; skip register_capabilities",
            )

        await _before_step(
            gate,
            "sdk.discover_agents",
            "POST /arf/v1/agent-discoveries，按目标能力发现 Agent B。",
        )
        discovered = await client.discover_agents(
            agent_id=profile.agent_id,
            task_description=args.task_description,
            required_skills=[args.target_capability],
            discovery_scope=args.discovery_scope,
            max_results=args.max_results,
        )
        _emit(
            "DISCOVERY_RESULT",
            required_capability=args.target_capability,
            agents=[
                {
                    "agent_id": item.agent_id,
                    "service_endpoints": item.service_endpoints,
                    "skills": list(item.skills),
                    "priority": item.priority,
                }
                for item in discovered
            ],
        )
        candidates = [
            item for item in discovered if item.agent_id != profile.agent_id
        ]
        if args.target_agent_id:
            candidates = [
                item
                for item in candidates
                if item.agent_id == args.target_agent_id
            ]
        candidates = [
            item
            for item in candidates
            if args.target_capability in item.skills
        ]
        if not candidates:
            expected = args.target_agent_id or args.target_capability
            raise RuntimeError(f"Agent B was not found for selector {expected!r}")
        target = candidates[0]
        _emit(
            "TARGET_B_SELECTED",
            agent_id=target.agent_id,
            service_endpoints=target.service_endpoints,
        )

        await _before_step(
            gate,
            "sdk.create_group",
            "POST /acf/v1/agents-grouping，邀请发现到的 Agent B 建组；"
            f"dnn={args.dnn!r}。",
        )
        group = await client.create_group(
            profile.agent_id,
            [target.agent_id],
            group_name=args.group_name,
            dnn=args.dnn,
            scope=args.group_scope,
            max_members=2,
        )
        _emit("GROUP_CREATED", group_id=group.group_id)

        await _before_step(
            gate,
            "sdk.get_group_snapshot",
            "等待 AgentRuntime 通过 WebSocket 下发群组配置，并读取 SDK 缓存。",
        )
        snapshot = await _wait_for_group(
            client,
            group.group_id,
            target.agent_id,
            args.group_timeout,
        )
        member = snapshot.members_by_agent_id[target.agent_id]
        _emit(
            "GROUP_CONFIG_READY",
            group_id=group.group_id,
            generation=snapshot.generation,
            target_agent_id=target.agent_id,
            target_agent_ip=member.agent_ip,
            target_service_endpoint=member.service_endpoint,
        )

        await _before_step(
            gate,
            "sdk.send_message",
            "SDK 根据 group_id 和 target_agent_id 从缓存解析 B 的 IP/端口，"
            "并 POST /A2A/message。",
        )
        receipt = await client.send_message(
            group.group_id,
            target.agent_id,
            args.message,
            timeout_seconds=args.message_timeout,
            message_type=args.message_type,
            task_id=args.task_id,
        )
        if not receipt.delivered:
            raise RuntimeError(
                f"message {receipt.message_id} was not accepted by Agent B"
            )
        _emit(
            "MESSAGE_DELIVERED",
            message_id=receipt.message_id,
            group_id=group.group_id,
            target_agent_id=target.agent_id,
        )

        create_request = _create_compute_request(
            args,
            group_id=group.group_id,
            requester_agent_id=profile.agent_id,
            target_agent_id=target.agent_id,
        )
        await _before_step(
            gate,
            "sdk.create_computing_session",
            "POST /v1/computing/session-requests 提交 CREATE；"
            f"group_id={group.group_id}，target_agent_id={target.agent_id}，"
            f"capability_id={args.compute_capability_id}。",
        )
        create_status = await client.create_computing_session(
            create_request,
            timeout_seconds=args.compute_timeout,
        )
        compute_session_id = create_status.compute_service_session_id
        if not compute_session_id:
            raise RuntimeError(
                "CREATE response does not contain compute_service_session_id: "
                f"status={create_status.status}, cause={create_status.cause}"
            )
        _emit(
            "COMPUTING_SESSION_CREATED",
            request_id=create_request.request_id,
            compute_service_session_id=compute_session_id,
            status=create_status.status,
            status_revision=create_status.status_revision,
            cause=create_status.cause,
        )

        await _before_step(
            gate,
            "sdk.send_message",
            "仅通过 A2A 向 B 发送 compute_service_session_id；"
            "Sandbox 地址、端口和接口路径不进入应用消息。",
        )
        compute_receipt = await client.send_message(
            group.group_id,
            target.agent_id,
            _compute_session_message(compute_session_id),
            timeout_seconds=args.message_timeout,
            message_type=COMPUTE_SESSION_MESSAGE_TYPE,
            task_id=f"computing:{compute_session_id}",
        )
        if not compute_receipt.delivered:
            raise RuntimeError(
                "compute_service_session_id was not accepted by Agent B: "
                f"message_id={compute_receipt.message_id}"
            )
        _emit(
            "COMPUTING_SESSION_NOTIFIED",
            message_id=compute_receipt.message_id,
            compute_service_session_id=compute_session_id,
        )

        if args.query_session:
            query_request = _compute_control_request(
                ComputeRequestType.QUERY, compute_session_id
            )
            await _before_step(
                gate,
                "sdk.query_computing_session",
                "POST /v1/computing/session-requests 查询正式算力会话状态。",
            )
            query_status = await client.query_computing_session(
                query_request,
                timeout_seconds=args.compute_timeout,
            )
            _emit(
                "COMPUTING_SESSION_QUERIED",
                request_id=query_request.request_id,
                compute_service_session_id=compute_session_id,
                status=query_status.status,
                status_revision=query_status.status_revision,
                cause=query_status.cause,
            )

        await _before_step(
            gate,
            "sdk.get_processed_video_stream",
            "等待 SDK 内部 consumer C-02，使用缓存的 Sandbox 端点完成 WebRTC 协商。",
        )
        processed_stream = await client.get_processed_video_stream(
            compute_session_id,
            timeout_seconds=args.media_timeout,
        )
        _emit(
            "PROCESSED_VIDEO_CONNECTED",
            compute_service_session_id=compute_session_id,
        )
        received_frames = await _receive_processed_frames(
            processed_stream,
            frame_count=args.frame_count,
            frame_timeout=args.frame_timeout,
        )
        _emit(
            "PROCESSED_VIDEO_VERIFIED",
            compute_service_session_id=compute_session_id,
            received_frames=received_frames,
        )

        terminal_status = None
        if args.terminal_action != "none":
            request_type = (
                ComputeRequestType.RELEASE
                if args.terminal_action == "release"
                else ComputeRequestType.CANCEL
            )
            terminal_request = _compute_control_request(
                request_type, compute_session_id
            )
            method_name = (
                "sdk.release_computing_session"
                if request_type is ComputeRequestType.RELEASE
                else "sdk.cancel_computing_session"
            )
            await _before_step(
                gate,
                method_name,
                "POST /v1/computing/session-requests 结束算力会话；"
                "后续 C-05 由 SDK 自动关闭媒体连接并回复 C-06。",
            )
            if request_type is ComputeRequestType.RELEASE:
                terminal_status = await client.release_computing_session(
                    terminal_request,
                    timeout_seconds=args.compute_timeout,
                )
            else:
                terminal_status = await client.cancel_computing_session(
                    terminal_request,
                    timeout_seconds=args.compute_timeout,
                )
            terminal_request_sent = True
            _emit(
                "COMPUTING_SESSION_TERMINATED",
                action=args.terminal_action,
                request_id=terminal_request.request_id,
                compute_service_session_id=compute_session_id,
                status=terminal_status.status,
                status_revision=terminal_status.status_revision,
                cause=terminal_status.cause,
            )

        completed = True
        return {
            "agent_id": profile.agent_id,
            "target_agent_id": target.agent_id,
            "group_id": group.group_id,
            "message_id": receipt.message_id,
            "compute_message_id": compute_receipt.message_id,
            "compute_service_session_id": compute_session_id,
            "received_frames": received_frames,
            "terminal_status": terminal_status,
        }
    finally:
        unregister_group()
        unregister_network()
        try:
            if (
                compute_session_id is not None
                and args.terminal_action != "none"
                and not terminal_request_sent
            ):
                try:
                    cleanup_request = _compute_control_request(
                        ComputeRequestType.RELEASE, compute_session_id
                    )
                    await client.release_computing_session(
                        cleanup_request,
                        timeout_seconds=args.compute_timeout,
                    )
                    _emit(
                        "COMPUTING_SESSION_RELEASED_DURING_CLEANUP",
                        compute_service_session_id=compute_session_id,
                    )
                except Exception as exc:
                    _emit(
                        "COMPUTING_SESSION_CLEANUP_FAILED",
                        compute_service_session_id=compute_session_id,
                        error=str(exc) or repr(exc),
                    )
            if processed_stream is not None:
                try:
                    await processed_stream.close()
                except Exception as exc:
                    _emit(
                        "PROCESSED_VIDEO_CLOSE_FAILED",
                        compute_service_session_id=compute_session_id,
                        error=str(exc) or repr(exc),
                    )
            if args.deregister_on_exit and profile is not None:
                deregistered = await client.deregister_identity(
                    profile.agent_id, reason="retired"
                )
                if not deregistered.success:
                    raise RuntimeError(
                        "Agent A identity deregistration failed: "
                        f"{deregistered.message}"
                    )
                _emit("IDENTITY_DEREGISTERED", agent_id=profile.agent_id)
            if completed:
                await _before_step(
                    gate,
                    "sdk.close",
                    "关闭 Agent A SDK，释放 WebSocket、MASQUE、TUN 和路由资源。",
                )
        finally:
            await client.close()
            _emit("SDK_CLOSED")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Agent A discovers B, creates an ACN group and verifies the consumer "
            "side of a computing video session."
        )
    )
    value.add_argument("--runtime-ip", required=True)
    value.add_argument("--runtime-port", type=int, default=8080)
    value.add_argument("--tcp-port", type=int, default=4001)
    value.add_argument("--udp-port", type=int, default=28443)
    value.add_argument("--masque-url", required=True)
    value.add_argument("--masque-token")
    value.add_argument("--tun-name", default="agent_tun_a")
    value.add_argument("--tun-mtu", type=int, default=1280)
    value.add_argument("--agent-name", default="Agent-A")
    value.add_argument("--owner", default="ab-test-owner-a")
    value.add_argument("--description", default="Agent A video offload consumer test")
    value.add_argument("--region", default="CN")
    value.add_argument("--target-capability", default="video_rendering")
    value.add_argument("--target-agent-id")
    value.add_argument("--priority", type=int, default=1)
    value.add_argument("--task-id", default="agent-a-to-b-test")
    value.add_argument(
        "--task-description", default="discover a video offload Agent B"
    )
    value.add_argument("--discovery-scope", default="intra_plmn")
    value.add_argument("--max-results", type=int, default=10)
    value.add_argument("--group-name", default="agent-a-b-test-group")
    value.add_argument("--dnn", default="internet")
    value.add_argument("--group-scope", default="private")
    value.add_argument("--group-timeout", type=float, default=60.0)
    value.add_argument(
        "--message",
        type=_json_object,
        default={"type": "text", "content": "hello Agent B from Agent A"},
    )
    value.add_argument("--message-type", default="text")
    value.add_argument("--message-timeout", type=float, default=10.0)
    value.add_argument("--compute-capability-id", default="video_rendering")
    value.add_argument("--compute-api-version")
    value.add_argument("--compute-image-id")
    value.add_argument("--compute-cpu-millicores", type=int, default=2000)
    value.add_argument("--compute-memory-mib", type=int, default=4096)
    value.add_argument("--compute-gpu-count", type=_non_negative_int)
    value.add_argument("--compute-gpu-model")
    value.add_argument("--compute-snssai")
    value.add_argument("--compute-max-duration-ms", type=_non_negative_int)
    value.add_argument("--compute-placement-region")
    value.add_argument("--compute-data-residency-region")
    value.add_argument("--compute-ui-locale")
    value.add_argument("--compute-request-id")
    value.add_argument("--compute-timeout", type=float, default=30.0)
    value.add_argument(
        "--allow-base-qos",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    value.add_argument(
        "--query-session",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="query the created session before opening the processed stream",
    )
    value.add_argument(
        "--terminal-action",
        choices=("release", "cancel", "none"),
        default="release",
        help="action sent after the requested number of processed frames",
    )
    value.add_argument("--media-timeout", type=float, default=30.0)
    value.add_argument(
        "--frame-count",
        type=_non_negative_int,
        default=1,
        help="processed frames required for success; 0 receives until interrupted",
    )
    value.add_argument("--frame-timeout", type=float, default=30.0)
    value.add_argument("--log-file", default="./logs/agent-a-test.log")
    value.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default="INFO",
    )
    value.add_argument(
        "--prompt",
        action="store_true",
        help="wait for Enter before each outbound SDK operation",
    )
    value.add_argument(
        "--fresh-registration",
        action="store_true",
        help=(
            "deregister any persisted identity during startup, then always "
            "apply and publish a new identity"
        ),
    )
    value.add_argument("--deregister-on-exit", action="store_true")
    return value


async def main(args: argparse.Namespace) -> None:
    _emit(
        "TEST_STARTING",
        interactive=args.prompt,
        fresh_registration=args.fresh_registration,
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
        await run_agent_a(args, gate=gate)
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
