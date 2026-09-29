"""ACN/Unitree machine-dog agent for the campus patrol case.

The program is the machine-dog side of the demo flow:

* register a digital identity and capabilities (``robot dog``, ``patrol`` and
  ``camera``) with AgentRuntime;
* accept a group invitation from the AR glasses;
* receive a confirmed patrol request over A2A;
* optionally create a ``dog-vision`` computing session and upload camera video;
* forward a danger alert to the glasses; and
* map confirmed voice commands to Unitree high-level actions.

The application protocol intentionally stays small and explicit. A glasses
agent sends payloads such as::

    {"type": "patrol_request", "zone": "A", "confirmed": true}
    {"type": "dog_command", "command": "intimidate", "confirmed": true}
    {"type": "dog_command", "command": "expel", "confirmed": true}

``expel`` is deliberately confirmation-gated because it invokes the
high-risk Unitree ``FrontPounce`` action.

Unitree's official Python SDK exposes ``SportClient.Hello()`` and
``SportClient.FrontPounce()``. The direct adapter below calls those methods;
the HTTP adapter targets the sibling ``compute_backend/dog-ctrl`` service so
the same orchestration can be used when DDS control is kept in a separate
process.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

try:  # Running from an installed wheel.
    from agent_sdk import (
        AcnContext,
        AgentSdk,
        ComputeConstraints,
        ComputeInputFormat,
        ComputeRequestType,
        ComputeResources,
        ComputeSessionRequest,
        NetworkMessageAction,
        NetworkMessageType,
    )
except (ImportError, ModuleNotFoundError):  # Keep --dry-run dependency-free.
    # Direct execution from a source checkout may not have the SDK's optional
    # cryptography/aioquic dependencies installed. The real classes are loaded
    # by bootstrap/_run_video_offload when the networked mode is selected.
    AgentSdk = Any  # type: ignore[misc,assignment]
    AcnContext = ComputeConstraints = ComputeInputFormat = ComputeRequestType = Any  # type: ignore[misc,assignment]
    ComputeResources = ComputeSessionRequest = Any  # type: ignore[misc,assignment]

    class NetworkMessageType(str, Enum):
        GROUP_INVITATION = "GROUP_INVITATION"
        GROUP_CONFIG = "GROUP_CONFIG"
        UNKNOWN = "UNKNOWN"

    class NetworkMessageAction(str, Enum):
        ACCEPT = "ACCEPT"
        REJECT = "REJECT"
        ACK = "ACK"


LOGGER = logging.getLogger("dog-patrol-agent")
COMPUTE_SESSION_ID_FIELD = "compute_service_session_id"
DEFAULT_CAPABILITIES = (
    "robot dog",
    "patrol",
    "camera",
    "巡逻",
    "相机",
    "danger-response",
    "危险响应",
    "dog-vision",
)


class DogActions(Protocol):
    async def stand_up(self) -> Mapping[str, Any]: ...

    async def forward(self) -> Mapping[str, Any]: ...

    async def back(self) -> Mapping[str, Any]: ...

    async def left(self) -> Mapping[str, Any]: ...

    async def right(self) -> Mapping[str, Any]: ...

    async def intimidate(self) -> Mapping[str, Any]: ...

    async def expel(self) -> Mapping[str, Any]: ...

    async def stop(self) -> Mapping[str, Any]: ...

    async def close(self) -> None: ...


class DryRunDogActions:
    """Deterministic action adapter for a laptop or CI without a robot."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def stand_up(self) -> Mapping[str, Any]:
        return await self._record("StandUp")

    async def _record(self, action: str) -> Mapping[str, Any]:
        self.calls.append(action)
        return {"ok": True, "command": action, "dry_run": True}

    async def forward(self) -> Mapping[str, Any]:
        return await self._record("forward")

    async def back(self) -> Mapping[str, Any]:
        return await self._record("back")

    async def left(self) -> Mapping[str, Any]:
        return await self._record("left")

    async def right(self) -> Mapping[str, Any]:
        return await self._record("right")

    async def intimidate(self) -> Mapping[str, Any]:
        return await self._record("Hello")

    async def expel(self) -> Mapping[str, Any]:
        return await self._record("FrontPounce")

    async def stop(self) -> Mapping[str, Any]:
        return await self._record("StopMove")

    async def close(self) -> None:
        return None


