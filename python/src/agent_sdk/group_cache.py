from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .errors import AgentSdkError, ErrorCode
from .models import GroupConfigSnapshot, GroupMemberInfo
from .routes import GroupRouteManager


SEMANTIC_VERSION = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentSdkError(
            ErrorCode.GROUP_CONFIG_INVALID,
            f"{field} must be a non-empty string",
            field=field,
        )
    return value.strip()


def _parse_service_endpoint(value: Any, agent_ip: str, field: str) -> tuple[str, int]:
    endpoint = _require_string(value, field)
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise AgentSdkError(
            ErrorCode.GROUP_CONFIG_INVALID,
            f"{field} contains an invalid port",
            field=field,
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path
        or parsed.fragment
    ):
        raise AgentSdkError(
            ErrorCode.GROUP_CONFIG_INVALID,
            f"{field} must be an absolute HTTP/HTTPS URL without credentials or fragment",
            field=field,
        )
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if not 1 <= port <= 65535:
        raise AgentSdkError(
            ErrorCode.GROUP_CONFIG_INVALID,
            f"{field} port must be in 1..65535",
            field=field,
        )
    # The service URL supplies scheme, port and path.  The verified Agent IP is
    # always used as the destination so traffic follows the installed /32 route.
    host = f"[{agent_ip}]" if ":" in agent_ip else agent_ip
    authority = f"{host}:{port}"
    return urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, "")), port


def _parse_timestamp(value: Any) -> datetime:
    text = _require_string(value, "timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgentSdkError(
            ErrorCode.GROUP_CONFIG_INVALID,
            "timestamp must be RFC3339",
            field="timestamp",
        ) from exc
    if parsed.tzinfo is None:
        raise AgentSdkError(
            ErrorCode.GROUP_CONFIG_INVALID,
            "timestamp must include a timezone",
            field="timestamp",
        )
    return parsed.astimezone(timezone.utc)


