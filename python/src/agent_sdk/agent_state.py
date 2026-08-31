from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .errors import AgentSdkError, ErrorCode
from .models import AgentProfile


class AgentLifecycleState(str, Enum):
    """Persistent Agent business lifecycle, independent from SDK connectivity."""

    NO_IDENTITY = "NO_IDENTITY"
    IDENTITY_READY = "IDENTITY_READY"
    CARD_PUBLISHED = "CARD_PUBLISHED"


@dataclass(frozen=True, slots=True)
class IdentityApplicationContext:
    owner: str
    name: str
    description: str
    metadata: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class AgentCardContext:
    priority: int
    vc_list: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class PersistedAgentState:
    state: AgentLifecycleState
    profile: AgentProfile | None
    identity_application: IdentityApplicationContext | None = None
    agent_card: AgentCardContext | None = None


class AgentStateStore:
    """Atomic JSON persistence scoped to one AgentRuntime endpoint."""

    SCHEMA_VERSION = 2

    def __init__(self, directory: str | Path | None = None) -> None:
        if directory is None:
            xdg_state_home = os.environ.get("XDG_STATE_HOME")
            base = (
                Path(xdg_state_home).expanduser()
                if xdg_state_home
                else Path.home() / ".local" / "state"
            )
            directory = base / "agent-sdk" / "agents"
        self._directory = Path(directory).expanduser()

    @property
    def directory(self) -> Path:
        return self._directory

    def state_file(self, runtime_host: str, runtime_port: int) -> Path:
        endpoint = f"{runtime_host.strip().lower()}:{runtime_port}"
        digest = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
        return self._directory / f"{digest}.json"

    def load(
        self,
        runtime_host: str,
        runtime_port: int,
        agent_tun_ip: str,
    ) -> PersistedAgentState:
        path = self.state_file(runtime_host, runtime_port)
        if not path.exists():
            return PersistedAgentState(AgentLifecycleState.NO_IDENTITY, None)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(raw, dict)
                or raw.get("schema_version") != self.SCHEMA_VERSION
            ):
                raise ValueError("unsupported schema_version")
            runtime = raw.get("runtime")
            if not isinstance(runtime, dict):
                raise ValueError("runtime must be an object")
            if (
                runtime.get("host") != runtime_host
                or runtime.get("port") != runtime_port
            ):
                raise ValueError("Runtime endpoint does not match the state file")
            if raw.get("agent_tun_ip") != agent_tun_ip:
                raise ValueError("Agent TUN IP does not match the current PDU session")
            state = AgentLifecycleState(raw.get("state"))
            if state is AgentLifecycleState.NO_IDENTITY:
                raise ValueError("NO_IDENTITY must be represented by an absent state file")
            profile_raw = raw.get("profile")
            if not isinstance(profile_raw, dict):
                raise ValueError("profile must be an object")
            agent_id = profile_raw.get("agent_id")
            agent_name = profile_raw.get("agent_name")
            identity_vc = profile_raw.get("identity_vc")
            if not isinstance(agent_id, str) or not agent_id:
                raise ValueError("profile.agent_id must be a non-empty string")
            if not isinstance(agent_name, str) or not agent_name:
                raise ValueError("profile.agent_name must be a non-empty string")
            if not isinstance(identity_vc, Mapping):
                raise ValueError("profile.identity_vc must be an object")
            identity_application = self._load_identity_application(
                raw.get("identity_application")
            )
            agent_card = self._load_agent_card(raw.get("agent_card"))
            if identity_application is None:
                raise ValueError("identity_application must be persisted")
            if state is AgentLifecycleState.CARD_PUBLISHED and agent_card is None:
                raise ValueError("agent_card must be persisted in CARD_PUBLISHED")
            return PersistedAgentState(
                state,
                AgentProfile(agent_id, agent_name, dict(identity_vc)),
                identity_application,
                agent_card,
            )
        except AgentSdkError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AgentSdkError(
                ErrorCode.AGENT_STATE_INVALID,
                f"cannot restore Agent state from {path}: {exc}",
                details={"state_file": str(path)},
            ) from exc

    def save(
        self,
        runtime_host: str,
        runtime_port: int,
        agent_tun_ip: str,
        state: AgentLifecycleState,
        profile: AgentProfile,
        identity_application: IdentityApplicationContext,
        agent_card: AgentCardContext | None = None,
    ) -> None:
        if state is AgentLifecycleState.NO_IDENTITY:
            raise AgentSdkError(
                ErrorCode.AGENT_STATE_INVALID,
                "NO_IDENTITY cannot be saved with an Agent profile",
            )
        path = self.state_file(runtime_host, runtime_port)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        record: dict[str, Any] = {
            "schema_version": self.SCHEMA_VERSION,
            "runtime": {"host": runtime_host, "port": runtime_port},
            "agent_tun_ip": agent_tun_ip,
            "state": state.value,
            "profile": {
                "agent_id": profile.agent_id,
                "agent_name": profile.agent_name,
                "identity_vc": dict(profile.identity_vc),
            },
            "identity_application": {
                "owner": identity_application.owner,
                "name": identity_application.name,
                "description": identity_application.description,
                "metadata": dict(identity_application.metadata),
            },
        }
        if agent_card is not None:
            record["agent_card"] = {
                "priority": agent_card.priority,
                "vc_list": [dict(item) for item in agent_card.vc_list],
            }
        try:
            self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self._directory, 0o700)
            with temporary.open("x", encoding="utf-8") as handle:
                os.chmod(temporary, 0o600)
                json.dump(
                    record,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except (OSError, TypeError, ValueError) as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise AgentSdkError(
                ErrorCode.AGENT_STATE_PERSISTENCE_FAILED,
                f"cannot persist Agent state to {path}: {exc}",
                details={"state_file": str(path)},
            ) from exc

    def clear(self, runtime_host: str, runtime_port: int) -> None:
        path = self.state_file(runtime_host, runtime_port)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise AgentSdkError(
                ErrorCode.AGENT_STATE_PERSISTENCE_FAILED,
                f"cannot clear Agent state file {path}: {exc}",
                details={"state_file": str(path)},
            ) from exc

    @staticmethod
    def _load_identity_application(raw: Any) -> IdentityApplicationContext | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("identity_application must be an object")
        owner = raw.get("owner")
        name = raw.get("name")
        description = raw.get("description")
        metadata = raw.get("metadata")
        if not all(isinstance(item, str) and item for item in (owner, name, description)):
            raise ValueError("identity_application strings must be non-empty")
        if not isinstance(metadata, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in metadata.items()
        ):
            raise ValueError("identity_application.metadata must contain strings")
        return IdentityApplicationContext(owner, name, description, dict(metadata))

    @staticmethod
    def _load_agent_card(raw: Any) -> AgentCardContext | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("agent_card must be an object")
        priority = raw.get("priority")
        vc_list = raw.get("vc_list")
        if not isinstance(priority, int):
            raise ValueError("agent_card.priority must be an integer")
        if not isinstance(vc_list, list) or not vc_list or not all(
            isinstance(item, dict) for item in vc_list
        ):
            raise ValueError("agent_card.vc_list must contain objects")
        return AgentCardContext(priority, tuple(dict(item) for item in vc_list))
