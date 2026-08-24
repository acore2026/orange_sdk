from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from ipaddress import ip_interface
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Mapping

from agent_sdk import (
    AgentSdk,
    NetworkMessageAction,
    NetworkMessageType,
    __version__,
)


def _json_object(value: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON message: {exc}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("message must be a JSON object")
    return parsed


def _netns_id() -> int | None:
    try:
        return os.stat("/proc/self/ns/net").st_ino
    except OSError:
        return None


def _emit(logger: logging.Logger, role: str, event: str, **fields: Any) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "role": role,
        "event": event,
        **fields,
    }
    logger.info(json.dumps(record, ensure_ascii=False, default=str))


def _configure_app_logger(role: str, file_path: str) -> logging.Logger:
    destination = Path(file_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"masque_two_instance_test.{role}.{os.getpid()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    local_file = RotatingFileHandler(
        destination,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    local_file.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)
    logger.addHandler(local_file)
    return logger


class PairNetworkListener:
    def __init__(self, role: str, logger: logging.Logger) -> None:
        self._role = role
        self._logger = logger

    async def on_network_message(
        self, message_type: NetworkMessageType, payload: Mapping[str, Any]
    ) -> NetworkMessageAction:
        if message_type is NetworkMessageType.GROUP_INVITATION:
            _emit(
                self._logger,
                self._role,
                "GROUP_INVITATION_ACCEPTED",
                group_name=(payload.get("group_config") or {}).get("group_name")
                if isinstance(payload.get("group_config"), Mapping)
                else None,
            )
            return NetworkMessageAction.ACCEPT
        if message_type is NetworkMessageType.GROUP_CONFIG:
            _emit(
                self._logger,
                self._role,
                "GROUP_CONFIG_COMMITTED",
                group_id=payload.get("group_id"),
            )
            return NetworkMessageAction.ACK
        _emit(
            self._logger,
            self._role,
            "NETWORK_MESSAGE_REJECTED",
            message_type=str(message_type),
        )
        return NetworkMessageAction.REJECT


class PairGroupListener:
    def __init__(
        self,
        role: str,
        logger: logging.Logger,
        received_event: asyncio.Event,
    ) -> None:
        self._role = role
        self._logger = logger
        self._received_event = received_event
        self.last_message: tuple[str, str, Mapping[str, Any]] | None = None

    async def on_group_message(
        self,
        group_id: str,
        sender_agent_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.last_message = (group_id, sender_agent_id, dict(payload))
        _emit(
            self._logger,
            self._role,
            "A2A_MESSAGE_RECEIVED",
            group_id=group_id,
            sender_agent_id=sender_agent_id,
            payload=dict(payload),
        )
        self._received_event.set()


def _apply_role_defaults(args: argparse.Namespace) -> argparse.Namespace:
    role = args.role.upper()
    args.role = role
    suffix = role.lower()
    args.tcp_port = args.tcp_port or 4001
    args.udp_port = args.udp_port or 28443
    args.tun_name = args.tun_name or f"agent_tun_{suffix}"
    args.agent_name = args.agent_name or f"MASQUE-Test-Agent-{role}"
    args.owner = args.owner or f"masque-test-owner-{suffix}"
    args.state_dir = args.state_dir or f"./state/masque-pair-{suffix}"
    args.app_log_file = args.app_log_file or f"./logs/masque-pair-{suffix}-app.log"
    args.sdk_log_file = args.sdk_log_file or f"./logs/masque-pair-{suffix}-sdk.log"
    return args


def _validate_args(args: argparse.Namespace) -> None:
    if args.role == "A" and not args.target_agent_id:
        raise ValueError("role A requires --target-agent-id printed by role B")
    if args.group_timeout <= 0 or args.message_timeout <= 0:
        raise ValueError("group and message timeouts must be greater than zero")
    if args.role == "B" and args.receive_timeout < 0:
        raise ValueError("receive timeout must be zero or greater")
    if args.post_receive_linger < 0:
        raise ValueError("post-receive linger must be zero or greater")
    current_netns = _netns_id()
    if (
        args.peer_netns_id is not None
        and current_netns is not None
        and args.peer_netns_id == current_netns
    ):
        raise ValueError(
            "A and B are in the same Linux network namespace; the kernel can "
            "short-circuit Agent IP traffic locally, so this cannot prove the "
            "MASQUE/5GC path"
        )


async def _wait_for_group(
    sdk: AgentSdk,
    group_id: str,
    target_agent_id: str,
    timeout: float,
):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        snapshot = await sdk.get_group_snapshot(group_id)
        if snapshot is not None and target_agent_id in snapshot.members_by_agent_id:
            return snapshot
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(
                "timed out waiting for the signed acf_group_config containing "
                f"target {target_agent_id}"
            )
        await asyncio.sleep(0.2)


async def _initialize_and_apply_identity(
    sdk: AgentSdk,
    args: argparse.Namespace,
    logger: logging.Logger,
):
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
        log_file_path=args.sdk_log_file,
        log_level=args.log_level,
    )
    if not initialized.masque_connected:
        raise RuntimeError("SDK init returned without an active MASQUE connection")
    agent_ip = str(ip_interface(initialized.agent_tun_cidr).ip)
    if args.expected_agent_ip and agent_ip != args.expected_agent_ip:
        raise RuntimeError(
            f"AgentRuntime returned Agent IP {agent_ip}, expected {args.expected_agent_ip}"
        )
    _emit(
        logger,
        args.role,
        "MASQUE_CONNECTED",
        masque_url=args.masque_url,
        agent_tun_cidr=initialized.agent_tun_cidr,
        tun_name=args.tun_name,
    )

    profile = await sdk.apply_identity(
        owner=args.owner,
        name=args.agent_name,
        description=f"WSL Ubuntu MASQUE two-instance test role {args.role}",
        metadata={"region": args.region, "os": "Linux", "version": __version__},
    )
    _emit(
        logger,
        args.role,
        "IDENTITY_READY",
        agent_id=profile.agent_id,
        instruction=(
            "copy this agent_id into role A --target-agent-id"
            if args.role == "B"
            else None
        ),
    )
    return initialized, profile