class GroupMemberCache:
    def __init__(self, route_manager: GroupRouteManager) -> None:
        self._route_manager = route_manager
        self._snapshots: dict[str, GroupConfigSnapshot] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def build_candidate(
        payload: Mapping[str, Any],
        *,
        local_agent_id: str,
        local_agent_ip: str,
        local_tcp_port: int,
        local_udp_port: int,
    ) -> GroupConfigSnapshot:
        if payload.get("notification_type") != "acf_group_config":
            raise AgentSdkError(
                ErrorCode.GROUP_CONFIG_INVALID,
                "notification_type must be acf_group_config",
                field="notification_type",
            )
        version = _require_string(payload.get("version"), "version")
        if SEMANTIC_VERSION.fullmatch(version) is None:
            raise AgentSdkError(
                ErrorCode.GROUP_CONFIG_INVALID,
                "version must use semantic version syntax",
                field="version",
            )
        try:
            major = int(version.split(".", 1)[0])
        except ValueError as exc:
            raise AgentSdkError(
                ErrorCode.GROUP_CONFIG_INVALID,
                "version must use semantic version syntax",
                field="version",
            ) from exc
        if major != 1:
            raise AgentSdkError(
                ErrorCode.GROUP_CONFIG_VERSION_UNSUPPORTED,
                f"unsupported group config version: {version}",
                field="version",
            )

        group_id = _require_string(payload.get("group_id"), "group_id")
        target_agent_id = payload.get("target_agent_id")
        if target_agent_id is not None and _require_string(
            target_agent_id, "target_agent_id"
        ) != local_agent_id:
            raise AgentSdkError(
                ErrorCode.GROUP_CONFIG_INVALID,
                "target_agent_id does not match the local agent",
                field="target_agent_id",
            )
        timestamp = _parse_timestamp(payload.get("timestamp"))
        raw_members = payload.get("members")
        if not isinstance(raw_members, Mapping) or not raw_members:
            raise AgentSdkError(
                ErrorCode.GROUP_CONFIG_INVALID,
                "members must be a non-empty object",
                field="members",
            )

        members: dict[str, GroupMemberInfo] = {}
        claimed_ips: dict[str, str] = {}
        for label, value in raw_members.items():
            if not isinstance(value, Mapping):
                raise AgentSdkError(
                    ErrorCode.GROUP_CONFIG_INVALID,
                    f"members.{label} must be an object",
                    field=f"members.{label}",
                )
            prefix = f"members.{label}"
            agent_id = _require_string(value.get("agent_id"), f"{prefix}.agent_id")
            if agent_id in members:
                raise AgentSdkError(
                    ErrorCode.GROUP_CONFIG_INVALID,
                    f"duplicate agent_id: {agent_id}",
                    field=f"{prefix}.agent_id",
                )
            try:
                agent_ip = str(
                    ip_address(_require_string(value.get("agent_ip"), f"{prefix}.agent_ip"))
                )
            except ValueError as exc:
                raise AgentSdkError(
                    ErrorCode.GROUP_CONFIG_INVALID,
                    f"invalid agent_ip: {value.get('agent_ip')}",
                    field=f"{prefix}.agent_ip",
                ) from exc
            previous = claimed_ips.get(agent_ip)
            if previous is not None and previous != agent_id:
                raise AgentSdkError(
                    ErrorCode.GROUP_CONFIG_INVALID,
                    f"agent_ip {agent_ip} is claimed by multiple agents",
                    field=f"{prefix}.agent_ip",
                )
            claimed_ips[agent_ip] = agent_id

            raw_skills = value.get("skills")
            if not isinstance(raw_skills, list) or not all(
                isinstance(item, str) and item for item in raw_skills
            ):
                raise AgentSdkError(
                    ErrorCode.GROUP_CONFIG_INVALID,
                    "skills must be a list of non-empty strings",
                    field=f"{prefix}.skills",
                )
            service_endpoint, tcp_port = _parse_service_endpoint(
                value.get("service_endpoints"),
                agent_ip,
                f"{prefix}.service_endpoints",
            )
            members[agent_id] = GroupMemberInfo(
                agent_id=agent_id,
                agent_name=_require_string(
                    value.get("agent_name"), f"{prefix}.agent_name"
                ),
                capabilities=tuple(raw_skills),
                agent_ip=agent_ip,
                tcp_port=tcp_port,
                udp_port=0,
                did_key="",
                service_endpoint=service_endpoint,
            )

        local_member = members.get(local_agent_id)
        if local_member is None:
            raise AgentSdkError(
                ErrorCode.GROUP_CONFIG_INVALID,
                "group config does not contain the local agent",
                field="members",
            )
        if local_member.agent_ip != str(ip_address(local_agent_ip)):
            raise AgentSdkError(
                ErrorCode.AGENT_IP_MISMATCH,
                "local member agent_ip does not match the Agent TUN address",
                field="members.agent_ip",
            )
        if local_member.tcp_port != local_tcp_port:
            raise AgentSdkError(
                ErrorCode.GROUP_CONFIG_INVALID,
                "local member service endpoint port does not match the SDK listener",
                field="members",
            )

        return GroupConfigSnapshot.immutable(
            group_id=group_id,
            version=version,
            notification_timestamp=timestamp,
            members=members,
        )

    async def commit(
        self, candidate: GroupConfigSnapshot, *, local_agent_id: str
    ) -> GroupConfigSnapshot:
        async with self._lock:
            current = self._snapshots.get(candidate.group_id)
            if (
                current is not None
                and candidate.notification_timestamp <= current.notification_timestamp
            ):
                raise AgentSdkError(
                    ErrorCode.GROUP_CONFIG_STALE,
                    "group config is not newer than the committed snapshot",
                )
            generation = 1 if current is None else current.generation + 1
            committed = GroupConfigSnapshot.immutable(
                group_id=candidate.group_id,
                version=candidate.version,
                notification_timestamp=candidate.notification_timestamp,
                members=dict(candidate.members_by_agent_id),
                generation=generation,
            )
            peers = {
                member.agent_ip
                for agent_id, member in committed.members_by_agent_id.items()
                if agent_id != local_agent_id
            }
            await self._route_manager.replace_group_peers(candidate.group_id, peers)
            self._snapshots[candidate.group_id] = committed
            return committed

    async def resolve(self, group_id: str, agent_id: str) -> GroupMemberInfo:
        async with self._lock:
            snapshot = self._snapshots.get(group_id)
            if snapshot is None:
                raise AgentSdkError(
                    ErrorCode.GROUP_NOT_ACTIVE,
                    f"group {group_id} has no committed configuration",
                )
            member = snapshot.members_by_agent_id.get(agent_id)
            if member is None:
                raise AgentSdkError(
                    ErrorCode.TARGET_NOT_IN_GROUP,
                    f"target {agent_id} is not in group {group_id}",
                )
            return member

    async def snapshot(self, group_id: str) -> GroupConfigSnapshot | None:
        async with self._lock:
            return self._snapshots.get(group_id)

    async def close_group(self, group_id: str) -> None:
        async with self._lock:
            await self._route_manager.replace_group_peers(group_id, set())
            self._snapshots.pop(group_id, None)

    async def close(self) -> None:
        async with self._lock:
            for group_id in tuple(self._snapshots):
                await self._route_manager.replace_group_peers(group_id, set())
            self._snapshots.clear()
