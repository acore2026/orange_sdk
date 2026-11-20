#!/usr/bin/env python3
"""Three real terminals, one AgentSdk per process, no shared coordinator.

Start B/C, then A with the same --run-id. Discovery selects batch-specific
skills; after grouping all coordination and delivery receipts use AGW Tracks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import signal
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_sdk import AGWConfig, AgentSdk, NetworkMessageAction, NetworkMessageType, __version__


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False, default=str), flush=True)


def positive_seconds(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def batch_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", value):
        raise argparse.ArgumentTypeError("run-id must contain 1..32 letters, digits, underscores or hyphens")
    return value


def parser(default_role: str | None = None) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--role", choices=("a", "b", "c"), default=default_role, required=default_role is None)
    result.add_argument("--run-id", type=batch_id, required=True, help="same fresh test batch on all terminals")
    result.add_argument("--runtime-ip", required=True)
    result.add_argument("--runtime-port", type=int, default=8088)
    result.add_argument("--masque-url", required=True)
    result.add_argument("--masque-authorization")
    result.add_argument("--relay-ipv4", required=True, help="Relay N6 IPv4 reached through CONNECT-IP/UPF")
    result.add_argument("--relay-port", type=int, default=4433)
    result.add_argument("--tcp-port", type=int, default=4001)
    result.add_argument("--udp-port", type=int, default=4002)
    result.add_argument("--tun-name", help="defaults to agw_a / agw_b / agw_c")
    result.add_argument("--dnn", default="internet")
    result.add_argument("--messages", type=positive_integer, default=3)
    result.add_argument("--message-interval", type=positive_seconds, default=.5)
    result.add_argument("--timeout", type=positive_seconds, default=180)
    result.add_argument("--message-timeout", type=positive_seconds, default=30)
    result.add_argument("--log-file")
    result.add_argument("--state-dir", help="dedicated test state; defaults to ~/.local/state/agent-sdk/agw-demo/<run>/<role>")
    result.add_argument("--test-vc-private-key", help="optional lab issuer key override; otherwise uses the SDK lab issuer")
    return result


class Scenario:
    def __init__(self, args: argparse.Namespace, sdk: AgentSdk):
        self.args, self.sdk = args, sdk
        self.profile = None
        self.group_id: str | None = None
        self.group_version: int | None = None
        self.members: dict[str, str] = {}
        self.configurations: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue(maxsize=16)
        self.messages: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue(maxsize=128)
        self.callback_error: Exception | None = None
        self.finishing = False
        self.publisher_departed = asyncio.Event()

    def name(self, role: str) -> str:
        return f"agw-{self.args.run_id}-{role}"

    def skill(self, role: str) -> str:
        return f"agw-demo/{self.args.run_id}/{role}"

    async def on_network_message(self, message_type, payload):
        if message_type is NetworkMessageType.GROUP_INVITATION:
            info = payload.get("group_info", {})
            accepted = isinstance(info, Mapping) and info.get("group_name") == f"agw-{self.args.run_id}"
            emit("INVITATION", role=self.args.role, accepted=accepted, payload=payload)
            return NetworkMessageAction.ACCEPT if accepted else NetworkMessageAction.REJECT
        if message_type is NetworkMessageType.GROUP_CONFIG:
            # The SDK has committed this snapshot. Do not await AGW here:
            # ACF opens ACTIVE only after receiving this callback's N-13 ACK.
            try:
                members = payload.get("members", {})
                names = {member.get("agent_name") for member in members.values()} if isinstance(members, Mapping) else set()
                if payload.get("group_id") == self.group_id and self.finishing and self.name("a") not in names:
                    self.publisher_departed.set()
                if payload.get("notification_type") == "acf_group_config":
                    self.configurations.put_nowait(dict(payload))
                emit("GROUP_CONFIG_ACK", role=self.args.role, group_id=payload.get("group_id"), group_version=payload.get("group_version"))
            except Exception as exc:
                self.callback_error = exc
            return NetworkMessageAction.ACK
        return NetworkMessageAction.REJECT

    async def on_group_message(self, group_id, sender_agent_id, payload):
        # L3 is diagnostic only; it never counts as AGW delivery.
        emit("L3_MESSAGE", group_id=group_id, sender_agent_id=sender_agent_id, payload=payload)

    def on_agw_event(self, event):
        emit("AGW_EVENT", role=self.args.role, detail=event)
        if event.get("event") == "message":
            try:
                self.messages.put_nowait(dict(event))
            except asyncio.QueueFull:
                self.callback_error = RuntimeError("AGW application receive queue exceeded")

    def check_callbacks(self) -> None:
        if self.callback_error is not None:
            raise RuntimeError("terminal callback failed") from self.callback_error

    async def discover_pair(self) -> list[str]:
        found = {}
        while len(found) != 2:
            self.check_callbacks()
            for role in ("b", "c"):
                if role in found:
                    continue
                values = await self.sdk.discover_agents(
                    self.profile.agent_id, f"AGW demo {self.args.run_id}", [self.skill(role)], max_results=64)
                matches = {value.agent_id for value in values if self.skill(role) in value.skills}
                if len(matches) > 1:
                    raise RuntimeError(f"multiple {role.upper()} Agents in this run; choose a fresh --run-id")
                if matches:
                    found[role] = matches.pop()
            if len(found) != 2:
                await asyncio.sleep(.5)
        if len(set(found.values())) != 2:
            raise RuntimeError("B and C must be different Agents")
        emit("DISCOVERY_CONFIRMED", peers=found)
        return [found["b"], found["c"]]

    async def group(self) -> None:
        expected_id = None
        if self.args.role == "a":
            targets = await self.discover_pair()
            result = await self.sdk.create_group(self.profile.agent_id, targets,
                group_name=f"agw-{self.args.run_id}", dnn=self.args.dnn, max_members=3)
            expected_id = result.group_id
        while True:
            value = await self.configurations.get()
            if expected_id is not None and value.get("group_id") != expected_id:
                continue
            snapshot = await self.sdk.get_group_snapshot(value["group_id"])
            if snapshot is None:
                continue
            by_name = {member.agent_name: member.agent_id for member in snapshot.members_by_agent_id.values()}
            if set(by_name) != {self.name(role) for role in ("a", "b", "c")}:
                raise RuntimeError("group must contain exactly this batch's A/B/C Agents")
            self.group_id, self.group_version = snapshot.group_id, snapshot.group_version
            self.members = {role: by_name[self.name(role)] for role in ("a", "b", "c")}
            if self.members[self.args.role] != self.profile.agent_id:
                raise RuntimeError("group local identity mismatch")
            emit("GROUP_CONFIRMED", role=self.args.role, group_id=self.group_id, group_version=self.group_version, members=self.members)
            return

    async def prepare_tracks(self) -> None:
        own_track = "status" if self.args.role == "a" else "receipt"
        await self.sdk.register_agw_track(self.group_id, own_track, "application/json")
        subscriptions: dict[str, str] = {}
        for role, member in self.members.items():
            if role == self.args.role:
                continue
            required = "status" if role == "a" else "receipt"
            while True:
                self.check_callbacks()
                directory = self.sdk.get_agw_catalog(self.group_id, member)
                if directory and required in {entry["name"] for entry in directory["tracks"]}:
                    emit("CATALOG_CONFIRMED", role=self.args.role, publisher_agent_id=member, catalog=directory)
                    break
                await asyncio.sleep(.1)
            if self.args.role == "a" or role == "a":
                await self.sdk.subscribe_agw_track(self.group_id, member, required)
                subscriptions[member] = required
        await self.wait_subscriptions(subscriptions)
        emit("AGW_READY", role=self.args.role, state=self.sdk.get_agw_state(self.group_id))

    async def wait_subscriptions(self, subscriptions: Mapping[str, str]) -> None:
        deadline = time.monotonic() + self.args.message_timeout
        while time.monotonic() < deadline:
            self.check_callbacks()
            ready = {
                (item.get("member_agent_id"), item.get("track"))
                for item in self.sdk.get_agw_state(self.group_id)
                if item.get("direction") == "subscriber" and item.get("state") == "READY"
            }
            if all((member, track) in ready for member, track in subscriptions.items()):
                return
            await asyncio.sleep(.1)
        raise TimeoutError(f"AGW subscriptions not ready: {dict(subscriptions)}")

    async def receive(self, timeout: float) -> tuple[str, Mapping[str, Any]]:
        async def next_valid():
            while True:
                self.check_callbacks()
                event = await self.messages.get()
                if event.get("group_id") != self.group_id or event.get("group_version") != self.group_version:
                    continue
                payload = event.get("payload")
                if not isinstance(payload, Mapping) or payload.get("run_id") != self.args.run_id:
                    continue
                role = next((role for role, member in self.members.items() if member == event.get("publisher_agent_id")), None)
                if role is None or payload.get("role") != role:
                    raise RuntimeError("AGW payload role/publisher mismatch")
                if event.get("track") != ("status" if role == "a" else "receipt"):
                    raise RuntimeError("unexpected business track")
                return role, payload
        return await asyncio.wait_for(next_valid(), timeout)

    async def send(self, message_type: str, **fields) -> None:
        payload = {"type": message_type, "run_id": self.args.run_id, "role": self.args.role, **fields}
        await self.sdk.publish_agw_message(self.group_id, "status" if self.args.role == "a" else "receipt", payload)
        emit("AGW_SENT", role=self.args.role, group_id=self.group_id, payload=payload)

    async def wait_receipts(self, kind: str, message_id: str | None = None) -> None:
        confirmed = set()
        deadline = time.monotonic() + self.args.message_timeout
        while confirmed != {"b", "c"}:
            role, payload = await self.receive(max(.001, deadline - time.monotonic()))
            if role in ("b", "c") and payload.get("type") == kind and (message_id is None or payload.get("message_id") == message_id):
                confirmed.add(role)

    async def publisher(self) -> None:
        await self.wait_receipts("READY")
        for sequence in range(1, self.args.messages + 1):
            message_id = str(uuid.uuid4())
            await self.send("DATA", message_id=message_id, sequence=sequence, text="AGW 群组多播")
            await self.wait_receipts("RECEIVED", message_id)
            emit("MULTICAST_CONFIRMED", group_id=self.group_id, message_id=message_id, sequence=sequence,
                 receivers=[self.members["b"], self.members["c"]])
            await asyncio.sleep(self.args.message_interval)
        await self.send("COMPLETE", count=self.args.messages)
        await self.wait_receipts("COMPLETE_ACK")

    async def subscriber(self) -> None:
        received = {}
        await self.send("READY")
        while True:
            try:
                role, payload = await self.receive(1 if not received else self.args.message_timeout)
            except asyncio.TimeoutError:
                if received:
                    raise
                await self.send("READY")
                continue
            if role != "a":
                continue
            if payload.get("type") == "DATA":
                sequence, message_id = payload.get("sequence"), payload.get("message_id")
                if type(sequence) is not int or not isinstance(message_id, str) or not message_id:
                    raise RuntimeError("invalid DATA object")
                if sequence in received and received[sequence] != message_id:
                    raise RuntimeError("sequence reused with different message ID")
                if sequence != len(received) + 1 and sequence not in received:
                    raise RuntimeError("missing or out-of-order multicast object")
                received[sequence] = message_id
                emit("MULTICAST_RECEIVED", role=self.args.role, group_id=self.group_id, payload=payload)
                await self.send("RECEIVED", message_id=message_id, sequence=sequence)
            elif payload.get("type") == "COMPLETE":
                if type(payload.get("count")) is not int or payload["count"] != len(received) or not received:
                    raise RuntimeError("COMPLETE count does not match received objects")
                self.finishing = True
                await self.send("COMPLETE_ACK")
                # A's normal deregistration changes membership. Wait for that
                # notification before B/C retire, avoiding a final-ACK race.
                await asyncio.wait_for(self.publisher_departed.wait(), self.args.message_timeout)
                return

    async def execute(self) -> None:
        self.sdk.register_network_message_listener(self)
        self.sdk.register_group_message_listener(self)
        self.sdk.register_agw_listener(self.on_agw_event)
        initialized = await self.sdk.init(self.args.runtime_ip, self.args.runtime_port, self.args.tcp_port, self.args.udp_port,
            masque_server_url=self.args.masque_url, masque_authorization=self.args.masque_authorization,
            tun_name=self.args.tun_name or f"agw_{self.args.role}",
            log_file_path=self.args.log_file or f"logs/agw-{self.args.run_id}-{self.args.role}.log")
        emit("INITIALIZED", role=self.args.role, result=initialized)
        previous = self.sdk.local_profile
        if previous is not None:
            if previous.agent_name != self.name(self.args.role):
                raise RuntimeError("test state belongs to another Agent; use a separate --state-dir")
            result = await self.sdk.deregister_identity(previous.agent_id, reason="replaced")
            if not result.success:
                raise RuntimeError(f"previous test Agent cleanup failed: {result.message}")
        self.profile = await self.sdk.apply_identity("agw-terminal-test", self.name(self.args.role), "ACN AGW three-terminal test",
            {"region": "lab", "os": "Linux", "version": __version__})
        credentials = [self.profile.identity_vc]
        if self.args.role == "a":
            ability = await self.sdk.get_network_ability(self.profile.agent_id)
            credentials.append(ability.ability_vc)
        result = await self.sdk.register_capabilities(self.profile.agent_id, 10, credentials=credentials,
            capabilities=[self.skill(self.args.role)], agent_name=self.profile.agent_name,
            test_vc_private_key_path=self.args.test_vc_private_key)
        if not result.success:
            raise RuntimeError(f"Profile registration failed: {result.message}")
        emit("PROFILE_READY", role=self.args.role, agent_id=self.profile.agent_id, skill=self.skill(self.args.role))
        await self.group()
        await self.prepare_tracks()
        await (self.publisher() if self.args.role == "a" else self.subscriber())

    async def cleanup(self) -> None:
        try:
            profile = self.sdk.local_profile
            if profile is not None and profile.agent_name == self.name(self.args.role):
                result = await asyncio.wait_for(self.sdk.deregister_identity(profile.agent_id, reason="retired"), 30)
                if not result.success:
                    raise RuntimeError(f"Agent deregistration failed: {result.message}")
                emit("DEREGISTERED", role=self.args.role, agent_id=profile.agent_id)
        finally:
            await self.sdk.close()


async def run(args: argparse.Namespace, *, sdk: AgentSdk | None = None) -> None:
    if sdk is None:
        state_dir = args.state_dir or Path.home() / ".local/state/agent-sdk/agw-demo" / args.run_id / args.role
        sdk = AgentSdk(agw_config=AGWConfig(args.relay_ipv4, args.relay_port), agent_state_directory=state_dir)
    scenario = Scenario(args, sdk)
    failure = None
    try:
        await asyncio.wait_for(scenario.execute(), args.timeout)
    except BaseException as exc:
        failure = exc
    try:
        await scenario.cleanup()
    except Exception as exc:
        if failure is not None:
            raise RuntimeError(f"test failed: {failure!r}; cleanup also failed: {exc}") from failure
        raise
    if failure is not None:
        raise failure
    emit("SCENARIO_PASSED", role=args.role, group_id=scenario.group_id)


async def run_with_signals(args):
    task = asyncio.create_task(run(args))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main(default_role: str | None = None) -> None:
    args = parser(default_role).parse_args()
    try:
        asyncio.run(run_with_signals(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        emit("SCENARIO_INTERRUPTED", role=args.role)
        sys.exit(130)
    except Exception as exc:
        emit("SCENARIO_FAILED", role=args.role, error=f"{type(exc).__name__}: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