class UnitreeGo2Actions:
    """Direct high-level Go2 control through Unitree's official Python SDK."""

    def __init__(
        self,
        *,
        interface: str | None = None,
        step_seconds: float = 1.0,
        speed_mps: float = 0.15,
        yaw_rate_radps: float = 0.45,
        action_hold_seconds: float = 4.0,
    ) -> None:
        self.interface = interface
        self.step_seconds = step_seconds
        self.speed_mps = speed_mps
        self.yaw_rate_radps = yaw_rate_radps
        self.action_hold_seconds = action_hold_seconds
        self._client: Any | None = None

    def _start(self) -> None:
        if self._client is not None:
            return
        try:
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
            from unitree_sdk2py.go2.sport.sport_client import SportClient
        except Exception as exc:  # pragma: no cover - hardware dependency
            raise RuntimeError(
                "unitree_sdk2py is required for --action-backend unitree; "
                "install unitree_sdk2_python and CycloneDDS"
            ) from exc
        ChannelFactoryInitialize(0, self.interface)
        client = SportClient()
        client.SetTimeout(10.0)
        client.Init()
        self._client = client

    async def _call(self, method: str, *args: Any) -> Mapping[str, Any]:
        self._start()
        assert self._client is not None
        code = await asyncio.to_thread(getattr(self._client, method), *args)
        if code != 0:
            raise RuntimeError(f"Unitree {method} failed with code {code}")
        return {"ok": True, "command": method, "sdk_code": code}

    async def _velocity_step(self, action: str, vx: float, vyaw: float) -> Mapping[str, Any]:
        self._start()
        assert self._client is not None
        deadline = asyncio.get_running_loop().time() + self.step_seconds
        try:
            while asyncio.get_running_loop().time() < deadline:
                code = await asyncio.to_thread(self._client.Move, vx, 0.0, vyaw)
                if code != 0:
                    raise RuntimeError(f"Unitree Move failed with code {code}")
                await asyncio.sleep(0.1)
        finally:
            await asyncio.to_thread(self._client.StopMove)
        return {"ok": True, "command": action}

    async def forward(self) -> Mapping[str, Any]:
        return await self._velocity_step("forward", self.speed_mps, 0.0)

    async def back(self) -> Mapping[str, Any]:
        return await self._velocity_step("back", -self.speed_mps, 0.0)

    async def left(self) -> Mapping[str, Any]:
        return await self._velocity_step("left", 0.0, abs(self.yaw_rate_radps))

    async def right(self) -> Mapping[str, Any]:
        return await self._velocity_step("right", 0.0, -abs(self.yaw_rate_radps))

    async def stand_up(self) -> Mapping[str, Any]:
        return await self._call("StandUp")

    async def intimidate(self) -> Mapping[str, Any]:
        await self._call("StopMove")
        result = await self._call("Hello")
        await asyncio.sleep(self.action_hold_seconds)
        return result

    async def expel(self) -> Mapping[str, Any]:
        await self._call("StopMove")
        # Official Unitree API: SportClient.FrontPounce(), no parameters.
        return await self._call("FrontPounce")

    async def stop(self) -> Mapping[str, Any]:
        return await self._call("StopMove")

    async def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self.stop()
        self._client = None


