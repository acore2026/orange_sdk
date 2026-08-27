from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class NetworkMessageType(str, Enum):
    GROUP_INVITATION = "GROUP_INVITATION"
    GROUP_CONFIG = "GROUP_CONFIG"
    UNKNOWN = "UNKNOWN"


class NetworkMessageAction(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    ACK = "ACK"


@dataclass(frozen=True, slots=True)
class GroupMemberInfo:
    agent_id: str
    agent_name: str
    capabilities: tuple[str, ...]
    agent_ip: str
    tcp_port: int
    udp_port: int
    did_key: str
    service_endpoint: str = ""

    @property
    def skills(self) -> tuple[str, ...]:
        """Skills advertised by the member (``capabilities`` compatibility alias)."""
        return self.capabilities


@dataclass(frozen=True, slots=True)
class GroupConfigSnapshot:
    group_id: str
    version: str
    notification_timestamp: datetime
    members_by_agent_id: Mapping[str, GroupMemberInfo]
    generation: int = 0

    @classmethod
    def immutable(
        cls,
        *,
        group_id: str,
        version: str,
        notification_timestamp: datetime,
        members: dict[str, GroupMemberInfo],
        generation: int = 0,
    ) -> "GroupConfigSnapshot":
        return cls(
            group_id=group_id,
            version=version,
            notification_timestamp=notification_timestamp,
            members_by_agent_id=MappingProxyType(dict(members)),
            generation=generation,
        )


@dataclass(frozen=True, slots=True)
class SdkInitResult:
    runtime_connected: bool
    masque_connected: bool
    local_tcp_endpoint: str
    local_udp_endpoint: str
    agent_tcp_endpoint: str
    agent_udp_endpoint: str
    agent_tun_cidr: str
    masque_proxy_endpoint: str


@dataclass(slots=True)
class GroupInfo:
    group_id: str
    group_name: str
    status: str = "PENDING"


@dataclass(frozen=True, slots=True)
class MessageReceipt:
    message_id: str
    delivered: bool
    delivered_at: datetime | None


@dataclass(frozen=True, slots=True)
class AgentProfile:
    agent_id: str
    agent_name: str
    identity_vc: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class OperationResult:
    success: bool
    operation_id: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class NetworkAbility:
    ability_vc: Mapping[str, Any]
    abilities: tuple[str, ...]
    valid_until: datetime | None


@dataclass(frozen=True, slots=True)
class DiscoveredAgent:
    agent_id: str
    ip: str
    tcp_port: int
    udp_port: int
    skills: tuple[str, ...]
    priority: int


@dataclass(slots=True)
class OffloadingSession:
    session_id: str
    sandbox_id: str
    state: str
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