async def run_role_a(
    sdk: AgentSdk,
    args: argparse.Namespace,
    logger: logging.Logger,
) -> None:
    _, profile = await _initialize_and_apply_identity(sdk, args, logger)
    group = await sdk.create_group(
        profile.agent_id,
        [args.target_agent_id],
        group_name=args.group_name,
        scope="private",
        max_members=2,
    )
    _emit(logger, args.role, "GROUP_CREATED", group_id=group.group_id)
    snapshot = await _wait_for_group(
        sdk,
        group.group_id,
        args.target_agent_id,
        args.group_timeout,
    )
    _emit(
        logger,
        args.role,
        "GROUP_READY",
        group_id=group.group_id,
        generation=snapshot.generation,
        target_agent_id=args.target_agent_id,
    )
    receipt = await sdk.send_message(
        group.group_id,
        args.target_agent_id,
        args.message,
        timeout_seconds=args.message_timeout,
        message_type=args.message_type,
        task_id=args.task_id,
    )
    if not receipt.delivered:
        raise RuntimeError("role B did not return status=OK")
    _emit(
        logger,
        args.role,
        "A2A_MESSAGE_DELIVERED",
        group_id=group.group_id,
        target_agent_id=args.target_agent_id,
        message_id=receipt.message_id,
    )
    if args.deregister_on_exit:
        await sdk.deregister_identity(profile.agent_id, reason="retired")
        _emit(logger, args.role, "IDENTITY_DEREGISTERED", agent_id=profile.agent_id)


async def run_role_b(
    sdk: AgentSdk,
    args: argparse.Namespace,
    logger: logging.Logger,
    received_event: asyncio.Event,
) -> None:
    _, profile = await _initialize_and_apply_identity(sdk, args, logger)
    _emit(
        logger,
        args.role,
        "WAITING_FOR_A2A_MESSAGE",
        receive_timeout_seconds=args.receive_timeout,
    )
    try:
        if args.receive_timeout == 0:
            await received_event.wait()
        else:
            await asyncio.wait_for(received_event.wait(), args.receive_timeout)
    except TimeoutError as exc:
        raise TimeoutError(
            "role B timed out before receiving an A2A message through MASQUE"
        ) from exc
    _emit(logger, args.role, "TEST_PASSED", proof="A2A_MESSAGE_RECEIVED")
    if args.post_receive_linger:
        await asyncio.sleep(args.post_receive_linger)
    if args.deregister_on_exit:
        await sdk.deregister_identity(profile.agent_id, reason="retired")
        _emit(logger, args.role, "IDENTITY_DEREGISTERED", agent_id=profile.agent_id)


