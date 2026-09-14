from __future__ import annotations

from dataclasses import dataclass
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
    service_endpoints: str
    skills: tuple[str, ...]
    priority: int


class ComputeRequestType(str, Enum):
    CREATE = "CREATE"
    QUERY = "QUERY"
    CANCEL = "CANCEL"
    RELEASE = "RELEASE"


class ComputeInputFormat(str, Enum):
    NATURAL_LANGUAGE = "NATURAL_LANGUAGE"
    STRUCTURED = "STRUCTURED"


class ComputeRole(str, Enum):
    CONSUMER = "consumer"
    PRODUCER = "producer"


@dataclass(frozen=True, slots=True)
class AcnContext:
    group_id: str
    requester_agent_id: str
    target_agent_id: str


@dataclass(frozen=True, slots=True)
class ComputeResources:
    cpu_millicores: int | None = None
    memory_mib: int | None = None
    gpu_count: int | None = None
    gpu_model: str | None = None


@dataclass(frozen=True, slots=True)
class ComputeConstraints:
    capability_id: str
    api_version: str | None = None
    image_id: str | None = None
    resources: ComputeResources | None = None
    dnn: str | None = None
    snssai: str | None = None
    allow_base_qos: bool | None = None
    max_duration_ms: int | None = None
    placement_region: str | None = None
    data_residency_region: str | None = None


@dataclass(frozen=True, slots=True)
class ComputeSessionRequest:
    message_type: str
    request_type: ComputeRequestType
    input_format: ComputeInputFormat
    request_id: str
    acn_context: AcnContext | None = None
    text: str | None = None
    constraints: ComputeConstraints | None = None
    compute_service_session_id: str | None = None
    target_request_id: str | None = None
    ui_locale: str | None = None


@dataclass(frozen=True, slots=True)
class ComputeSessionStatus:
    message_type: str
    request_id: str
    status: str
    cause: str
    compute_service_session_id: str | None = None
    status_revision: str | None = None
    missing_fields: tuple[str, ...] = ()
    result: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Snssai:
    sst: int
    sd: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeDataPlane:
    access_type: str
    session_selection: str


@dataclass(frozen=True, slots=True)
class ComputeNetworkBinding:
    pdu_session_id: int
    dnn: str
    snssai: Snssai
    ue_ipv4: str
    runtime_data_plane: RuntimeDataPlane


@dataclass(frozen=True, slots=True)
class ComputeConnectionParameters:
    media_connections_path: str
    transport: str
    recognition_target_path_template: str | None = None
    video_codec: str | None = None


@dataclass(frozen=True, slots=True)
class ComputingSession:
    compute_service_session_id: str
    compute_instance_id: str
    binding_ref: str
    role: ComputeRole
    receiver_agent_id: str
    service_endpoint: str
    network_binding: ComputeNetworkBinding
    connection_parameters: ComputeConnectionParameters
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ComputingContext:
    compute_service_session_id: str
    compute_instance_id: str
    binding_ref: str
    role: ComputeRole
    agent_id: str


@dataclass(frozen=True, slots=True)
class RecognitionTarget:
    label: str
    prompt: str


@dataclass(frozen=True, slots=True)
class RecognitionTargetStatus:
    request_id: str
    computing_context: ComputingContext
    status: str
    target_revision: str
    target: RecognitionTarget


class ControlInputType(str, Enum):
    TEXT = "TEXT"
    STRUCTURED = "STRUCTURED"


class ControlAction(str, Enum):
    MOVEMENT = "movement"
    GRAB = "grab"
    SEARCH_OBJECT = "search_object"


class ControlTargetRole(str, Enum):
    PRODUCER = "producer"
    SANDBOX = "sandbox"


@dataclass(frozen=True, slots=True)
class ControlActionTarget:
    role: ControlTargetRole
    agent_id: str | None = None


@dataclass(frozen=True, slots=True)
class ControlActionRequest:
    request_id: str
    input_type: ControlInputType
    action: ControlAction | None = None
    text: str | None = None
    language: str | None = None
    parameters: Mapping[str, Any] | None = None
    target: ControlActionTarget | None = None


@dataclass(frozen=True, slots=True)
class ControlActionStatus:
    request_id: str
    action_id: str
    status: str
    cause: str
    computing_context: ComputingContext | None = None
    normalized_action: ControlAction | None = None
    normalized_parameters: Mapping[str, Any] | None = None
    result: Mapping[str, Any] | None = None