class HttpDogActions:
    """Adapter for ``/root/lpx/compute_backend/dog-ctrl``."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._session: Any | None = None

    async def _post(self, path: str) -> Mapping[str, Any]:
        if self._session is None:
            import aiohttp

            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds)
            )
        async with self._session.post(f"{self.base_url}{path}") as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(f"dog-ctrl {path} returned HTTP {response.status}: {body}")
            return body

    async def forward(self) -> Mapping[str, Any]:
        return await self._post("/forward")

    async def stand_up(self) -> Mapping[str, Any]:
        return await self._post("/stand-up")

    async def back(self) -> Mapping[str, Any]:
        return await self._post("/back")

    async def left(self) -> Mapping[str, Any]:
        return await self._post("/left")

    async def right(self) -> Mapping[str, Any]:
        return await self._post("/right")

    async def intimidate(self) -> Mapping[str, Any]:
        return await self._post("/wave")

    async def expel(self) -> Mapping[str, Any]:
        return await self._post("/front-pounce")

    async def stop(self) -> Mapping[str, Any]:
        return await self._post("/stop")

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


@dataclass(frozen=True)
class DogPatrolConfig:
    runtime_ip: str
    runtime_port: int
    local_tcp_port: int
    local_udp_port: int
    masque_url: str
    masque_authorization: str | None
    owner: str
    agent_name: str
    description: str = "Unitree Go2 campus patrol agent"
    region: str = "campus"
    dnn: str = "internet"
    priority: int = 10
    capabilities: tuple[str, ...] = DEFAULT_CAPABILITIES
    compute_capability_id: str = "dog-vision"
    compute_cpu_millicores: int = 2000
    compute_memory_mib: int = 4096
    camera_id: int = 0
    log_file: str = "./logs/dog-patrol-agent.log"
    log_level: str = "INFO"


class DogPatrolAgent:
    """Machine-dog orchestration and the A2A message protocol."""

    def __init__(self, sdk: AgentSdk, actions: DogActions, config: DogPatrolConfig) -> None:
        self.sdk = sdk
        self.actions = actions
        self.config = config
        self.profile: Any | None = None
        self.group_id: str | None = None
        self.controller_agent_id: str | None = None
        self.current_task_id: str | None = None
        self.current_zone: str | None = None
        self._patrol_task: asyncio.Task[None] | None = None
        self._offload_task: asyncio.Task[None] | None = None
        self._offload_upload: Any | None = None
        self._offload_session_id: str | None = None
        self._producer_upload_task: asyncio.Task[None] | None = None
        self._producer_session_id: str | None = None
        self._closed = False
        self._seen_commands: set[str] = set()
        self._unregister_network: Any | None = None
        self._unregister_group: Any | None = None

    async def bootstrap(self) -> Any:
        """Run identity, network ability, and capability registration."""
        self._unregister_network = self.sdk.register_network_message_listener(self)
        self._unregister_group = self.sdk.register_group_message_listener(self)
        await self.sdk.init(
            self.config.runtime_ip,
            self.config.runtime_port,
            local_tcp_port=self.config.local_tcp_port,
            local_udp_port=self.config.local_udp_port,
            masque_server_url=self.config.masque_url,
            masque_authorization=self.config.masque_authorization,
            log_file_path=self.config.log_file,
            log_level=self.config.log_level,
        )
        self.profile = await self.sdk.apply_identity(
            owner=self.config.owner,
            name=self.config.agent_name,
            description=self.config.description,
            metadata={"region": self.config.region, "device": "unitree-go2", "role": "robot dog"},
        )
        ability = await self.sdk.get_network_ability(self.profile.agent_id)
        await self.sdk.register_capabilities(
            self.profile.agent_id,
            priority=self.config.priority,
            credentials=[ability.ability_vc],
            capabilities=self.config.capabilities,
            agent_name=self.profile.agent_name,
        )
        self._emit("READY", agent_id=self.profile.agent_id, capabilities=self.config.capabilities)
        return self.profile

    async def on_network_message(self, message_type: NetworkMessageType, payload: Mapping[str, Any]) -> NetworkMessageAction:
        if message_type is NetworkMessageType.GROUP_INVITATION:
            self._emit("GROUP_INVITATION_ACCEPTED", group_id=payload.get("group_id"), payload=dict(payload))
            return NetworkMessageAction.ACCEPT
        if message_type is NetworkMessageType.GROUP_CONFIG:
            self._emit("GROUP_CONFIG_APPLIED", group_id=payload.get("group_id"), payload=dict(payload))
            return NetworkMessageAction.ACK
        return NetworkMessageAction.REJECT

    async def on_group_message(self, group_id: str, sender_agent_id: str, payload: Mapping[str, Any]) -> None:
        """Handle a normalized payload received through the secure group."""
        self.group_id = group_id
        if COMPUTE_SESSION_ID_FIELD in payload:
            await self._handle_compute_session_notification(group_id, sender_agent_id, payload)
            return
        kind = str(payload.get("type") or payload.get("event") or "").strip().lower()
        if kind in {"patrol_request", "dispatch_patrol", "patrol_start"}:
            await self._handle_patrol_request(group_id, sender_agent_id, payload)
        elif kind in {"dog_command", "command", "patrol_command", "robot_action"}:
            await self._handle_dog_command(group_id, sender_agent_id, payload)
        elif kind in {"danger_detected", "vision_alert"}:
            await self.notify_danger(
                object_name=str(payload.get("object") or payload.get("label") or "unknown threat"),
                confidence=payload.get("confidence"),
                evidence=payload.get("evidence"),
                task_id=str(payload.get("task_id") or self.current_task_id or ""),
            )
        elif kind in {"patrol_stop", "stop_patrol"}:
            await self.stop_patrol(reason=str(payload.get("reason") or "remote_stop"))
        else:
            self._emit("MESSAGE_IGNORED", group_id=group_id, sender_agent_id=sender_agent_id, payload=dict(payload))

    async def _handle_compute_session_notification(
        self,
        group_id: str,
        sender_agent_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Consume the Android/RayNeo producer notification format.

        The glasses agent owns the CREATE request.  The dog is the producer and
        must only start ``start_video_upload`` after receiving the session ID.
        """
        session_id = payload.get(COMPUTE_SESSION_ID_FIELD)
        if not isinstance(session_id, str) or not session_id:
            await self._send_event(
                group_id,
                sender_agent_id,
                {"type": "video_offload_failed", "reason": "invalid_compute_service_session_id"},
            )
            return
        if self._producer_session_id == session_id and self._producer_upload_task is not None:
            return
        self.controller_agent_id = sender_agent_id
        self._producer_session_id = session_id
        if self.current_task_id is None:
            self.current_task_id = f"patrol-{group_id}"
            self.current_zone = "A区域"
        if self._patrol_task is None or self._patrol_task.done():
            route = ["forward", "left", "forward", "right"]
            self._patrol_task = asyncio.create_task(
                self._run_patrol(self.current_task_id, self.current_zone or "A区域", route)
            )
        self._producer_upload_task = asyncio.create_task(
            self._run_producer_upload(group_id, sender_agent_id, session_id)
        )

    async def _run_producer_upload(
        self,
        group_id: str,
        controller_agent_id: str,
        session_id: str,
    ) -> None:
        """Start the producer side requested by the RayNeo Agent B flow."""
        try:
            self._offload_upload = await self.sdk.start_video_upload(
                session_id,
                camera_id=self.config.camera_id,
                width=1280,
                height=720,
                fps=30,
                bitrate_kbps=2500,
            )
            await self._send_event(
                group_id,
                controller_agent_id,
                {
                    "type": "video_upload_started",
                    COMPUTE_SESSION_ID_FIELD: session_id,
                    "task_id": self.current_task_id or f"patrol-{group_id}",
                },
                task_id=self.current_task_id or f"patrol-{group_id}",
            )
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Producer video upload failed")
            await self._send_event(
                group_id,
                controller_agent_id,
                {"type": "video_offload_failed", "reason": str(exc)},
            )
        finally:
            if self._offload_upload is not None:
                with contextlib.suppress(Exception):
                    await self._offload_upload.stop()
                self._offload_upload = None
            self._producer_session_id = None

    async def _handle_patrol_request(self, group_id: str, sender_agent_id: str, payload: Mapping[str, Any]) -> None:
        confirmed = bool(payload.get("confirmed", payload.get("user_confirmed", False)))
        task_id = str(payload.get("task_id") or uuid.uuid4())
        if not confirmed:
            await self._send_event(group_id, sender_agent_id, {"type": "patrol_confirmation_required", "task_id": task_id})
            return
        if self._patrol_task is not None and not self._patrol_task.done():
            await self._send_event(group_id, sender_agent_id, {"type": "patrol_rejected", "task_id": task_id, "reason": "busy"})
            return
        try:
            await self.actions.stand_up()
        except Exception as exc:
            LOGGER.exception("Unable to stand robot before patrol")
            await self._send_event(
                group_id,
                sender_agent_id,
                {"type": "patrol_rejected", "task_id": task_id, "reason": "stand_up_failed", "detail": str(exc)},
                task_id=task_id,
            )
            return
        self.controller_agent_id = sender_agent_id
        self.current_task_id = task_id
        self.current_zone = str(payload.get("zone") or payload.get("target_area") or "A")
        self._emit("PATROL_ACCEPTED", task_id=task_id, zone=self.current_zone)
        await self._send_event(
            group_id,
            sender_agent_id,
            {"type": "patrol_accepted", "task_id": task_id, "zone": self.current_zone, "dog_id": self._agent_id},
            task_id=task_id,
        )
        route = payload.get("route")
        route_steps = route if isinstance(route, list) else ["forward", "left", "forward", "right"]
        self._patrol_task = asyncio.create_task(self._run_patrol(task_id, self.current_zone, route_steps))
        compute_agent_id = payload.get("compute_agent_id")
        if isinstance(compute_agent_id, str) and compute_agent_id:
            self._offload_task = asyncio.create_task(
                self._run_video_offload(group_id, sender_agent_id, compute_agent_id, task_id)
            )

    async def _run_patrol(self, task_id: str, zone: str, route_steps: Sequence[Any]) -> None:
        methods = {"forward": self.actions.forward, "back": self.actions.back, "left": self.actions.left, "right": self.actions.right}
        try:
            for index, raw_step in enumerate(route_steps):
                step = raw_step.get("action") if isinstance(raw_step, Mapping) else raw_step
                method = methods.get(str(step).strip().lower())
                if method is None:
                    raise ValueError(f"unsupported patrol step: {step!r}")
                result = await method()
                self._emit("PATROL_PROGRESS", task_id=task_id, zone=zone, step=index, action=str(step), result=dict(result))
                if self.controller_agent_id and self.group_id:
                    await self._send_event(
                        self.group_id,
                        self.controller_agent_id,
                        {"type": "patrol_progress", "task_id": task_id, "zone": zone, "step": index, "action": str(step)},
                        task_id=task_id,
                    )
            await self._send_event(self.group_id or "", self.controller_agent_id or "", {"type": "patrol_completed", "task_id": task_id, "zone": zone}, task_id=task_id)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.actions.stop()
            raise
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Patrol failed")
            await self._send_event(self.group_id or "", self.controller_agent_id or "", {"type": "patrol_failed", "task_id": task_id, "reason": str(exc)}, task_id=task_id)

    async def _handle_dog_command(self, group_id: str, sender_agent_id: str, payload: Mapping[str, Any]) -> None:
        command = str(payload.get("command") or payload.get("action") or "").strip().lower()
        aliases = {
            "威吓歹徒": "intimidate",
            "威吓": "intimidate",
            "intimidate": "intimidate",
            "wave": "intimidate",
            "scrape": "intimidate",
            "驱逐歹徒": "expel",
            "驱逐": "expel",
            "expel": "expel",
            "front_pounce": "expel",
            "frontpounce": "expel",
        }
        normalized = aliases.get(command)
        command_id = str(payload.get("command_id") or payload.get("request_id") or f"{sender_agent_id}:{command}:{self.current_task_id}")
        if command_id in self._seen_commands:
            return
        if normalized is None:
            self._seen_commands.add(command_id)
            await self._send_event(group_id, sender_agent_id, {"type": "command_rejected", "command_id": command_id, "reason": "unsupported_command"})
            return
        confirmed = bool(payload.get("confirmed", payload.get("user_confirmed", False)))
        # The RayNeo UI performs a second confirmation before sending the
        # high-risk action and carries that decision as ``risk=high``.
        if command_kind := str(payload.get("type") or "").strip().lower():
            if command_kind == "robot_action" and payload.get("risk") in {"medium", "high"}:
                confirmed = True
        if not confirmed:
            await self._send_event(group_id, sender_agent_id, {"type": "command_confirmation_required", "command_id": command_id, "command": normalized})
            return
        self._seen_commands.add(command_id)
        await self.stop_patrol(reason=f"command:{normalized}")
        method = self.actions.intimidate if normalized == "intimidate" else self.actions.expel
        try:
            result = await method()
            self._emit("DOG_ACTION_COMPLETED", command=normalized, command_id=command_id, result=dict(result))
            await self._send_event(group_id, sender_agent_id, {"type": "dog_action_completed", "command_id": command_id, "command": normalized, "result": dict(result)})
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Dog command failed")
            await self._send_event(group_id, sender_agent_id, {"type": "dog_action_failed", "command_id": command_id, "command": normalized, "reason": str(exc)})

    async def notify_danger(self, *, object_name: str, confidence: Any = None, evidence: Any = None, task_id: str = "") -> None:
        """Send a vision result to the AR controller without taking action."""
        if not self.group_id or not self.controller_agent_id:
            return
        payload: dict[str, Any] = {"type": "danger_alert", "task_id": task_id, "object": object_name, "severity": "high"}
        if confidence is not None:
            payload["confidence"] = confidence
        if evidence is not None:
            payload["evidence"] = evidence
        self._emit("DANGER_ALERT", **payload)
        await self._send_event(self.group_id, self.controller_agent_id, payload, task_id=task_id or self.current_task_id or "danger-alert")

    async def stop_patrol(self, *, reason: str = "stop") -> None:
        if self._patrol_task is not None and not self._patrol_task.done():
            self._patrol_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._patrol_task
        self._patrol_task = None
        if self._offload_task is not None and not self._offload_task.done():
            self._offload_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._offload_task
        self._offload_task = None
        if self._producer_upload_task is not None and not self._producer_upload_task.done():
            self._producer_upload_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._producer_upload_task
        self._producer_upload_task = None
        with contextlib.suppress(Exception):
            await self.actions.stop()
        if self.current_task_id:
            self._emit("PATROL_STOPPED", task_id=self.current_task_id, reason=reason)

    async def _run_video_offload(self, group_id: str, controller_agent_id: str, compute_agent_id: str, task_id: str) -> None:
        """Create the formal SDK session and keep the producer upload alive."""
        if self.profile is None:
            return
        from agent_sdk import (  # imported lazily so --dry-run needs no SDK dependencies
            AcnContext as SdkAcnContext,
            ComputeConstraints as SdkComputeConstraints,
            ComputeInputFormat as SdkComputeInputFormat,
            ComputeRequestType as SdkComputeRequestType,
            ComputeResources as SdkComputeResources,
            ComputeSessionRequest as SdkComputeSessionRequest,
        )
        request_id = f"{task_id}-vision"
        try:
            status = await self.sdk.create_computing_session(
                SdkComputeSessionRequest(
                    message_type="COMPUTE_SESSION_REQUEST",
                    request_type=SdkComputeRequestType.CREATE,
                    input_format=SdkComputeInputFormat.STRUCTURED,
                    request_id=request_id,
                    acn_context=SdkAcnContext(group_id=group_id, requester_agent_id=self.profile.agent_id, target_agent_id=compute_agent_id),
                    constraints=SdkComputeConstraints(
                        capability_id=self.config.compute_capability_id,
                        resources=SdkComputeResources(cpu_millicores=self.config.compute_cpu_millicores, memory_mib=self.config.compute_memory_mib),
                        dnn=self.config.dnn,
                        allow_base_qos=True,
                    ),
                ),
            )
            session_id = status.compute_service_session_id
            if not session_id:
                raise RuntimeError(f"vision session was not created: {status.status}/{status.cause}")
            self._offload_session_id = session_id
            self._emit("COMPUTING_SESSION_CREATED", task_id=task_id, session_id=session_id)
            await self._send_event(group_id, controller_agent_id, {"type": "video_offload_started", COMPUTE_SESSION_ID_FIELD: session_id, "task_id": task_id}, task_id=task_id)
            self._offload_upload = await self.sdk.start_video_upload(session_id, camera_id=self.config.camera_id, width=1280, height=720, fps=30, bitrate_kbps=2500)
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Video offload failed")
            await self._send_event(group_id, controller_agent_id, {"type": "video_offload_failed", "task_id": task_id, "reason": str(exc)}, task_id=task_id)
        finally:
            if self._offload_upload is not None:
                with contextlib.suppress(Exception):
                    await self._offload_upload.stop()
                self._offload_upload = None
            if self._offload_session_id is not None:
                with contextlib.suppress(Exception):
                    await self.sdk.release_computing_session(
                        SdkComputeSessionRequest(
                            message_type="COMPUTE_SESSION_REQUEST",
                            request_type=SdkComputeRequestType.RELEASE,
                            input_format=SdkComputeInputFormat.STRUCTURED,
                            request_id=f"{task_id}-vision-release",
                            compute_service_session_id=self._offload_session_id,
                        )
                    )
                with contextlib.suppress(Exception):
                    await self.sdk.await_computing_session_closed(self._offload_session_id)
                self._offload_session_id = None

    async def _send_event(self, group_id: str, target_agent_id: str, payload: Mapping[str, Any], *, task_id: str | None = None) -> None:
        if not group_id or not target_agent_id:
            return
        try:
            await self.sdk.send_message(group_id, target_agent_id, payload, message_type="dog-event.v1", task_id=task_id or self.current_task_id or "dog-event")
        except Exception:  # noqa: BLE001
            LOGGER.exception("Failed to send A2A event")

    @property
    def _agent_id(self) -> str:
        return str(getattr(self.profile, "agent_id", "dog-local"))

    def _emit(self, event: str, **fields: Any) -> None:
        print(json.dumps({"agent": "dog", "event": event, **fields}, ensure_ascii=False, default=str), flush=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.stop_patrol(reason="shutdown")
        if self._offload_task is not None and not self._offload_task.done():
            self._offload_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._offload_task
        if self._unregister_network:
            self._unregister_network()
        if self._unregister_group:
            self._unregister_group()
        with contextlib.suppress(Exception):
            await self.actions.close()
        with contextlib.suppress(Exception):
            await self.sdk.close()


class _DryRunSdk:
    def __init__(self) -> None:
        self.events: list[Mapping[str, Any]] = []

    async def send_message(self, group_id: str, target_agent_id: str, payload: Mapping[str, Any], **_: Any) -> None:
        self.events.append({"group_id": group_id, "target_agent_id": target_agent_id, **dict(payload)})

    async def close(self) -> None:
        return None


async def run_dry_run() -> None:
    actions = DryRunDogActions()
    agent = DogPatrolAgent(
        _DryRunSdk(),
        actions,
        DogPatrolConfig("dry-run", 0, 0, 0, "https://dry-run.invalid", None, "demo", "D-1"),
    )
    agent.profile = type("Profile", (), {"agent_id": "dog-d1", "agent_name": "D-1"})()
    await agent.on_group_message("secure-domain-demo", "glasses-001", {"type": "patrol_request", "task_id": "task-a", "zone": "A", "confirmed": True, "route": ["forward", "left"]})
    await agent._patrol_task
    await agent.on_group_message("secure-domain-demo", "glasses-001", {"type": "danger_detected", "object": "knife-wielding-person", "confidence": 0.97})
    await agent.on_group_message("secure-domain-demo", "glasses-001", {"type": "dog_command", "command": "intimidate", "confirmed": True, "command_id": "c-1"})
    await agent.on_group_message("secure-domain-demo", "glasses-001", {"type": "dog_command", "command": "expel", "confirmed": True, "command_id": "c-2"})
    print(json.dumps({"dry_run_actions": actions.calls}, ensure_ascii=False))
    await agent.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ACN campus-patrol machine-dog agent")
    parser.add_argument("--dry-run", action="store_true", help="run the complete story with simulated Unitree actions")
    parser.add_argument("--action-backend", choices=("unitree", "http"), default="unitree")
    parser.add_argument("--dog-ctrl-url", default="http://127.0.0.1:8080")
    parser.add_argument("--unitree-interface", default=os.getenv("DOG_CTRL_INTERFACE"))
    parser.add_argument("--runtime-ip", required=False, default=os.getenv("ACN_RUNTIME_IP", "127.0.0.1"))
    parser.add_argument("--runtime-port", type=int, default=int(os.getenv("ACN_RUNTIME_PORT", "8080")))
    parser.add_argument("--local-tcp-port", type=int, default=4002)
    parser.add_argument("--local-udp-port", type=int, default=28444)
    parser.add_argument("--masque-url", default=os.getenv("ACN_MASQUE_URL", "https://127.0.0.1:4433"))
    parser.add_argument("--masque-token", default=os.getenv("ACN_MASQUE_TOKEN"))
    parser.add_argument("--owner", default="campus-security")
    parser.add_argument("--agent-name", default="D-1")
    parser.add_argument("--camera-id", type=int, default=0, help="V4L2 camera index used by SDK video upload")
    parser.add_argument("--log-file", default="./logs/dog-patrol-agent.log")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--stay-running", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> None:
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s [dog] %(message)s")
    if args.dry_run:
        await run_dry_run()
        return
    sdk = AgentSdk()
    actions: DogActions = UnitreeGo2Actions(interface=args.unitree_interface) if args.action_backend == "unitree" else HttpDogActions(args.dog_ctrl_url)
    config = DogPatrolConfig(
        runtime_ip=args.runtime_ip,
        runtime_port=args.runtime_port,
        local_tcp_port=args.local_tcp_port,
        local_udp_port=args.local_udp_port,
        masque_url=args.masque_url,
        masque_authorization=f"Bearer {args.masque_token}" if args.masque_token else None,
        owner=args.owner,
        agent_name=args.agent_name,
        camera_id=args.camera_id,
        log_file=args.log_file,
        log_level=args.log_level.upper(),
    )
    agent = DogPatrolAgent(sdk, actions, config)
    try:
        await agent.bootstrap()
        if args.stay_running:
            await asyncio.Event().wait()
    finally:
        await agent.close()


def main() -> None:
    asyncio.run(_run(_build_parser().parse_args()))


if __name__ == "__main__":
    main()
