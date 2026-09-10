from importlib.metadata import PackageNotFoundError, version

from .errors import AgentSdkError, ErrorCode
from .agent_state import AgentLifecycleState
from .contracts import (
    MediaOffloadAdapter,
    RemoteVideoStream,
    VideoUploadHandle,
)
from .models import (
    AgentProfile,
    DiscoveredAgent,
    GroupConfigSnapshot,
    GroupInfo,
    GroupMemberInfo,
    MessageReceipt,
    NetworkMessageAction,
    NetworkMessageType,
    NetworkAbility,
    OffloadingSession,
    OperationResult,
    ProcessedVideoEndpoint,
    SandboxSpec,
    SdkInitResult,
    VideoUploadEndpoint,
)
from .sdk import AgentSdk

try:
    __version__ = version("agent-connect-sdk")
except PackageNotFoundError:  # Source checkout without an installed distribution.
    __version__ = "0.17.5"

__all__ = [
    "AgentSdk",
    "AgentSdkError",
    "AgentProfile",
    "AgentLifecycleState",
    "DiscoveredAgent",
    "ErrorCode",
    "GroupConfigSnapshot",
    "GroupInfo",
    "GroupMemberInfo",
    "MessageReceipt",
    "MediaOffloadAdapter",
    "NetworkMessageAction",
    "NetworkMessageType",
    "NetworkAbility",
    "OffloadingSession",
    "OperationResult",
    "ProcessedVideoEndpoint",
    "SandboxSpec",
    "RemoteVideoStream",
    "SdkInitResult",
    "VideoUploadHandle",
    "VideoUploadEndpoint",
    "__version__",
]
