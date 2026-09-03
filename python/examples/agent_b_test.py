"""Agent B: publish a capability, accept Agent A's group, and receive messages.

Outbound SDK operations run continuously by default. Use ``--prompt`` for
manual stepping. Network-initiated group messages are handled immediately; an
invitation is accepted without prompting.
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
            {"role": "B", "event": event, **fields},
            ensure_ascii=False,
            default=str,
        ),
        flush=True,
    )


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
    def __init__(self, message_event: asyncio.Event) -> None:
        self.message_event = message_event
        self.last_message: dict[str, Any] | None = None

    async def on_group_message(self, group_id, sender_agent_id, payload):
        self.last_message = {
            "group_id": group_id,
            "sender_agent_id": sender_agent_id,
            "payload": dict(payload),
        }
        _emit("B_MESSAGE_RECEIVED", **self.last_message)
        self.message_event.set()


async def run_agent_b(
    args: argparse.Namespace,
    *,
    sdk: AgentSdk | None = None,
    gate: EnterStepGate | None = None,
    stop_event: asyncio.Event | None = None,
) -> dict[str, Any]:
    client = sdk or AgentSdk()
    message_event = stop_event or asyncio.Event()
    message_listener = AgentBGroupListener(message_event)
    unregister_network = lambda: None
    unregister_group = lambda: None
    profile = None

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
                "状态1：POST /idm/v1/identity-applications，为 Agent B 申请数字身份。",
            )
            profile = await client.apply_identity(
                owner=args.owner,
                name=args.agent_name,
                description=args.description,
                metadata={
                    "region": args.region,
                    "os": "Linux",
                    "version": "0.17.0",
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
            agent_tun_cidr=initialized.agent_tun_cidr,
            listen_endpoint=initialized.agent_tcp_endpoint,
        )
        print(
            "Agent B 已就绪：现在启动 Agent A，B 会自动接受邀请并打印收到的消息。",
            flush=True,
        )

        if args.wait_timeout > 0:
            await asyncio.wait_for(
                message_event.wait(), timeout=args.wait_timeout
            )
        else:
            await message_event.wait()

        if args.exit_after_message:
            _emit("EXIT_AFTER_MESSAGE")
        else:
            _emit("FIRST_MESSAGE_RECEIVED_KEEP_RUNNING")
            await asyncio.Event().wait()

        return {
            "agent_id": profile.agent_id,
            "capability": args.capability,
            "last_message": message_listener.last_message,
        }
    finally:
        unregister_group()
        unregister_network()
        try:
            if args.deregister_on_exit and profile is not None:
                await client.deregister_identity(profile.agent_id, reason="retired")
                _emit("IDENTITY_DEREGISTERED", agent_id=profile.agent_id)
        finally:
            await client.close()
            _emit("SDK_CLOSED")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Agent B publishes a capability, accepts Agent A's group, and receives a message."
        )
    )
    value.add_argument("--runtime-ip", required=True)
    value.add_argument("--runtime-port", type=int, default=8080)
    value.add_argument("--local-vlan-ip", required=True)
    value.add_argument("--tcp-port", type=int, default=4001)
    value.add_argument("--udp-port", type=int, default=28443)
    value.add_argument("--masque-url", required=True)
    value.add_argument("--masque-token")
    value.add_argument("--tun-name", default="agent_tun_b")
    value.add_argument("--tun-mtu", type=int, default=1280)
    value.add_argument("--agent-name", default="Agent-B")
    value.add_argument("--owner", default="ab-test-owner-b")
    value.add_argument("--description", default="Agent B capability provider test")
    value.add_argument("--region", default="CN")
    value.add_argument("--capability", default="text")
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
        "--exit-after-message",
        action="store_true",
        help="close Agent B after the first received A2A message",
    )
    value.add_argument(
        "--wait-timeout",
        type=float,
        default=0,
        help="seconds to wait for the first message; 0 waits indefinitely",
    )
    value.add_argument("--deregister-on-exit", action="store_true")
    return value


async def main(args: argparse.Namespace) -> None:
    _emit("TEST_STARTING", interactive=args.prompt)
    gate = EnterStepGate() if args.prompt else None
    await run_agent_b(args, gate=gate)


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
