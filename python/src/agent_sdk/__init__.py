from importlib.metadata import PackageNotFoundError, version

from .errors import AgentSdkError, ErrorCode
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
    SdkInitResult,
)
from .sdk import AgentSdk

try:
    __version__ = version("agent-connect-sdk")
except PackageNotFoundError:  # Source checkout without an installed distribution.
    __version__ = "0.14.0"

__all__ = [
    "AgentSdk",
    "AgentSdkError",
    "AgentProfile",
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
    "RemoteVideoStream",
    "SdkInitResult",
    "VideoUploadHandle",
    "__version__",
]
