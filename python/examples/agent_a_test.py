"""Agent A: discover Agent B by capability, create a group, and send a message.

Outbound SDK operations run continuously by default. Use ``--prompt`` when
manual step-by-step execution is needed. AgentRuntime downlink callbacks are
always handled immediately so grouping is not blocked by terminal input.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from typing import Any

from agent_sdk import (
    AgentLifecycleState,
    AgentSdk,
    NetworkMessageAction,
    NetworkMessageType,
)
from interactive_linux_agent import EnterStepGate, InteractiveDemoAborted


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
                    "version": "0.15.1",
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
            if not item.skills or args.target_capability in item.skills
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

        if args.deregister_on_exit:
            await _before_step(
                gate,
                "sdk.deregister_identity",
                "POST /acn-agent/v1/agent-deletions，注销 Agent A 测试身份。",
            )
            await client.deregister_identity(profile.agent_id, reason="retired")
            _emit("IDENTITY_DEREGISTERED", agent_id=profile.agent_id)

        completed = True
        return {
            "agent_id": profile.agent_id,
            "target_agent_id": target.agent_id,
            "group_id": group.group_id,
            "message_id": receipt.message_id,
        }
    finally:
        unregister_group()
        unregister_network()
        try:
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
            "Agent A discovers B by capability, creates a group, and sends a message."
        )
    )
    value.add_argument("--runtime-ip", required=True)
    value.add_argument("--runtime-port", type=int, default=8080)
    value.add_argument("--local-vlan-ip", required=True)
    value.add_argument("--tcp-port", type=int, default=4001)
    value.add_argument("--udp-port", type=int, default=28443)
    value.add_argument("--masque-url", required=True)
    value.add_argument("--masque-token")
    value.add_argument("--tun-name", default="agent_tun_a")
    value.add_argument("--tun-mtu", type=int, default=1280)
    value.add_argument("--agent-name", default="Agent-A")
    value.add_argument("--owner", default="ab-test-owner-a")
    value.add_argument("--description", default="Agent A capability discovery test")
    value.add_argument("--region", default="CN")
    value.add_argument("--target-capability", default="text")
    value.add_argument("--target-agent-id")
    value.add_argument("--priority", type=int, default=1)
    value.add_argument("--task-id", default="agent-a-to-b-test")
    value.add_argument(
        "--task-description", default="discover a text-capable Agent B"
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
    value.add_argument("--deregister-on-exit", action="store_true")
    return value


async def main(args: argparse.Namespace) -> None:
    _emit("TEST_STARTING", interactive=args.prompt)
    gate = EnterStepGate() if args.prompt else None
    await run_agent_a(args, gate=gate)


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
            local_vlan_ip=arguments.local_vlan_ip,
            sdk_log_file=arguments.log_file,
        )
        raise