async def run_instance(
    args: argparse.Namespace,
    *,
    sdk: AgentSdk | None = None,
) -> None:
    args = _apply_role_defaults(args)
    _validate_args(args)
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    previous_state_home = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = str(state_dir)
    logger = _configure_app_logger(args.role, args.app_log_file)
    current_netns = _netns_id()
    _emit(
        logger,
        args.role,
        "INSTANCE_STARTING",
        pid=os.getpid(),
        netns_id=current_netns,
        state_dir=str(state_dir),
        sdk_log_file=str(Path(args.sdk_log_file).expanduser().resolve()),
    )
    if args.peer_netns_id is None:
        _emit(
            logger,
            args.role,
            "NETNS_CHECK_REQUIRED",
            message=(
                "compare A/B netns_id values; they must differ to prove traffic "
                "did not short-circuit inside one Linux network namespace"
            ),
        )

    active_sdk = sdk or AgentSdk()
    received_event = asyncio.Event()
    network_listener = PairNetworkListener(args.role, logger)
    group_listener = PairGroupListener(args.role, logger, received_event)
    unregister_network = active_sdk.register_network_message_listener(network_listener)
    unregister_group = active_sdk.register_group_message_listener(group_listener)
    try:
        if args.role == "A":
            await run_role_a(active_sdk, args, logger)
        else:
            await run_role_b(active_sdk, args, logger, received_event)
    finally:
        try:
            unregister_group()
            unregister_network()
            await active_sdk.close()
            _emit(logger, args.role, "INSTANCE_CLOSED")
        finally:
            if previous_state_home is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = previous_state_home


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Run one side of a real two-instance Agent SDK MASQUE/A2A test. "
            "Start role B first, copy its agent_id, then start role A."
        )
    )
    value.add_argument("--role", required=True, type=str.upper, choices=("A", "B"))
    value.add_argument("--runtime-ip", required=True)
    value.add_argument("--runtime-port", required=True, type=int)
    value.add_argument("--local-vlan-ip", required=True)
    value.add_argument("--masque-url", required=True)
    value.add_argument("--masque-token")
    value.add_argument("--tcp-port", type=int)
    value.add_argument("--udp-port", type=int)
    value.add_argument("--tun-name")
    value.add_argument("--tun-mtu", type=int, default=1280)
    value.add_argument("--expected-agent-ip")
    value.add_argument("--agent-name")
    value.add_argument("--owner")
    value.add_argument("--region", default="CN")
    value.add_argument("--target-agent-id")
    value.add_argument("--group-name", default="masque-two-instance-test")
    value.add_argument("--task-id", default="masque-two-instance-test")
    value.add_argument("--message-type", default="text")
    value.add_argument(
        "--message",
        type=_json_object,
        default={"type": "text", "content": "hello from Agent A through MASQUE"},
    )
    value.add_argument("--group-timeout", type=float, default=60.0)
    value.add_argument("--message-timeout", type=float, default=10.0)
    value.add_argument(
        "--receive-timeout",
        type=float,
        default=300.0,
        help="role B wait time in seconds; use 0 to wait forever",
    )
    value.add_argument(
        "--post-receive-linger",
        type=float,
        default=2.0,
        help="seconds role B keeps listeners alive after receipt so status=OK is flushed",
    )
    value.add_argument(
        "--peer-netns-id",
        type=int,
        help=(
            "network namespace ID printed by the peer; the script rejects an "
            "equal value because that cannot prove the MASQUE path"
        ),
    )
    value.add_argument("--state-dir")
    value.add_argument("--app-log-file")
    value.add_argument("--sdk-log-file")
    value.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default="INFO",
    )
    value.add_argument("--deregister-on-exit", action="store_true")
    return value


if __name__ == "__main__":
    parsed_args = parser().parse_args()
    try:
        asyncio.run(run_instance(parsed_args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
    except Exception as error:
        print(f"MASQUE TWO-INSTANCE TEST FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
