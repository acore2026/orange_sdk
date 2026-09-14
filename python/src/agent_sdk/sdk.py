from __future__ import annotations

import asyncio
import functools
import inspect
import ipaddress
import logging
import socket
import time
import uuid
from dataclasses import dataclass
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

from .agent_state import (
    AgentCardContext,
    AgentLifecycleState,
    AgentStateStore,
    IdentityApplicationContext,
)
from .capability_vc import TEST_CAPABILITY_ISSUER_DID, issue_test_capability_vcs
from .config import SdkConfig
from .contracts import (
    ConnectIpTransport,
    ControlRequestAuthenticator,
    GroupMessageListener,
    LocalServer,
    MediaOffloadAdapter,
    MessageSignatureVerifier,
    MessageSigner,
    NetworkMessageListener,
    PeerMessenger,
    ProofVerifier,
    SandboxTransport,
    RuntimeTransport,
    TunDevice,
    RemoteVideoStream,
    RuntimeHttpResponse,
    VideoUploadHandle,
)
from .errors import AgentSdkError, ErrorCode
from .group_cache import GroupMemberCache
from .logging_utils import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_FILE_PATH,
    DEFAULT_LOG_LEVEL,
    DEFAULT_LOG_MAX_BYTES,
    close_logger,
    configure_local_logger,
    log_event,
)
from .masque import AioquicConnectIpTransport
from .models import (
    AgentProfile,
    AcnContext,
    ComputeConnectionParameters,
    ComputeConstraints,
    ComputeInputFormat,
    ComputeNetworkBinding,
    ComputeRequestType,
    ComputeResources,
    ComputeRole,
    ComputeSessionRequest,
    ComputeSessionStatus,
    ComputingContext,
    ComputingSession,
    ControlAction,
    ControlActionRequest,
    ControlActionStatus,
    ControlInputType,
    ControlTargetRole,
    DiscoveredAgent,
    GroupConfigSnapshot,
    GroupInfo,
    MessageReceipt,
    NetworkAbility,
    NetworkMessageAction,
    NetworkMessageType,
    OperationResult,
    RecognitionTarget,
    RecognitionTargetStatus,
    RuntimeDataPlane,
    SdkInitResult,
    Snssai,
)
from .rest_server import AiohttpLocalServer
from .routes import GroupRouteManager, Pyroute2RouteBackend, RouteBackend
from .runtime import HttpPeerMessenger, HttpRuntimeTransport, HttpSandboxTransport
from .security import (
    DeviceControlRequestAuthenticator,
    DeviceMessageSigner,
    DeviceSigningIdentityStore,
    DisabledMessageSignatureVerifier,
    DisabledProofVerifier,
)
from .tun import LinuxTunDevice, validate_ip_packet

_COMPUTING_SESSION_REQUEST_PATH = "/v1/computing/session-requests"
_COMPUTE_CONNECT_CONFIG = "COMPUTE_CONNECT_CONFIG"
_COMPUTE_SESSION_STATUS = "COMPUTE_SESSION_STATUS"
_COMPUTE_SESSION_CLOSE = "COMPUTE_SESSION_CLOSE"
_COMPUTE_TERMINAL_STATUSES = {
    "REJECTED",
    "CLARIFICATION_REQUIRED",
    "REQUEST_CANCELLED",
    "NOT_FOUND",
    "FAILED",
    "COMPLETED",
}
_COMPUTE_STATUSES = {
    "ACCEPTED",
    "WAITING_PARTICIPANTS",
    "PLANNING",
    "COORDINATING",
    "RESERVED",
    "ACTIVATING",
    "MEDIA_CONNECTING",
    "ACTIVE",
    "RELEASING",
    "COMPENSATING",
    "CLEANUP_FAILED",
    "FAILED",
    "COMPLETED",
    "REJECTED",
    "CLARIFICATION_REQUIRED",
    "REQUEST_CANCELLED",
    "NOT_FOUND",
}

TunFactory = Callable[[str, str, int], Awaitable[TunDevice]]
MasqueFactory = Callable[[SdkConfig], ConnectIpTransport]
RuntimeFactory = Callable[[str, int], RuntimeTransport]
ServerFactory = Callable[[], LocalServer]
RouteBackendFactory = Callable[[SdkConfig, TunDevice], RouteBackend]


class _ManagedVideoUpload:
    def __init__(self, inner: VideoUploadHandle, close_remote: Callable[[], Awaitable[None]]):
        self._inner = inner
        self._close_remote = close_remote
        self._local_stopped = False
        self._remote_closed = False

    @property
    def track_id(self) -> str:
        return self._inner.track_id

    @property
    def state(self) -> str:
        return self._inner.state

    async def pause(self) -> None:
        await self._inner.pause()

    async def resume(self) -> None:
        await self._inner.resume()

    async def stop(self) -> None:
        if self._local_stopped and self._remote_closed:
            return
        local_error: BaseException | None = None
        if not self._local_stopped:
            try:
                await self._inner.stop()
                self._local_stopped = True
            except BaseException as exc:
                local_error = exc
        if not self._remote_closed:
            await self._close_remote()
            self._remote_closed = True
        if local_error is not None:
            raise local_error


class _ManagedRemoteVideoStream:
    def __init__(self, inner: RemoteVideoStream, close_remote: Callable[[], Awaitable[None]]):
        self._inner = inner
        self._close_remote = close_remote
        self._local_closed = False
        self._remote_closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._inner.__anext__()

    async def recv(self) -> Any:
        return await self._inner.recv()

    async def close(self) -> None:
        if self._local_closed and self._remote_closed:
            return
        local_error: BaseException | None = None
        if not self._local_closed:
            try:
                await self._inner.close()
                self._local_closed = True
            except BaseException as exc:
                local_error = exc
        if not self._remote_closed:
            await self._close_remote()
            self._remote_closed = True
        if local_error is not None:
            raise local_error


@dataclass(slots=True)
class _MediaConnection:
    session: ComputingSession
    request_id: str
    media_connection_id: str
    local: VideoUploadHandle | RemoteVideoStream
    managed: VideoUploadHandle | RemoteVideoStream


@dataclass(slots=True)
class _PendingMediaConnection:
    session: ComputingSession
    request_id: str
    offer_sdp: str
    prepared: Any

def _bound_arguments(function, instance, args, kwargs) -> dict[str, Any]:
    try:
        bound = inspect.signature(function).bind_partial(instance, *args, **kwargs)
    except TypeError:
        return {"positional_count": len(args), "keyword_names": sorted(kwargs)}
    bound.arguments.pop("self", None)
    return dict(bound.arguments)


def logged_async(function):
    @functools.wraps(function)
    async def wrapper(self, *args, **kwargs):
        started = time.perf_counter()
        self._log(
            logging.INFO,
            "function_enter",
            function=function.__name__,
            arguments=_bound_arguments(function, self, args, kwargs),
        )
        try:
            result = await function(self, *args, **kwargs)
        except Exception as exc:
            sdk_state = getattr(self, "state", "UNKNOWN")
            self._log(
                logging.ERROR,
                "function_error",
                exc_info=True,
                function=function.__name__,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                error_type=type(exc).__name__,
                error=str(exc),
                error_code=getattr(getattr(exc, "code", None), "value", None),
                sdk_state=sdk_state,
                sdk_kept_running=sdk_state == "READY",
            )
            if sdk_state == "READY":
                self._log(
                    logging.WARNING,
                    "interface_failure_isolated",
                    function=function.__name__,
                    sdk_state=sdk_state,
                    sdk_kept_running=True,
                )
            raise
        self._log(
            logging.INFO,
            "function_exit",
            function=function.__name__,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            result=result,
        )
        return result

    return wrapper


def logged_sync(function):
    @functools.wraps(function)
    def wrapper(self, *args, **kwargs):
        started = time.perf_counter()
        self._log(
            logging.INFO,
            "function_enter",
            function=function.__name__,
            arguments=_bound_arguments(function, self, args, kwargs),
        )
        try:
            result = function(self, *args, **kwargs)
        except Exception as exc:
            sdk_state = getattr(self, "state", "UNKNOWN")
            self._log(
                logging.ERROR,
                "function_error",
                exc_info=True,
                function=function.__name__,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                error_type=type(exc).__name__,
                error=str(exc),
                error_code=getattr(getattr(exc, "code", None), "value", None),
                sdk_state=sdk_state,
                sdk_kept_running=sdk_state == "READY",
            )
            if sdk_state == "READY":
                self._log(
                    logging.WARNING,
                    "interface_failure_isolated",
                    function=function.__name__,
                    sdk_state=sdk_state,
                    sdk_kept_running=True,
                )
            raise
        self._log(
            logging.INFO,
            "function_exit",
            function=function.__name__,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            result=result,
        )
        return result

    return wrapper


class AgentSdk:
    def __init__(
        self,
        *,
        _proof_verifier: ProofVerifier | None = None,
        _control_request_authenticator: ControlRequestAuthenticator | None = None,
        _message_signer: MessageSigner | None = None,
        _message_signature_verifier: MessageSignatureVerifier | None = None,
        peer_messenger: PeerMessenger | None = None,
        tun_factory: TunFactory | None = None,
        masque_factory: MasqueFactory | None = None,
        runtime_factory: RuntimeFactory | None = None,
        server_factory: ServerFactory | None = None,
        route_backend_factory: RouteBackendFactory | None = None,
        _media_offload_adapter: MediaOffloadAdapter | None = None,
        _sandbox_transport: SandboxTransport | None = None,
        agent_state_directory: str | Path | None = None,
    ) -> None:
        self._logger = logging.getLogger(f"agent_sdk.client.{id(self)}")
        self._logger.propagate = False
        self._pending_logs: list[tuple[int, str, bool, dict[str, Any]]] = []
        self._device_identity_store = DeviceSigningIdentityStore()
        self._group_config_verification_enabled = _proof_verifier is not None
        self._a2a_verification_enabled = _message_signature_verifier is not None
        self._proof_verifier = _proof_verifier or DisabledProofVerifier()
        self._control_request_authenticator = (
            _control_request_authenticator
            or DeviceControlRequestAuthenticator(self._device_identity_store)
        )
        self._message_signer = _message_signer or DeviceMessageSigner(
            self._device_identity_store
        )
        self._message_signature_verifier = (
            _message_signature_verifier or DisabledMessageSignatureVerifier()
        )
        self._peer_messenger = peer_messenger or HttpPeerMessenger(logger=self._logger)
        self._tun_factory = tun_factory or LinuxTunDevice.create
        self._masque_factory = masque_factory or (
            lambda config: AioquicConnectIpTransport(
                server_url=config.masque_server_url,
                authorization=config.masque_authorization,
                logger=self._logger,
            )
        )
        self._runtime_factory = runtime_factory or (
            lambda host, port: HttpRuntimeTransport(
                host,
                port,
                logger=self._logger,
            )
        )
        self._server_factory = server_factory or (
            lambda: AiohttpLocalServer(logger=self._logger)
        )
        self._route_backend_factory = route_backend_factory or (
            lambda config, tun: Pyroute2RouteBackend(
                tun.name, config.agent_tun_ip
            )
        )
        self._media_offload_adapter = _media_offload_adapter
        self._sandbox_transport = _sandbox_transport or HttpSandboxTransport(
            logger=self._logger
        )
        self._agent_state_store = AgentStateStore(agent_state_directory)

        self._state = "NEW"
        self._config: SdkConfig | None = None
        self._runtime: RuntimeTransport | None = None
        self._server: LocalServer | None = None
        self._tun: TunDevice | None = None
        self._masque: ConnectIpTransport | None = None
        self._routes: GroupRouteManager | None = None
        self._groups: GroupMemberCache | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._network_listener: NetworkMessageListener | None = None
        self._group_listener: GroupMessageListener | None = None
        self._profile: AgentProfile | None = None
        self._agent_lifecycle_state = AgentLifecycleState.NO_IDENTITY
        self._identity_application_context: IdentityApplicationContext | None = None
        self._agent_card_context: AgentCardContext | None = None
        self._group_info: dict[str, GroupInfo] = {}
        self._ue_info: Mapping[str, Any] | None = None
        self._compute_requests: dict[str, Mapping[str, Any]] = {}
        self._compute_create_requests: dict[str, ComputeSessionRequest] = {}
        self._computing_statuses: dict[str, ComputeSessionStatus] = {}
        self._computing_statuses_by_request: dict[str, ComputeSessionStatus] = {}
        self._computing_sessions: dict[str, ComputingSession] = {}
        self._computing_media: dict[str, _MediaConnection] = {}
        self._computing_pending_media: dict[str, _PendingMediaConnection] = {}
        self._computing_media_locks: dict[str, asyncio.Lock] = {}
        self._computing_closing: set[str] = set()
        self._computing_close_results: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        self._computing_control_lock = asyncio.Lock()
        self._computing_session_changed = asyncio.Condition()
        self._identity_removal_lock = asyncio.Lock()
        self._identity_removal_in_progress = False
        self._computing_requests_in_flight: set[str] = set()
        self._computing_closed_session_ids: set[str] = set()
        self._received_a2a_message_ids: dict[str, None] = {}
        self._received_a2a_lock = asyncio.Lock()

    def _log(
        self,
        level: int,
        event: str,
        *,
        exc_info: bool = False,
        **fields: Any,
    ) -> None:
        if not self._logger.handlers:
            self._pending_logs.append((level, event, False, dict(fields)))
            return
        log_event(
            self._logger,
            level,
            event,
            exc_info=exc_info,
            **fields,
        )

    def _configure_logging(
        self,
        *,
        file_path: str,
        level: str,
        max_bytes: int,
        backup_count: int,
    ) -> None:
        self._logger = configure_local_logger(
            name=f"agent_sdk.client.{id(self)}",
            file_path=file_path,
            level=level,
            max_bytes=max_bytes,
            backup_count=backup_count,
        )
        pending, self._pending_logs = self._pending_logs, []
        for pending_level, pending_event, _, pending_fields in pending:
            log_event(
                self._logger,
                pending_level,
                pending_event,
                buffered_before_init=True,
                **pending_fields,
            )

    @property
    def state(self) -> str:
        return self._state

    @property
    def agent_lifecycle_state(self) -> AgentLifecycleState:
        return self._agent_lifecycle_state

    @property
    def local_profile(self) -> AgentProfile | None:
        return self._profile

    async def init(
        self,
        agent_runtime_ip: str,
        agent_runtime_port: int,
        local_tcp_port: int,
        local_udp_port: int,
        *,
        masque_server_url: str,
        masque_authorization: str | None = None,
        tun_name: str = "agent_tun0",
        tun_mtu: int = 1280,
        log_file_path: str = DEFAULT_LOG_FILE_PATH,
        log_level: str = DEFAULT_LOG_LEVEL,
        log_max_bytes: int = DEFAULT_LOG_MAX_BYTES,
        log_backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
    ) -> SdkInitResult:
        """Initialize the SDK using AgentRuntime for all control-plane requests."""
        self._configure_logging(
            file_path=log_file_path,
            level=log_level,
            max_bytes=log_max_bytes,
            backup_count=log_backup_count,
        )
        started = time.perf_counter()
        self._log(
            logging.INFO,
            "function_enter",
            function="init",
            arguments={
                "agent_runtime_ip": agent_runtime_ip,
                "agent_runtime_port": agent_runtime_port,
                "local_tcp_port": local_tcp_port,
                "local_udp_port": local_udp_port,
                "masque_server_url": masque_server_url,
                "masque_authorization": masque_authorization,
                "tun_name": tun_name,
                "tun_mtu": tun_mtu,
                "log_file_path": log_file_path,
                "log_level": log_level,
                "log_max_bytes": log_max_bytes,
                "log_backup_count": log_backup_count,
            },
        )
        if not self._group_config_verification_enabled:
            self._log(
                logging.WARNING,
                "inbound_signature_verification_disabled",
                security_profile="internal-test-only",
                group_config_proof_verification="disabled",
                a2a_message_proof="not_defined_by_contract",
                control_request_signing="enabled",
            )
        try:
            if self._state not in {"NEW", "CLOSED"}:
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"cannot init SDK in state {self._state}",
                )
            SdkConfig.validate_client_parameters(
                agent_runtime_ip=agent_runtime_ip,
                agent_runtime_port=agent_runtime_port,
                local_tcp_port=local_tcp_port,
                local_udp_port=local_udp_port,
                masque_server_url=masque_server_url,
                masque_authorization=masque_authorization,
                tun_name=tun_name,
                tun_mtu=tun_mtu,
                log_file_path=log_file_path,
                log_level=log_level,
                log_max_bytes=log_max_bytes,
                log_backup_count=log_backup_count,
            )
            # The persistent P-256 identity is SDK-owned. It is generated on the
            # first startup attempt and reused on subsequent startups.
            self._device_identity_store.ensure()
            self._state = "INITIALIZING"
            self._runtime = self._runtime_factory(
                agent_runtime_ip,
                agent_runtime_port,
            )
            self._ue_info = await self._runtime.get_ue_info()
            agent_tun_ip = HttpRuntimeTransport.select_ue_agent_ip(self._ue_info)
            config = SdkConfig.validate(
                agent_runtime_ip=agent_runtime_ip,
                agent_runtime_port=agent_runtime_port,
                local_tcp_port=local_tcp_port,
                local_udp_port=local_udp_port,
                agent_tun_ip=agent_tun_ip,
                masque_server_url=masque_server_url,
                masque_authorization=masque_authorization,
                tun_name=tun_name,
                tun_mtu=tun_mtu,
                log_file_path=log_file_path,
                log_level=log_level,
                log_max_bytes=log_max_bytes,
                log_backup_count=log_backup_count,
            )
            self._config = config
            restored = self._agent_state_store.load(
                config.agent_runtime_ip,
                config.agent_runtime_port,
                config.agent_tun_ip,
            )
            self._agent_lifecycle_state = restored.state
            self._profile = restored.profile
            self._identity_application_context = restored.identity_application
            self._agent_card_context = restored.agent_card
            self._log(
                logging.INFO,
                "agent_state_restored",
                agent_lifecycle_state=restored.state.value,
                agent_id=restored.profile.agent_id if restored.profile else None,
                state_file=str(
                    self._agent_state_store.state_file(
                        config.agent_runtime_ip,
                        config.agent_runtime_port,
                    )
                ),
            )
            self._tun = await self._tun_factory(
                config.tun_name, config.agent_tun_cidr, config.tun_mtu
            )
            backend = self._route_backend_factory(config, self._tun)
            self._routes = GroupRouteManager(backend)
            self._groups = GroupMemberCache(self._routes)

            self._server = self._server_factory()
            await self._server.start(
                agent_ip=config.agent_tun_ip,
                tcp_port=config.local_tcp_port,
                udp_port=config.local_udp_port,
                on_a2a_message=self._handle_a2a_message,
            )

            self._masque = self._masque_factory(config)
            await self._masque.start(self._write_downlink_packet)
            self._pump_task = asyncio.create_task(
                self._pump_uplink(), name="agent-tun-uplink"
            )
            self._state = "READY"
            await self._runtime.start_downlink(
                self._handle_runtime_downlink,
                self._recover_compute_statuses,
            )
            result = SdkInitResult(
                runtime_connected=True,
                masque_connected=self._masque.connected,
                local_tcp_endpoint=f"{config.agent_tun_ip}:{config.local_tcp_port}",
                local_udp_endpoint=f"{config.agent_tun_ip}:{config.local_udp_port}",
                agent_tcp_endpoint=f"{config.agent_tun_ip}:{config.local_tcp_port}",
                agent_udp_endpoint=f"{config.agent_tun_ip}:{config.local_udp_port}",
                agent_tun_cidr=config.agent_tun_cidr,
                masque_proxy_endpoint=config.masque_server_url,
            )
            self._log(
                logging.INFO,
                "function_exit",
                function="init",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                result=result,
            )
            return result
        except Exception as exc:
            self._log(
                logging.ERROR,
                "function_error",
                exc_info=True,
                function="init",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                error_type=type(exc).__name__,
                error=str(exc),
                error_code=getattr(getattr(exc, "code", None), "value", None),
            )
            await self.close()
            raise

    def _require_ready(self) -> None:
        if self._state != "READY":
            raise AgentSdkError(
                ErrorCode.SDK_NOT_INITIALIZED, "SDK is not initialized"
            )

    def _require_agent_state(
        self,
        expected: AgentLifecycleState | set[AgentLifecycleState],
        *,
        operation: str,
        agent_id: str | None = None,
    ) -> None:
        accepted = {expected} if isinstance(expected, AgentLifecycleState) else expected
        if self._agent_lifecycle_state not in accepted:
            expected_text = ", ".join(sorted(item.value for item in accepted))
            raise AgentSdkError(
                ErrorCode.AGENT_STATE_TRANSITION_INVALID,
                f"{operation} requires Agent state {expected_text}; current state is "
                f"{self._agent_lifecycle_state.value}",
                details={
                    "operation": operation,
                    "current_state": self._agent_lifecycle_state.value,
                    "expected_states": sorted(item.value for item in accepted),
                },
            )
        if agent_id is not None and (
            self._profile is None or self._profile.agent_id != agent_id
        ):
            raise AgentSdkError(
                ErrorCode.AGENT_STATE_TRANSITION_INVALID,
                f"{operation} agent_id does not match the persisted local identity",
                field="agent_id",
            )

    def _persist_agent_state(
        self,
        state: AgentLifecycleState,
        profile: AgentProfile,
        *,
        identity_application: IdentityApplicationContext | None = None,
        agent_card: AgentCardContext | None = None,
    ) -> None:
        if self._config is None:
            raise AgentSdkError(
                ErrorCode.SDK_NOT_INITIALIZED,
                "SDK configuration is unavailable for Agent state persistence",
            )
        resolved_identity_application = (
            identity_application or self._identity_application_context
        )
        if resolved_identity_application is None:
            raise AgentSdkError(
                ErrorCode.AGENT_STATE_INVALID,
                "identity application context is unavailable",
            )
        self._agent_state_store.save(
            self._config.agent_runtime_ip,
            self._config.agent_runtime_port,
            self._config.agent_tun_ip,
            state,
            profile,
            resolved_identity_application,
            agent_card,
        )
        self._profile = profile
        self._agent_lifecycle_state = state
        self._identity_application_context = resolved_identity_application
        self._agent_card_context = agent_card
        self._log(
            logging.INFO,
            "agent_state_transition",
            agent_lifecycle_state=state.value,
            agent_id=profile.agent_id,
        )

    def _clear_agent_state(self) -> None:
        if self._config is None:
            raise AgentSdkError(
                ErrorCode.SDK_NOT_INITIALIZED,
                "SDK configuration is unavailable for Agent state persistence",
            )
        self._agent_state_store.clear(
            self._config.agent_runtime_ip,
            self._config.agent_runtime_port,
        )
        previous_agent_id = self._profile.agent_id if self._profile else None
        self._profile = None
        self._agent_lifecycle_state = AgentLifecycleState.NO_IDENTITY
        self._identity_application_context = None
        self._agent_card_context = None
        self._log(
            logging.INFO,
            "agent_state_transition",
            agent_lifecycle_state=AgentLifecycleState.NO_IDENTITY.value,
            agent_id=previous_agent_id,
        )

    def _allowed(self, ip: str) -> bool:
        assert self._routes is not None
        address = ipaddress.ip_address(ip)
        return any(
            address in ipaddress.ip_network(route, strict=False)
            for route in self._routes.allowed_host_routes
        )

    async def _pump_uplink(self) -> None:
        assert self._tun is not None and self._masque is not None
        assert self._config is not None
        while self._state in {"INITIALIZING", "READY"}:
            packet = await self._tun.read()
            if not packet:
                return
            try:
                source, destination = validate_ip_packet(packet, self._config.tun_mtu)
            except ValueError:
                continue
            if source != self._config.agent_tun_ip or not self._allowed(destination):
                continue
            await self._masque.send_packet(packet)

    async def _write_downlink_packet(self, packet: bytes) -> None:
        assert self._tun is not None and self._config is not None
        try:
            source, destination = validate_ip_packet(packet, self._config.tun_mtu)
        except ValueError:
            return
        if destination != self._config.agent_tun_ip or not self._allowed(source):
            return
        await self._tun.write(packet)

    @logged_sync
    def register_network_message_listener(
        self, listener: NetworkMessageListener
    ) -> Callable[[], None]:
        if self._network_listener is not None:
            raise AgentSdkError(
                ErrorCode.LISTENER_ALREADY_REGISTERED,
                "network message listener is already registered",
            )
        self._network_listener = listener

        def unregister() -> None:
            if self._network_listener is listener:
                self._network_listener = None

        return unregister

    @logged_sync
    def register_group_message_listener(
        self, listener: GroupMessageListener
    ) -> Callable[[], None]:
        self._group_listener = listener

        def unregister() -> None:
            if self._group_listener is listener:
                self._group_listener = None

        return unregister

    async def _handle_group_invitation(
        self, payload: Mapping[str, Any]
    ) -> NetworkMessageAction:
        if self._network_listener is None:
            return NetworkMessageAction.REJECT
        return await self._network_listener.on_network_message(
            NetworkMessageType.GROUP_INVITATION, payload
        )

    async def _handle_runtime_downlink(
        self,
        message_type: str,
        transaction_id: int,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        self._log(
            logging.INFO,
            "runtime_downlink_dispatch",
            message_type=message_type,
            transaction_id=transaction_id,
        )
        if message_type == _COMPUTE_CONNECT_CONFIG:
            async with self._computing_control_lock:
                return await self._handle_compute_connect_config(payload)
        if message_type == _COMPUTE_SESSION_STATUS:
            async with self._computing_control_lock:
                await self._handle_compute_session_status(payload)
            return None
        if message_type == _COMPUTE_SESSION_CLOSE:
            async with self._computing_control_lock:
                return await self._handle_compute_session_close(payload)
        if message_type == "ACN_AGENT_GROUPING_INVITATION":
            action = await self._handle_group_invitation(payload)
            group_info = payload.get("group_info")
            group_id = (
                group_info.get("group_id")
                if isinstance(group_info, Mapping)
                else None
            )
            response: dict[str, Any] = {"result": action.value}
            if isinstance(group_id, str) and group_id:
                response["group_id"] = group_id
            return response
        if message_type == "ACN_AGENT_GROUPING_NOTIFICATION":
            action = await self._handle_group_config(payload)
            response = {"result": action.value}
            group_id = payload.get("group_id")
            if isinstance(group_id, str) and group_id:
                response["group_id"] = group_id
            return response
        if self._network_listener is None:
            return {"result": NetworkMessageAction.REJECT.value}
        action = await self._network_listener.on_network_message(
            NetworkMessageType.UNKNOWN, payload
        )
        return {"result": action.value}

    async def _handle_group_config(
        self, payload: Mapping[str, Any]
    ) -> NetworkMessageAction:
        self._require_ready()
        if self._profile is None:
            return NetworkMessageAction.REJECT
        assert self._groups is not None and self._config is not None
        await self._proof_verifier.verify_group_config(payload)
        candidate = self._groups.build_candidate(
            payload,
            local_agent_id=self._profile.agent_id,
            local_agent_ip=self._config.agent_tun_ip,
            local_tcp_port=self._config.local_tcp_port,
            local_udp_port=self._config.local_udp_port,
        )
        changed = await self._groups.commit(
            candidate, local_agent_id=self._profile.agent_id
        )
        if not changed:
            self._log(
                logging.INFO,
                "group_config_replay_acknowledged",
                group_id=candidate.group_id,
                timestamp=candidate.notification_timestamp.isoformat(),
            )
            return NetworkMessageAction.ACK
        info = self._group_info.get(candidate.group_id)
        if info is None:
            info = GroupInfo(candidate.group_id, candidate.group_id)
            self._group_info[candidate.group_id] = info
        info.status = "ACTIVE"
        if self._network_listener is not None:
            try:
                await self._network_listener.on_network_message(
                    NetworkMessageType.GROUP_CONFIG, payload
                )
            except Exception:
                self._log(
                    logging.ERROR,
                    "listener_error",
                    exc_info=True,
                    listener="network_message_listener",
                    message_type=NetworkMessageType.GROUP_CONFIG,
                    group_id=candidate.group_id,
                )
        return NetworkMessageAction.ACK

    async def _handle_a2a_message(self, payload: Mapping[str, Any]) -> None:
        self._require_ready()
        if self._profile is None or self._group_listener is None:
            raise AgentSdkError(
                ErrorCode.GROUP_NOT_ACTIVE, "A2A listener or local identity is missing"
            )
        allowed_fields = {
            "message_id",
            "group_id",
            "src_agent_id",
            "dst_agent_id",
            "type",
            "task_id",
            "timestamp",
            "payload",
        }
        unexpected_fields = sorted(
            str(field) for field in payload if field not in allowed_fields
        )
        if unexpected_fields:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                f"A2A contains unsupported field: {unexpected_fields[0]}",
                field=unexpected_fields[0],
            )
        group_id = str(payload.get("group_id", ""))
        sender_id = str(payload.get("src_agent_id", ""))
        target_id = str(payload.get("dst_agent_id", ""))
        for field in (
            "message_id",
            "group_id",
            "src_agent_id",
            "dst_agent_id",
            "type",
            "task_id",
            "timestamp",
        ):
            if not isinstance(payload.get(field), str) or not payload[field]:
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"A2A {field} must be a non-empty string",
                    field=field,
                )
        if target_id != self._profile.agent_id:
            raise AgentSdkError(
                ErrorCode.TARGET_NOT_IN_GROUP, "A2A message targets another agent"
            )
        assert self._groups is not None
        await self._groups.resolve(group_id, sender_id)
        user_payload = payload.get("payload")
        if not isinstance(user_payload, Mapping):
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT, "A2A payload must be a JSON object"
            )
        message_id = str(payload["message_id"])
        async with self._received_a2a_lock:
            if message_id in self._received_a2a_message_ids:
                self._log(
                    logging.INFO,
                    "a2a_duplicate_acknowledged",
                    message_id=message_id,
                )
                return
            self._received_a2a_message_ids[message_id] = None
            try:
                await self._group_listener.on_group_message(
                    group_id, sender_id, user_payload
                )
            except BaseException:
                self._received_a2a_message_ids.pop(message_id, None)
                raise
            while len(self._received_a2a_message_ids) > 1024:
                del self._received_a2a_message_ids[
                    next(iter(self._received_a2a_message_ids))
                ]

    @logged_async
    async def send_message(
        self,
        group_id: str,
        target_agent_id: str,
        json_message: Mapping[str, Any],
        timeout_seconds: float = 5.0,
        *,
        message_type: str,
        task_id: str,
    ) -> MessageReceipt:
        self._require_ready()
        if timeout_seconds <= 0:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "timeout_seconds must be greater than zero",
                field="timeout_seconds",
            )
        if self._profile is None:
            raise AgentSdkError(
                ErrorCode.GROUP_NOT_ACTIVE, "local identity has not been applied"
            )
        if not message_type:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "message_type must be a non-empty string",
                field="message_type",
            )
        if not task_id:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "task_id must be a non-empty string",
                field="task_id",
            )
        assert self._groups is not None
        target = await self._groups.resolve(group_id, target_agent_id)
        message_id = str(uuid.uuid4())
        body: dict[str, Any] = {
            "message_id": message_id,
            "group_id": group_id,
            "type": message_type,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "payload": dict(json_message),
            "src_agent_id": self._profile.agent_id,
            "dst_agent_id": target_agent_id,
            "task_id": task_id,
        }
        response = await self._peer_messenger.send(
            target.service_endpoint, body, timeout_seconds
        )
        delivered = response.get("status") == "OK"
        return MessageReceipt(
            message_id=message_id,
            delivered=delivered,
            delivered_at=datetime.now(timezone.utc) if delivered else None,
        )

    @logged_async
    async def apply_identity(
        self,
        owner: str,
        name: str,
        description: str,
        metadata: Mapping[str, Any],
    ) -> AgentProfile:
        self._require_ready()
        path = "/idm/v1/identity-applications"
        self._validate_identity_application(owner, name, description, metadata)
        normalized_metadata = self._normalize_identity_metadata(metadata)
        identity_application = IdentityApplicationContext(
            owner,
            name,
            description,
            dict(normalized_metadata),
        )
        if self._agent_lifecycle_state is AgentLifecycleState.IDENTITY_READY:
            assert self._profile is not None
            previous_agent_id = self._profile.agent_id
            replacement = await self.deregister_identity(
                previous_agent_id,
                reason="replaced",
            )
            if not replacement.success:
                raise AgentSdkError(
                    ErrorCode.RUNTIME_REJECTED,
                    "cannot replace local identity because deregistration failed",
                    details={"agent_id": previous_agent_id},
                )
        self._require_agent_state(
            AgentLifecycleState.NO_IDENTITY,
            operation="apply_identity",
        )
        assert self._runtime is not None
        body = await self._authenticate_control_request(
            path,
            {
                "request_id": str(uuid.uuid4()),
                "owner": owner,
                "name": name,
                "public_key": self._device_identity_store.ensure().public_key_base64,
                "description": description,
                "metadata": normalized_metadata,
            },
        )
        response = await self._runtime.request("POST", path, body)
        if response.get("result") != "success":
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "Runtime identity response result must be success",
                field="result",
            )
        vc0 = self._require_response_object(response, "vc0")
        claims = vc0.get("claims")
        response_name = name
        if isinstance(claims, Mapping) and isinstance(
            claims.get("agent_name"), str
        ):
            response_name = str(claims["agent_name"])
        profile = AgentProfile(
            agent_id=str(response["agent_id"]),
            agent_name=response_name,
            identity_vc=dict(vc0),
        )
        self._persist_agent_state(
            AgentLifecycleState.IDENTITY_READY,
            profile,
            identity_application=identity_application,
        )
        return profile

    @logged_sync
    def set_local_profile_for_restore(
        self,
        profile: AgentProfile,
    ) -> None:
        """Restore a previously verified profile from secure local storage."""
        identity_application = self._identity_application_context or (
            IdentityApplicationContext(
                owner="restored-local-profile",
                name=profile.agent_name,
                description="Profile restored by the host application",
                metadata={"region": "unknown", "os": "unknown", "version": "unknown"},
            )
        )
        self._persist_agent_state(
            AgentLifecycleState.IDENTITY_READY,
            profile,
            identity_application=identity_application,
        )

    @logged_async
    async def deregister_identity(
        self, agent_id: str, reason: str = "retired"
    ) -> OperationResult:
        self._require_ready()
        self._require_agent_state(
            {AgentLifecycleState.IDENTITY_READY, AgentLifecycleState.CARD_PUBLISHED},
            operation="deregister_identity",
            agent_id=agent_id,
        )
        assert self._runtime is not None
        path = "/acn-agent/v1/agent-deletions"
        allowed_reasons = {
            "normal",
            "uninstalled",
            "replaced",
            "user_request",
            "security_event",
            "retired",
            "other",
        }
        if reason not in allowed_reasons:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "reason is not a supported deregistration reason",
                field="reason",
            )
        async with self._identity_removal_lock:
            await self._begin_identity_removal("deregister_identity")
            try:
                body = await self._authenticate_control_request(
                    path,
                    {
                        "request_id": str(uuid.uuid4()),
                        "agent_id": agent_id,
                        "reason": reason,
                    },
                )
                response = await self._runtime.request("POST", path, body)
                result = OperationResult(
                    bool(response.get("success", True)),
                    str(response.get("operation_id", "")),
                    str(response.get("message", "")),
                )
                if result.success:
                    self._clear_agent_state()
                return result
            finally:
                self._identity_removal_in_progress = False

    @logged_async
    async def reset_agent(self) -> OperationResult:
        """Return the local Agent lifecycle to state 1 (``NO_IDENTITY``).

        Reset is a local-only operation: it clears the persisted Profile/Card
        state without deregistering the identity from the network. It is
        idempotent in state 1.
        """
        self._require_ready()
        if self._agent_lifecycle_state is AgentLifecycleState.NO_IDENTITY:
            return OperationResult(
                True,
                "",
                "Agent is already in NO_IDENTITY state",
            )
        async with self._identity_removal_lock:
            await self._begin_identity_removal("reset_agent")
            try:
                self._clear_agent_state()
                return OperationResult(
                    True,
                    "",
                    "Local Agent state reset to NO_IDENTITY; network identity was not changed",
                )
            finally:
                self._identity_removal_in_progress = False

    async def _begin_identity_removal(self, operation: str) -> None:
        async with self._computing_control_lock:
            active_session_ids = self._active_computing_session_ids()
            active_request_ids = sorted(
                self._computing_requests_in_flight
                | set(self._unresolved_compute_create_request_ids())
            )
            if active_session_ids or active_request_ids:
                details = active_session_ids + [
                    f"request:{request_id}"
                    for request_id in active_request_ids
                ]
                raise AgentSdkError(
                    ErrorCode.AGENT_STATE_INVALID,
                    f"{operation} requires all computing sessions to be released or "
                    "cancelled before clearing the Agent Profile: "
                    + ", ".join(details),
                )
            self._identity_removal_in_progress = True

    def _active_computing_session_ids(self) -> list[str]:
        return sorted(
            set(self._computing_sessions)
            | set(self._computing_pending_media)
            | set(self._computing_media)
            | {
                session_id
                for session_id, status in self._computing_statuses.items()
                if session_id not in self._computing_closed_session_ids
                and status.status not in _COMPUTE_TERMINAL_STATUSES
            }
        )

    def _unresolved_compute_create_request_ids(self) -> list[str]:
        unresolved: list[str] = []
        for request_id in self._compute_create_requests:
            status = self._computing_statuses_by_request.get(request_id)
            session_id = status.compute_service_session_id if status else None
            session_status = self._computing_statuses.get(session_id or "")
            if status is None or not (
                status.status in _COMPUTE_TERMINAL_STATUSES
                or (
                    session_status is not None
                    and session_status.status in _COMPUTE_TERMINAL_STATUSES
                )
                or session_id in self._computing_closed_session_ids
            ):
                unresolved.append(request_id)
        return unresolved

    @logged_async
    async def get_network_ability(
        self,
        agent_id: str,
        intent: str = "Issue Network Ability Credential",
    ) -> NetworkAbility:
        self._require_ready()
        assert self._runtime is not None
        if not isinstance(intent, str) or not (1 <= len(intent) <= 256):
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "intent length must be in 1..256",
                field="intent",
            )
        path = "/idm/v1/network-ability"
        body = await self._authenticate_control_request(
            path,
            {
                "request_id": str(uuid.uuid4()),
                "agent_id": agent_id,
                "intent": intent,
            },
        )
        response = await self._runtime.request("POST", path, body)
        vc1 = self._require_response_object(response, "vc1")
        claims = vc1.get("claims")
        abilities: tuple[str, ...] = ()
        if isinstance(claims, Mapping):
            raw_abilities = claims.get("network_abilities")
            if raw_abilities is None:
                raw_abilities = claims.get("abilities")
            if isinstance(raw_abilities, list) and all(
                isinstance(item, str) and item for item in raw_abilities
            ):
                abilities = tuple(raw_abilities)
            elif isinstance(claims.get("agent_attribute"), str):
                abilities = (str(claims["agent_attribute"]),)
        valid_until = self._parse_optional_datetime(vc1.get("valid_until"))
        return NetworkAbility(
            ability_vc=dict(vc1),
            abilities=abilities,
            valid_until=valid_until,
        )

    @logged_async
    async def register_capabilities(
        self,
        agent_id: str,
        priority: int,
        credentials: Sequence[Mapping[str, Any]] | None = None,
        *,
        capabilities: Sequence[str] | None = None,
        agent_name: str | None = None,
        test_vc_private_key_path: str | Path | None = None,
    ) -> OperationResult:
        self._require_ready()
        self._require_agent_state(
            {
                AgentLifecycleState.IDENTITY_READY,
                AgentLifecycleState.CARD_PUBLISHED,
            },
            operation="register_capabilities",
            agent_id=agent_id,
        )
        vc_list = list(credentials or ())
        if capabilities is not None:
            resolved_agent_name = agent_name
            if (
                resolved_agent_name is None
                and self._profile is not None
                and self._profile.agent_id == agent_id
            ):
                resolved_agent_name = self._profile.agent_name
            if resolved_agent_name is None:
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    "agent_name is required when raw capabilities are published "
                    "without a matching local profile",
                    field="agent_name",
                )
            vc_list.extend(
                issue_test_capability_vcs(
                    agent_id=agent_id,
                    agent_name=resolved_agent_name,
                    capabilities=capabilities,
                    private_key_path=test_vc_private_key_path,
                )
            )
        if not vc_list:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "credentials or capabilities must contain at least one item",
                field="credentials",
            )
        if self._agent_lifecycle_state is AgentLifecycleState.CARD_PUBLISHED:
            identity_application = self._identity_application_context
            if identity_application is None:
                raise AgentSdkError(
                    ErrorCode.AGENT_STATE_INVALID,
                    "cannot replace Agent Card because identity application context is missing",
                )
            self._ensure_credentials_rebindable(vc_list, old_agent_id=agent_id)
            deregistered = await self.deregister_identity(
                agent_id,
                reason="replaced",
            )
            if not deregistered.success:
                raise AgentSdkError(
                    ErrorCode.RUNTIME_REJECTED,
                    "cannot replace Agent Card because identity deregistration failed",
                    details={"agent_id": agent_id},
                )
            new_profile = await self.apply_identity(
                identity_application.owner,
                identity_application.name,
                identity_application.description,
                identity_application.metadata,
            )
            rebound_credentials = await self._refresh_rebound_credentials(
                list(credentials or ()),
                old_agent_id=agent_id,
                new_profile=new_profile,
            )
            return await self.register_capabilities(
                new_profile.agent_id,
                priority,
                credentials=rebound_credentials,
                capabilities=capabilities,
                agent_name=agent_name or new_profile.agent_name,
                test_vc_private_key_path=test_vc_private_key_path,
            )
        assert self._config is not None
        service_endpoints = (
            f"http://{self._config.agent_tun_ip}:"
            f"{self._config.local_tcp_port}/A2A/message"
        )
        path = "/arf/v1/agent-cards"
        body = await self._authenticate_control_request(
            path,
            {
                "request_id": str(uuid.uuid4()),
                "agent_id": agent_id,
                "priority": priority,
                "service_endpoints": service_endpoints,
                "vc_list": vc_list,
            },
        )
        result = await self._operation(
            "POST",
            path,
            body,
        )
        if result.success:
            assert self._profile is not None
            self._persist_agent_state(
                AgentLifecycleState.CARD_PUBLISHED,
                self._profile,
                agent_card=AgentCardContext(
                    priority,
                    tuple(dict(item) for item in vc_list),
                ),
            )
        return result

    @logged_async
    async def update_capabilities(
        self,
        agent_id: str,
        update_items: Sequence[Mapping[str, Any]],
        credentials: Sequence[Mapping[str, Any]],
    ) -> OperationResult:
        self._require_ready()
        self._require_agent_state(
            AgentLifecycleState.CARD_PUBLISHED,
            operation="update_capabilities",
            agent_id=agent_id,
        )
        card_context = self._agent_card_context
        if card_context is None:
            raise AgentSdkError(
                ErrorCode.AGENT_STATE_INVALID,
                "cannot update Agent Card because persisted registration context is missing",
            )
        replacement_vcs = self._apply_capability_updates(
            card_context.vc_list,
            update_items,
            credentials,
        )
        path = "/arf/v1/agent-cards-update"
        body = await self._authenticate_control_request(
            path,
            {
                "request_id": str(uuid.uuid4()),
                "agent_id": agent_id,
                "update_items": list(update_items),
                "credentials": list(credentials),
            },
        )
        result = await self._operation(
            "POST",
            path,
            body,
        )
        if result.success:
            assert self._profile is not None
            self._persist_agent_state(
                AgentLifecycleState.CARD_PUBLISHED,
                self._profile,
                agent_card=AgentCardContext(
                    card_context.priority,
                    tuple(dict(item) for item in replacement_vcs),
                ),
            )
        return result

    @logged_async
    async def discover_agents(
        self,
        agent_id: str,
        task_description: str,
        required_skills: Sequence[str],
        discovery_scope: str = "intra_plmn",
        max_results: int = 10,
    ) -> list[DiscoveredAgent]:
        self._require_ready()
        assert self._runtime is not None
        path = "/arf/v1/agent-discoveries"
        body = await self._authenticate_control_request(
            path,
            {
                "request_id": str(uuid.uuid4()),
                "agent_id": agent_id,
                "task_description": task_description,
                "required_skills": list(required_skills),
                "discovery_scope": discovery_scope,
                "max_results": max_results,
            },
        )
        response = await self._runtime.request("POST", path, body)
        raw_results = response.get("result")
        if not isinstance(raw_results, list):
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "Runtime response field result must be an array",
                field="result",
            )
        agents: list[DiscoveredAgent] = []
        for index, item in enumerate(raw_results):
            if not isinstance(item, Mapping):
                raise AgentSdkError(
                    ErrorCode.RUNTIME_REJECTED,
                    f"Runtime response result[{index}] must be an object",
                )
            card = self._require_response_object(item, "agent_card")
            agents.append(
                DiscoveredAgent(
                    agent_id=str(card["agent_id"]),
                    service_endpoints=str(card["service_endpoints"]),
                    skills=tuple(card.get("skills", ())),
                    priority=int(item.get("priority", 0)),
                )
            )
        return sorted(agents, key=lambda item: item.priority)

    @logged_async
    async def create_group(
        self,
        agent_id: str,
        target_agent_ids: Sequence[str],
        group_name: str,
        dnn: str,
        scope: str = "private",
        max_members: int = 10,
    ) -> GroupInfo:
        self._require_ready()
        if not isinstance(dnn, str) or not dnn.strip():
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "dnn must be a non-empty string",
                field="dnn",
            )
        assert self._runtime is not None
        path = "/acf/v1/agents-grouping"
        body = await self._authenticate_control_request(
            path,
            {
                "request_id": str(uuid.uuid4()),
                "agent_id": agent_id,
                "target_agents": list(target_agent_ids),
                "group_config": {
                    "group_name": group_name,
                    "scope": scope,
                    "max_members": max_members,
                    "dnn": dnn,
                },
            },
        )
        response = await self._runtime.request("POST", path, body)
        if response.get("status") != "grouped":
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "Runtime group response status must be grouped",
                field="status",
            )
        group_id = str(response["group_id"])
        assert self._groups is not None
        snapshot = await self._groups.snapshot(group_id)
        current = self._group_info.get(group_id)
        status = (
            "ACTIVE"
            if snapshot is not None
            or (current is not None and current.status == "ACTIVE")
            else "PENDING"
        )
        info = GroupInfo(group_id, group_name, status)
        self._group_info[info.group_id] = info
        return info

    @logged_async
    async def create_computing_session(
        self,
        request: ComputeSessionRequest,
        timeout_seconds: float = 30.0,
    ) -> ComputeSessionStatus:
        await self._validate_compute_request(request, ComputeRequestType.CREATE)
        assert request.acn_context is not None
        if self._profile is None or (
            request.acn_context.requester_agent_id != self._profile.agent_id
        ):
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "acn_context.requester_agent_id must match the local Agent",
                field="acn_context.requester_agent_id",
            )
        assert self._groups is not None
        snapshot = await self._groups.snapshot(request.acn_context.group_id)
        group_info = self._group_info.get(request.acn_context.group_id)
        if snapshot is None or group_info is None or group_info.status != "ACTIVE":
            raise AgentSdkError(
                ErrorCode.GROUP_NOT_ACTIVE,
                f"group {request.acn_context.group_id} is not ACTIVE",
                field="acn_context.group_id",
            )
        if request.acn_context.target_agent_id not in snapshot.members_by_agent_id:
            raise AgentSdkError(
                ErrorCode.TARGET_NOT_IN_GROUP,
                "acn_context.target_agent_id is not in the configured group",
                field="acn_context.target_agent_id",
            )
        return await self._send_compute_request(request, timeout_seconds)

    @logged_async
    async def query_computing_session(
        self,
        request: ComputeSessionRequest,
        timeout_seconds: float = 30.0,
    ) -> ComputeSessionStatus:
        await self._validate_compute_request(request, ComputeRequestType.QUERY)
        return await self._send_compute_request(request, timeout_seconds)

    @logged_async
    async def cancel_computing_session(
        self,
        request: ComputeSessionRequest,
        timeout_seconds: float = 30.0,
    ) -> ComputeSessionStatus:
        await self._validate_compute_request(request, ComputeRequestType.CANCEL)
        return await self._send_compute_request(request, timeout_seconds)

    @logged_async
    async def release_computing_session(
        self,
        request: ComputeSessionRequest,
        timeout_seconds: float = 30.0,
    ) -> ComputeSessionStatus:
        await self._validate_compute_request(request, ComputeRequestType.RELEASE)
        return await self._send_compute_request(request, timeout_seconds)

    @logged_async
    async def await_computing_session_closed(
        self,
        compute_service_session_id: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._require_ready()
        session_id = self._require_nonempty_string(
            compute_service_session_id,
            "compute_service_session_id",
            ErrorCode.INVALID_ARGUMENT,
        )
        if timeout_seconds <= 0:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "timeout_seconds must be greater than zero",
                field="timeout_seconds",
            )

        async def wait() -> None:
            async with self._computing_session_changed:
                while session_id in self._active_computing_session_ids():
                    await self._computing_session_changed.wait()

        try:
            await asyncio.wait_for(wait(), timeout_seconds)
        except TimeoutError as exc:
            raise AgentSdkError(
                ErrorCode.TIMEOUT,
                f"timed out waiting for C-05 to close computing session {session_id}",
                retryable=True,
            ) from exc

    @logged_async
    async def start_video_upload(
        self,
        compute_service_session_id: str,
        camera_id: int = 0,
        width: int = 1920,
        height: int = 1080,
        fps: int = 30,
        bitrate_kbps: int = 4000,
        timeout_seconds: float = 120.0,
    ) -> VideoUploadHandle:
        if timeout_seconds <= 0:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "timeout_seconds must be greater than zero",
                field="timeout_seconds",
            )
        self._require_ready()
        session = await self._wait_for_computing_session(
            compute_service_session_id, timeout_seconds
        )
        if session.role is not ComputeRole.PRODUCER:
            raise AgentSdkError(
                ErrorCode.COMPUTING_SESSION_INVALID,
                "start_video_upload requires the producer configuration",
                field="role",
            )
        adapter = self._require_media_adapter()
        self._validate_media_codec(adapter, session)
        for field, value in (
            ("width", width),
            ("height", height),
            ("fps", fps),
            ("bitrate_kbps", bitrate_kbps),
        ):
            if value <= 0:
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"{field} must be greater than zero",
                    field=field,
                )
        lock = self._computing_media_locks.setdefault(
            compute_service_session_id, asyncio.Lock()
        )
        async with lock:
            existing = self._computing_media.get(compute_service_session_id)
            if existing is not None:
                return existing.managed  # type: ignore[return-value]
            pending = self._computing_pending_media.get(compute_service_session_id)
            if pending is None:
                prepared = await self._await_media_step(
                    adapter.prepare_video_upload(
                        session,
                        camera_id=camera_id,
                        width=width,
                        height=height,
                        fps=fps,
                        bitrate_kbps=bitrate_kbps,
                    ),
                    timeout_seconds,
                    "preparing the local WebRTC Offer",
                )
                try:
                    self._validate_local_media_offer(session, prepared.offer_sdp)
                except BaseException:
                    await prepared.abort()
                    raise
                pending = _PendingMediaConnection(
                    session,
                    f"media-{session.role.value}-{uuid.uuid4()}",
                    prepared.offer_sdp,
                    prepared,
                )
                self._computing_pending_media[compute_service_session_id] = pending
            prepared = pending.prepared
            connection_id: str | None = None
            try:
                request_id, connection_id, answer_sdp = await self._create_media_connection(
                    session, pending.request_id, pending.offer_sdp, timeout_seconds
                )
                if compute_service_session_id in self._computing_closing:
                    raise AgentSdkError(
                        ErrorCode.COMPUTING_SESSION_INVALID,
                        "computing session closed during media negotiation",
                    )
                await self._install_media_candidate_routes(session, answer_sdp)
                upload = await self._await_media_step(
                    prepared.apply_answer(answer_sdp, timeout_seconds),
                    timeout_seconds,
                    "applying the Sandbox WebRTC Answer",
                )
                if compute_service_session_id in self._computing_closing:
                    await upload.stop()
                    raise AgentSdkError(
                        ErrorCode.COMPUTING_SESSION_INVALID,
                        "computing session closed during media negotiation",
                    )
            except BaseException as exc:
                preserve_pending = (
                    connection_id is None
                    and isinstance(exc, AgentSdkError)
                    and exc.retryable
                    and compute_service_session_id not in self._computing_closing
                )
                if preserve_pending:
                    raise
                self._computing_pending_media.pop(compute_service_session_id, None)
                try:
                    await prepared.abort()
                finally:
                    if connection_id is not None:
                        try:
                            await self._delete_media_connection(
                                session, connection_id, timeout_seconds
                            )
                        except Exception:
                            pass
                        if compute_service_session_id not in self._computing_closing:
                            try:
                                await self._replace_compute_routes(session)
                            except Exception:
                                pass
                raise
            managed = _ManagedVideoUpload(
                upload,
                lambda: self._close_media_record(
                    compute_service_session_id, connection_id, timeout_seconds
                ),
            )
            self._computing_media[compute_service_session_id] = _MediaConnection(
                session, request_id, connection_id, upload, managed
            )
            self._computing_pending_media.pop(compute_service_session_id, None)
            return managed

    @logged_async
    async def get_processed_video_stream(
        self,
        compute_service_session_id: str,
        timeout_seconds: float = 120.0,
    ) -> RemoteVideoStream:
        if timeout_seconds <= 0:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "timeout_seconds must be greater than zero",
                field="timeout_seconds",
            )
        self._require_ready()
        session = await self._wait_for_computing_session(
            compute_service_session_id, timeout_seconds
        )
        if session.role is not ComputeRole.CONSUMER:
            raise AgentSdkError(
                ErrorCode.COMPUTING_SESSION_INVALID,
                "get_processed_video_stream requires the consumer configuration",
                field="role",
            )
        adapter = self._require_media_adapter()
        self._validate_media_codec(adapter, session)
        lock = self._computing_media_locks.setdefault(
            compute_service_session_id, asyncio.Lock()
        )
        async with lock:
            existing = self._computing_media.get(compute_service_session_id)
            if existing is not None:
                return existing.managed  # type: ignore[return-value]
            pending = self._computing_pending_media.get(compute_service_session_id)
            if pending is None:
                prepared = await self._await_media_step(
                    adapter.prepare_processed_video(session),
                    timeout_seconds,
                    "preparing the local WebRTC Offer",
                )
                try:
                    self._validate_local_media_offer(session, prepared.offer_sdp)
                except BaseException:
                    await prepared.abort()
                    raise
                pending = _PendingMediaConnection(
                    session,
                    f"media-{session.role.value}-{uuid.uuid4()}",
                    prepared.offer_sdp,
                    prepared,
                )
                self._computing_pending_media[compute_service_session_id] = pending
            prepared = pending.prepared
            connection_id: str | None = None
            try:
                request_id, connection_id, answer_sdp = await self._create_media_connection(
                    session, pending.request_id, pending.offer_sdp, timeout_seconds
                )
                if compute_service_session_id in self._computing_closing:
                    raise AgentSdkError(
                        ErrorCode.COMPUTING_SESSION_INVALID,
                        "computing session closed during media negotiation",
                    )
                await self._install_media_candidate_routes(session, answer_sdp)
                stream = await self._await_media_step(
                    prepared.apply_answer(answer_sdp, timeout_seconds),
                    timeout_seconds,
                    "applying the Sandbox WebRTC Answer",
                )
                if compute_service_session_id in self._computing_closing:
                    await stream.close()
                    raise AgentSdkError(
                        ErrorCode.COMPUTING_SESSION_INVALID,
                        "computing session closed during media negotiation",
                    )
            except BaseException as exc:
                preserve_pending = (
                    connection_id is None
                    and isinstance(exc, AgentSdkError)
                    and exc.retryable
                    and compute_service_session_id not in self._computing_closing
                )
                if preserve_pending:
                    raise
                self._computing_pending_media.pop(compute_service_session_id, None)
                try:
                    await prepared.abort()
                finally:
                    if connection_id is not None:
                        try:
                            await self._delete_media_connection(
                                session, connection_id, timeout_seconds
                            )
                        except Exception:
                            pass
                        if compute_service_session_id not in self._computing_closing:
                            try:
                                await self._replace_compute_routes(session)
                            except Exception:
                                pass
                raise
            managed = _ManagedRemoteVideoStream(
                stream,
                lambda: self._close_media_record(
                    compute_service_session_id, connection_id, timeout_seconds
                ),
            )
            self._computing_media[compute_service_session_id] = _MediaConnection(
                session, request_id, connection_id, stream, managed
            )
            self._computing_pending_media.pop(compute_service_session_id, None)
            return managed

    @logged_async
    async def update_recognition_target(
        self,
        compute_service_session_id: str,
        request_id: str,
        text: str,
        language: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> RecognitionTargetStatus:
        """Replace the consumer session's current visual recognition target."""
        self._validate_sandbox_timeout(timeout_seconds)
        self._validate_compute_request_id(request_id, "request_id")
        self._require_nonempty_string(text, "text", ErrorCode.INVALID_ARGUMENT)
        if language is not None:
            self._require_nonempty_string(
                language, "language", ErrorCode.INVALID_ARGUMENT
            )
        session = await self._wait_for_computing_session(
            compute_service_session_id, timeout_seconds
        )
        self._require_consumer_session(session, "update_recognition_target")
        context = self._media_context(session)
        input_body: dict[str, Any] = {"type": "TEXT", "text": text}
        if language is not None:
            input_body["language"] = language
        body = {
            "request_id": request_id,
            "computing_context": context,
            "input": input_body,
        }
        response = await self._sandbox_transport.request_with_status(
            "PUT",
            self._recognition_target_url(session),
            body,
            timeout_seconds,
            session.network_binding.ue_ipv4,
        )
        return self._parse_recognition_target_response(
            response, session, expected_request_id=request_id
        )

    @logged_async
    async def get_recognition_target(
        self,
        compute_service_session_id: str,
        timeout_seconds: float = 15.0,
    ) -> RecognitionTargetStatus:
        """Read the latest recognition target applied to a consumer session."""
        self._validate_sandbox_timeout(timeout_seconds)
        session = await self._wait_for_computing_session(
            compute_service_session_id, timeout_seconds
        )
        self._require_consumer_session(session, "get_recognition_target")
        response = await self._sandbox_transport.request_with_status(
            "GET",
            self._recognition_target_url(session),
            None,
            timeout_seconds,
            session.network_binding.ue_ipv4,
        )
        return self._parse_recognition_target_response(response, session)

    @logged_async
    async def create_control_action(
        self,
        compute_service_session_id: str,
        request: ControlActionRequest,
        timeout_seconds: float = 15.0,
    ) -> ControlActionStatus:
        """Submit one runtime action to the Sandbox for a consumer session."""
        self._validate_sandbox_timeout(timeout_seconds)
        self._validate_control_action_request(request)
        session = await self._wait_for_computing_session(
            compute_service_session_id, timeout_seconds
        )
        self._require_consumer_session(session, "create_control_action")
        body: dict[str, Any] = {
            "request_id": request.request_id,
            "computing_context": self._media_context(session),
            "input": {"type": request.input_type.value},
        }
        if request.action is not None:
            body["action"] = request.action.value
        if request.input_type is ControlInputType.TEXT:
            body["input"]["text"] = request.text
            if request.language is not None:
                body["input"]["language"] = request.language
        if request.parameters is not None:
            body["parameters"] = dict(request.parameters)
        if request.target is not None:
            target: dict[str, Any] = {"role": request.target.role.value}
            if request.target.agent_id is not None:
                target["agent_id"] = request.target.agent_id
            body["target"] = target
        response = await self._sandbox_transport.request_with_status(
            "POST",
            self._control_actions_url(session),
            body,
            timeout_seconds,
            session.network_binding.ue_ipv4,
        )
        return self._parse_control_action_response(
            response,
            session,
            expected_http_status=202,
            expected_request_id=request.request_id,
            require_context=True,
            require_normalized=request.input_type is ControlInputType.TEXT,
        )

    @logged_async
    async def get_control_action(
        self,
        compute_service_session_id: str,
        action_id: str,
        timeout_seconds: float = 15.0,
    ) -> ControlActionStatus:
        """Read an asynchronous Sandbox action without executing it again."""
        self._validate_sandbox_timeout(timeout_seconds)
        action_id = self._require_nonempty_string(
            action_id, "action_id", ErrorCode.INVALID_ARGUMENT
        )
        session = await self._wait_for_computing_session(
            compute_service_session_id, timeout_seconds
        )
        self._require_consumer_session(session, "get_control_action")
        url = (
            f"{self._control_actions_url(session).rstrip('/')}"
            f"/{quote(action_id, safe='')}"
        )
        response = await self._sandbox_transport.request_with_status(
            "GET", url, None, timeout_seconds, session.network_binding.ue_ipv4
        )
        return self._parse_control_action_response(
            response,
            session,
            expected_http_status=200,
            expected_action_id=action_id,
        )

    def _validate_control_action_request(self, request: ControlActionRequest) -> None:
        if not isinstance(request, ControlActionRequest):
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "request must be a ControlActionRequest",
                field="request",
            )
        self._validate_compute_request_id(request.request_id, "request_id")
        if not isinstance(request.input_type, ControlInputType):
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "input_type must be a ControlInputType",
                field="input_type",
            )
        if request.action is not None and not isinstance(request.action, ControlAction):
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "action must be a ControlAction",
                field="action",
            )
        if request.input_type is ControlInputType.TEXT:
            self._require_nonempty_string(
                request.text, "text", ErrorCode.INVALID_ARGUMENT
            )
            if request.language is not None:
                self._require_nonempty_string(
                    request.language, "language", ErrorCode.INVALID_ARGUMENT
                )
            if request.parameters is not None:
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    "TEXT control input must not contain parameters",
                    field="parameters",
                )
        else:
            if request.text is not None or request.language is not None:
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    "STRUCTURED control input must not contain text or language",
                    field="text",
                )
            if request.action is None:
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    "STRUCTURED control input requires action",
                    field="action",
                )
            if not isinstance(request.parameters, Mapping):
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    "STRUCTURED control input requires a parameters object",
                    field="parameters",
                )
        if request.target is not None:
            if not isinstance(request.target.role, ControlTargetRole):
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    "target.role must be a ControlTargetRole",
                    field="target.role",
                )
            if request.target.role is ControlTargetRole.PRODUCER:
                self._require_nonempty_string(
                    request.target.agent_id,
                    "target.agent_id",
                    ErrorCode.INVALID_ARGUMENT,
                )
            elif request.target.agent_id is not None:
                self._require_nonempty_string(
                    request.target.agent_id,
                    "target.agent_id",
                    ErrorCode.INVALID_ARGUMENT,
                )

    def _parse_control_action_response(
        self,
        response: RuntimeHttpResponse,
        session: ComputingSession,
        *,
        expected_http_status: int,
        expected_request_id: str | None = None,
        expected_action_id: str | None = None,
        require_context: bool = False,
        require_normalized: bool = False,
    ) -> ControlActionStatus:
        if response.status_code != expected_http_status:
            error = response.body.get("error")
            code = error.get("code") if isinstance(error, Mapping) else None
            message = error.get("message") if isinstance(error, Mapping) else None
            raise AgentSdkError(
                ErrorCode.SANDBOX_REJECTED,
                str(message or f"Sandbox returned HTTP {response.status_code}"),
                retryable=response.status_code >= 500,
                details={"sandbox_code": code, "status_code": response.status_code},
            )
        request_id = self._require_nonempty_string(
            response.body.get("request_id"),
            "request_id",
            ErrorCode.SANDBOX_REJECTED,
        )
        if expected_request_id is not None and request_id != expected_request_id:
            raise AgentSdkError(
                ErrorCode.SANDBOX_REJECTED,
                "response request_id does not match the control request",
                field="request_id",
            )
        action_id = self._require_nonempty_string(
            response.body.get("action_id"),
            "action_id",
            ErrorCode.SANDBOX_REJECTED,
        )
        if expected_action_id is not None and action_id != expected_action_id:
            raise AgentSdkError(
                ErrorCode.SANDBOX_REJECTED,
                "response action_id does not match the query",
                field="action_id",
            )
        raw_context = response.body.get("computing_context")
        expected_context = self._media_context(session)
        if require_context and raw_context is None:
            raise AgentSdkError(
                ErrorCode.SANDBOX_REJECTED,
                "response computing_context is required",
                field="computing_context",
            )
        if raw_context is not None and raw_context != expected_context:
            raise AgentSdkError(
                ErrorCode.SANDBOX_REJECTED,
                "response computing_context does not match C-02",
                field="computing_context",
            )
        status = self._require_nonempty_string(
            response.body.get("status"), "status", ErrorCode.SANDBOX_REJECTED
        )
        if status not in {
            "ACCEPTED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "UNKNOWN"
        }:
            raise AgentSdkError(
                ErrorCode.SANDBOX_REJECTED,
                "status is not a defined control action status",
                field="status",
            )
        cause = response.body.get("cause")
        if not isinstance(cause, str):
            raise AgentSdkError(
                ErrorCode.SANDBOX_REJECTED,
                "cause must be a string",
                field="cause",
            )
        raw_normalized_action = response.body.get("normalized_action")
        normalized_action: ControlAction | None = None
        if raw_normalized_action is not None:
            try:
                normalized_action = ControlAction(raw_normalized_action)
            except (TypeError, ValueError) as exc:
                raise AgentSdkError(
                    ErrorCode.SANDBOX_REJECTED,
                    "normalized_action is not a defined action",
                    field="normalized_action",
                ) from exc
        raw_normalized_parameters = response.body.get("normalized_parameters")
        if raw_normalized_parameters is not None and not isinstance(
            raw_normalized_parameters, Mapping
        ):
            raise AgentSdkError(
                ErrorCode.SANDBOX_REJECTED,
                "normalized_parameters must be an object",
                field="normalized_parameters",
            )
        if require_normalized and (
            normalized_action is None or raw_normalized_parameters is None
        ):
            raise AgentSdkError(
                ErrorCode.SANDBOX_REJECTED,
                "TEXT control response requires normalized_action and normalized_parameters",
            )
        raw_result = response.body.get("result")
        if raw_result is not None and not isinstance(raw_result, Mapping):
            raise AgentSdkError(
                ErrorCode.SANDBOX_REJECTED,
                "result must be an object",
                field="result",
            )
        context = None
        if raw_context is not None:
            context = ComputingContext(
                compute_service_session_id=session.compute_service_session_id,
                compute_instance_id=session.compute_instance_id,
                binding_ref=session.binding_ref,
                role=session.role,
                agent_id=session.receiver_agent_id,
            )
        return ControlActionStatus(
            request_id=request_id,
            action_id=action_id,
            status=status,
            cause=cause,
            computing_context=context,
            normalized_action=normalized_action,
            normalized_parameters=(
                dict(raw_normalized_parameters)
                if isinstance(raw_normalized_parameters, Mapping)
                else None
            ),
            result=dict(raw_result) if isinstance(raw_result, Mapping) else None,
        )

    @staticmethod
    def _validate_sandbox_timeout(timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "timeout_seconds must be greater than zero",
                field="timeout_seconds",
            )

    @staticmethod
    def _require_consumer_session(
        session: ComputingSession, operation: str
    ) -> None:
        if session.role is not ComputeRole.CONSUMER:
            raise AgentSdkError(
                ErrorCode.COMPUTING_SESSION_INVALID,
                f"{operation} requires the consumer configuration",
                field="role",
            )

    def _parse_recognition_target_response(
        self,
        response: RuntimeHttpResponse,
        session: ComputingSession,
        *,
        expected_request_id: str | None = None,
    ) -> RecognitionTargetStatus:
        if response.status_code != 200:
            error = response.body.get("error")
            code = error.get("code") if isinstance(error, Mapping) else None
            message = error.get("message") if isinstance(error, Mapping) else None
            raise AgentSdkError(
                ErrorCode.SANDBOX_REJECTED,
                str(message or f"Sandbox returned HTTP {response.status_code}"),
                retryable=response.status_code >= 500,
                details={"sandbox_code": code, "status_code": response.status_code},
            )
        request_id = self._require_nonempty_string(
            response.body.get("request_id"),
            "request_id",
            ErrorCode.SANDBOX_REJECTED,
        )
        if expected_request_id is not None and request_id != expected_request_id:
            raise AgentSdkError(
                ErrorCode.SANDBOX_REJECTED,
                "response request_id does not match the recognition request",
                field="request_id",
            )
        expected_context = self._media_context(session)
        if response.body.get("computing_context") != expected_context:
            raise AgentSdkError(
                ErrorCode.SANDBOX_REJECTED,
                "response computing_context does not match C-02",
                field="computing_context",
            )
        if response.body.get("status") != "APPLIED":
            raise AgentSdkError(
                ErrorCode.SANDBOX_REJECTED,
                "recognition target status must be APPLIED",
                field="status",
            )
        revision = response.body.get("target_revision")
        if (
            not isinstance(revision, str)
            or not revision.isdecimal()
            or int(revision) > 0xFFFFFFFFFFFFFFFF
        ):
            raise AgentSdkError(
                ErrorCode.SANDBOX_REJECTED,
                "target_revision must be a uint64 decimal string",
                field="target_revision",
            )
        raw_target = response.body.get("target")
        if not isinstance(raw_target, Mapping):
            raise AgentSdkError(
                ErrorCode.SANDBOX_REJECTED,
                "target must be an object",
                field="target",
            )
        label = self._require_nonempty_string(
            raw_target.get("label"), "target.label", ErrorCode.SANDBOX_REJECTED
        )
        prompt = self._require_nonempty_string(
            raw_target.get("prompt"), "target.prompt", ErrorCode.SANDBOX_REJECTED
        )
        return RecognitionTargetStatus(
            request_id=request_id,
            computing_context=ComputingContext(
                compute_service_session_id=session.compute_service_session_id,
                compute_instance_id=session.compute_instance_id,
                binding_ref=session.binding_ref,
                role=session.role,
                agent_id=session.receiver_agent_id,
            ),
            status="APPLIED",
            target_revision=revision,
            target=RecognitionTarget(label=label, prompt=prompt),
        )

    @staticmethod
    async def _await_media_step(awaitable, timeout_seconds: float, operation: str):
        try:
            return await asyncio.wait_for(awaitable, timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise AgentSdkError(
                ErrorCode.TIMEOUT,
                f"timed out while {operation}",
                retryable=True,
            ) from exc

    @staticmethod
    def _validate_media_codec(
        adapter: MediaOffloadAdapter, session: ComputingSession
    ) -> None:
        codec = session.connection_parameters.video_codec
        if codec is not None and not adapter.supports_video_codec(codec):
            raise AgentSdkError(
                ErrorCode.MEDIA_NEGOTIATION_FAILED,
                f"required video codec is not supported: {codec}",
                field="connection_parameters.video_codec",
            )

    @staticmethod
    def _media_context(session: ComputingSession) -> dict[str, str]:
        return {
            "compute_service_session_id": session.compute_service_session_id,
            "compute_instance_id": session.compute_instance_id,
            "binding_ref": session.binding_ref,
            "role": session.role.value,
            "agent_id": session.receiver_agent_id,
        }

    async def _create_media_connection(
        self,
        session: ComputingSession,
        request_id: str,
        offer_sdp: str,
        timeout_seconds: float,
    ) -> tuple[str, str, str]:
        context = self._media_context(session)
        body = {
            "request_id": request_id,
            "computing_context": context,
            "offer": {"type": "offer", "sdp": offer_sdp},
        }
        response: RuntimeHttpResponse | None = None
        for attempt in range(2):
            try:
                response = await self._sandbox_transport.request_with_status(
                    "POST",
                    self._media_connections_url(session),
                    body,
                    timeout_seconds,
                    session.network_binding.ue_ipv4,
                )
                break
            except AgentSdkError as exc:
                if attempt == 1 or not exc.retryable:
                    raise
        assert response is not None
        if response.status_code != 201:
            error = response.body.get("error")
            code = error.get("code") if isinstance(error, Mapping) else None
            message = error.get("message") if isinstance(error, Mapping) else None
            raise AgentSdkError(
                ErrorCode.MEDIA_NEGOTIATION_FAILED,
                str(message or f"Sandbox returned HTTP {response.status_code}"),
                retryable=response.status_code >= 500,
                details={"sandbox_code": code, "status_code": response.status_code},
            )
        raw_connection_id = response.body.get("media_connection_id")
        try:
            if response.body.get("request_id") != request_id:
                self._invalid_media_response("response request_id does not match the request")
            if response.body.get("computing_context") != context:
                self._invalid_media_response("response computing_context does not match C-02")
            connection_id = self._require_nonempty_string(
                raw_connection_id,
                "media_connection_id",
                ErrorCode.MEDIA_NEGOTIATION_FAILED,
            )
            answer = response.body.get("answer")
            if not isinstance(answer, Mapping) or answer.get("type") != "answer":
                self._invalid_media_response("answer.type must be answer")
            answer_sdp = self._require_nonempty_string(
                answer.get("sdp"), "answer.sdp", ErrorCode.MEDIA_NEGOTIATION_FAILED
            )
        except AgentSdkError:
            if isinstance(raw_connection_id, str) and raw_connection_id:
                try:
                    await self._delete_media_connection(
                        session, raw_connection_id, timeout_seconds
                    )
                except Exception:
                    pass
            raise
        return request_id, connection_id, answer_sdp

    @staticmethod
    def _invalid_media_response(message: str) -> None:
        raise AgentSdkError(ErrorCode.MEDIA_NEGOTIATION_FAILED, message)

    @classmethod
    def _validate_local_media_offer(
        cls, session: ComputingSession, offer_sdp: str
    ) -> None:
        expected_direction = (
            "a=sendonly" if session.role is ComputeRole.PRODUCER else "a=recvonly"
        )
        if expected_direction not in offer_sdp.splitlines():
            raise AgentSdkError(
                ErrorCode.MEDIA_NEGOTIATION_FAILED,
                f"local WebRTC Offer must contain {expected_direction}",
            )
        candidates = cls._candidate_ipv4s(offer_sdp)
        if session.network_binding.ue_ipv4 not in candidates:
            raise AgentSdkError(
                ErrorCode.MEDIA_NEGOTIATION_FAILED,
                "local WebRTC Offer has no ICE candidate for the C-02 UE IPv4 address",
            )
        host_addresses = cls._candidate_ipv4s(offer_sdp, host_only=True)
        if host_addresses != {session.network_binding.ue_ipv4}:
            raise AgentSdkError(
                ErrorCode.MEDIA_NEGOTIATION_FAILED,
                "local WebRTC Offer contains a host candidate outside the C-02 user plane",
            )

    @staticmethod
    def _candidate_ipv4s(sdp: str, *, host_only: bool = False) -> set[str]:
        addresses: set[str] = set()
        for line in sdp.splitlines():
            if not line.startswith("a=candidate:"):
                continue
            parts = line.split()
            if len(parts) < 6:
                continue
            if host_only and (len(parts) < 8 or parts[7].lower() != "host"):
                continue
            try:
                addresses.add(str(ipaddress.IPv4Address(parts[4])))
            except ValueError:
                continue
        return addresses

    async def _install_media_candidate_routes(
        self, session: ComputingSession, answer_sdp: str
    ) -> None:
        expected_direction = (
            "a=recvonly" if session.role is ComputeRole.PRODUCER else "a=sendonly"
        )
        if expected_direction not in answer_sdp.splitlines():
            raise AgentSdkError(
                ErrorCode.MEDIA_NEGOTIATION_FAILED,
                f"Sandbox Answer must contain {expected_direction}",
            )
        codec = session.connection_parameters.video_codec
        if codec is not None:
            wanted = codec.lower().removeprefix("video/")
            negotiated = {
                line.split(None, 1)[1].split("/", 1)[0].lower()
                for line in answer_sdp.splitlines()
                if line.startswith("a=rtpmap:") and len(line.split(None, 1)) == 2
            }
            if wanted not in negotiated:
                raise AgentSdkError(
                    ErrorCode.MEDIA_NEGOTIATION_FAILED,
                    f"Sandbox Answer did not negotiate required video codec {codec}",
                )
        candidates = self._candidate_ipv4s(answer_sdp)
        if not candidates:
            raise AgentSdkError(
                ErrorCode.MEDIA_NEGOTIATION_FAILED,
                "Sandbox Answer has no IPv4 ICE candidate",
            )
        await self._replace_compute_routes(session, candidates)

    async def _close_media_record(
        self, session_id: str, connection_id: str, timeout_seconds: float
    ) -> None:
        record = self._computing_media.get(session_id)
        if record is not None and record.media_connection_id != connection_id:
            return
        session = record.session if record is not None else self._computing_sessions.get(session_id)
        if session is None:
            return
        try:
            await self._delete_media_connection(session, connection_id, timeout_seconds)
            current = self._computing_media.get(session_id)
            if current is not None and current.media_connection_id == connection_id:
                self._computing_media.pop(session_id, None)
        finally:
            if session_id not in self._computing_closing:
                await self._replace_compute_routes(session)

    async def _delete_media_connection(
        self, session: ComputingSession, connection_id: str, timeout_seconds: float
    ) -> None:
        url = f"{self._media_connections_url(session).rstrip('/')}/{quote(connection_id, safe='')}"
        response = await self._sandbox_transport.request_with_status(
            "DELETE", url, None, timeout_seconds, session.network_binding.ue_ipv4
        )
        if response.status_code != 204:
            raise AgentSdkError(
                ErrorCode.MEDIA_NEGOTIATION_FAILED,
                f"Sandbox returned HTTP {response.status_code} for media DELETE",
                retryable=response.status_code >= 500,
                details={"status_code": response.status_code, "response": dict(response.body)},
            )

    async def _validate_compute_request(
        self,
        request: ComputeSessionRequest,
        expected_type: ComputeRequestType,
    ) -> None:
        self._require_ready()
        if not isinstance(request, ComputeSessionRequest):
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "request must be a ComputeSessionRequest",
                field="request",
            )
        if request.message_type != "COMPUTE_SESSION_REQUEST":
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "message_type must be COMPUTE_SESSION_REQUEST",
                field="message_type",
            )
        if request.request_type is not expected_type:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                f"request_type must be {expected_type.value}",
                field="request_type",
            )
        self._validate_compute_request_id(request.request_id, "request_id")
        if not isinstance(request.input_format, ComputeInputFormat):
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "input_format must be a ComputeInputFormat",
                field="input_format",
            )

        if expected_type is ComputeRequestType.CREATE:
            context = request.acn_context
            if not isinstance(context, AcnContext):
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    "acn_context is required for CREATE",
                    field="acn_context",
                )
            for field, value in (
                ("acn_context.group_id", context.group_id),
                ("acn_context.requester_agent_id", context.requester_agent_id),
                ("acn_context.target_agent_id", context.target_agent_id),
            ):
                self._require_nonempty_string(value, field, ErrorCode.INVALID_ARGUMENT)
            if request.ui_locale is not None:
                self._require_nonempty_string(
                    request.ui_locale, "ui_locale", ErrorCode.INVALID_ARGUMENT
                )
            if request.compute_service_session_id is not None or request.target_request_id is not None:
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    "CREATE must not contain a target session or target request",
                )
            if request.input_format is ComputeInputFormat.NATURAL_LANGUAGE:
                self._require_nonempty_string(
                    request.text, "text", ErrorCode.INVALID_ARGUMENT
                )
                if request.constraints is not None:
                    raise AgentSdkError(
                        ErrorCode.INVALID_ARGUMENT,
                        "natural-language CREATE must not contain constraints",
                        field="constraints",
                    )
            else:
                if request.text is not None:
                    raise AgentSdkError(
                        ErrorCode.INVALID_ARGUMENT,
                        "structured CREATE must not contain text",
                        field="text",
                    )
                self._validate_compute_constraints(request.constraints)
            return

        if request.input_format is not ComputeInputFormat.STRUCTURED:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "QUERY, CANCEL and RELEASE require input_format=STRUCTURED",
                field="input_format",
            )
        if any(
            value is not None
            for value in (
                request.acn_context,
                request.text,
                request.constraints,
                request.ui_locale,
            )
        ):
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "non-CREATE requests must not contain CREATE fields",
            )
        has_session = request.compute_service_session_id is not None
        has_target_request = request.target_request_id is not None
        if expected_type in {ComputeRequestType.QUERY, ComputeRequestType.CANCEL}:
            if has_session == has_target_request:
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    "exactly one of compute_service_session_id and target_request_id is required",
                )
        elif not has_session or has_target_request:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "RELEASE requires compute_service_session_id only",
                field="compute_service_session_id",
            )
        if has_session:
            self._require_nonempty_string(
                request.compute_service_session_id,
                "compute_service_session_id",
                ErrorCode.INVALID_ARGUMENT,
            )
        if has_target_request:
            self._validate_compute_request_id(
                request.target_request_id, "target_request_id"
            )

    @classmethod
    def _validate_compute_constraints(
        cls, constraints: ComputeConstraints | None
    ) -> None:
        if not isinstance(constraints, ComputeConstraints):
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "constraints are required for structured CREATE",
                field="constraints",
            )
        cls._require_nonempty_string(
            constraints.capability_id,
            "constraints.capability_id",
            ErrorCode.INVALID_ARGUMENT,
        )
        for field, value in (
            ("constraints.api_version", constraints.api_version),
            ("constraints.image_id", constraints.image_id),
            ("constraints.dnn", constraints.dnn),
            ("constraints.snssai", constraints.snssai),
            ("constraints.placement_region", constraints.placement_region),
            (
                "constraints.data_residency_region",
                constraints.data_residency_region,
            ),
        ):
            if value is not None:
                cls._require_nonempty_string(value, field, ErrorCode.INVALID_ARGUMENT)
        resources = constraints.resources
        if resources is not None:
            if not isinstance(resources, ComputeResources):
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    "constraints.resources must be ComputeResources",
                    field="constraints.resources",
                )
            for field, value in (
                ("cpu_millicores", resources.cpu_millicores),
                ("memory_mib", resources.memory_mib),
                ("gpu_count", resources.gpu_count),
            ):
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not 0 <= value <= 0xFFFFFFFF
                ):
                    raise AgentSdkError(
                        ErrorCode.INVALID_ARGUMENT,
                        f"constraints.resources.{field} must be uint32",
                        field=f"constraints.resources.{field}",
                    )
            if resources.gpu_model is not None:
                cls._require_nonempty_string(
                    resources.gpu_model,
                    "constraints.resources.gpu_model",
                    ErrorCode.INVALID_ARGUMENT,
                )
        duration = constraints.max_duration_ms
        if duration is not None and (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or not 0 <= duration <= 0xFFFFFFFF
        ):
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "constraints.max_duration_ms must be uint32",
                field="constraints.max_duration_ms",
            )
        if constraints.allow_base_qos is not None and not isinstance(
            constraints.allow_base_qos, bool
        ):
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "constraints.allow_base_qos must be a bool",
                field="constraints.allow_base_qos",
            )

    @staticmethod
    def _validate_compute_request_id(value: Any, field: str) -> None:
        if not isinstance(value, str) or not value or not 1 <= len(value.encode("utf-8")) <= 128:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                f"{field} must contain 1..128 UTF-8 bytes",
                field=field,
            )

    @staticmethod
    def _compute_request_body(request: ComputeSessionRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "message_type": request.message_type,
            "request_type": request.request_type.value,
            "input_format": request.input_format.value,
            "request_id": request.request_id,
        }
        if request.acn_context is not None:
            body["acn_context"] = {
                "group_id": request.acn_context.group_id,
                "requester_agent_id": request.acn_context.requester_agent_id,
                "target_agent_id": request.acn_context.target_agent_id,
            }
        if request.text is not None:
            body["text"] = request.text
        if request.constraints is not None:
            constraints: dict[str, Any] = {
                "capability_id": request.constraints.capability_id
            }
            for field in (
                "api_version",
                "image_id",
                "dnn",
                "snssai",
                "allow_base_qos",
                "max_duration_ms",
                "placement_region",
                "data_residency_region",
            ):
                value = getattr(request.constraints, field)
                if value is not None:
                    constraints[field] = value
            if request.constraints.resources is not None:
                resources = {
                    field: getattr(request.constraints.resources, field)
                    for field in (
                        "cpu_millicores",
                        "memory_mib",
                        "gpu_count",
                        "gpu_model",
                    )
                    if getattr(request.constraints.resources, field) is not None
                }
                constraints["resources"] = resources
            body["constraints"] = constraints
        for field in (
            "compute_service_session_id",
            "target_request_id",
            "ui_locale",
        ):
            value = getattr(request, field)
            if value is not None:
                body[field] = value
        return body

    async def _compute_preflight(self) -> None:
        assert self._runtime is not None
        status = await self._runtime.get_acn_status()
        if status.get("ready") is not True:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "GET /v1/acn/status did not report ready=true",
                retryable=True,
                details={"cause": "nas_not_ready"},
            )
        ue_info = await self._runtime.get_ue_info()
        HttpRuntimeTransport.select_ue_agent_ip(ue_info)
        sessions = ue_info.get("pdu_sessions")
        if not isinstance(sessions, list) or not any(
            isinstance(item, Mapping)
            and item.get("state") == "active"
            and item.get("type") == "IPv4"
            for item in sessions
        ):
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "no active IPv4 PDU Session is available",
                details={"cause": "pdu-session-required"},
            )
        accesses = ue_info.get("data_plane_accesses")
        compatible = [
            item
            for item in accesses or ()
            if isinstance(item, Mapping)
            and item.get("access_type") == "HTTP3_CONNECT_IP"
            and item.get("session_selection") == "EXACT_PDU_SESSION_ID"
        ]
        if not compatible:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "Runtime provides no exact HTTP3 CONNECT-IP data-plane access",
                details={"cause": "data-plane-access-unsupported"},
            )
        self._ue_info = ue_info

    async def _send_compute_request(
        self, request: ComputeSessionRequest, timeout_seconds: float
    ) -> ComputeSessionStatus:
        if timeout_seconds <= 0:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "timeout_seconds must be greater than zero",
                field="timeout_seconds",
            )
        body = self._compute_request_body(request)
        async with self._computing_control_lock:
            if self._identity_removal_in_progress:
                raise AgentSdkError(
                    ErrorCode.AGENT_STATE_INVALID,
                    "computing requests cannot start while the Agent Profile is being removed",
                )
            previous = self._compute_requests.get(request.request_id)
            if previous is not None and previous != body:
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    "request_id was already used with different computing content",
                    field="request_id",
                    details={"cause": "idempotency-conflict"},
                )
            self._compute_requests[request.request_id] = body
            self._computing_requests_in_flight.add(request.request_id)
        try:
            await self._compute_preflight()
            if request.request_type is ComputeRequestType.CREATE:
                self._compute_create_requests[request.request_id] = request
            assert self._runtime is not None
            response = await asyncio.wait_for(
                self._runtime.request_with_status(
                    "POST", _COMPUTING_SESSION_REQUEST_PATH, body
                ),
                timeout=timeout_seconds,
            )
            if not isinstance(response, RuntimeHttpResponse):
                raise AgentSdkError(
                    ErrorCode.RUNTIME_REJECTED,
                    "Runtime transport returned an invalid HTTP response",
                )
            allowed_status = (
                {202}
                if request.request_type is ComputeRequestType.CREATE
                else {200}
                if request.request_type is ComputeRequestType.QUERY
                else {200, 202}
            )
            if response.body.get("message_type") == _COMPUTE_SESSION_STATUS:
                if response.status_code not in allowed_status and not 400 <= response.status_code < 500:
                    raise AgentSdkError(
                        ErrorCode.RUNTIME_REJECTED,
                        f"Runtime returned invalid HTTP {response.status_code} for "
                        f"{request.request_type.value}",
                        details={"http_status": response.status_code},
                    )
                result = self._parse_compute_status(response.body)
                if result.request_id != request.request_id:
                    raise AgentSdkError(
                        ErrorCode.RUNTIME_REJECTED,
                        "C-04 request_id does not match the computing request",
                        field="request_id",
                    )
                await self._remember_compute_status(result)
                return self._computing_statuses_by_request.get(request.request_id, result)
            error = response.body.get("error")
            if isinstance(error, Mapping):
                cause = error.get("code")
                error_code = ErrorCode.TIMEOUT if response.status_code == 504 else ErrorCode.RUNTIME_REJECTED
                raise AgentSdkError(
                    error_code,
                    str(error.get("message") or f"Runtime rejected computing request: {cause}"),
                    retryable=response.status_code in {503, 504},
                    details={"cause": cause, "http_status": response.status_code},
                )
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                f"Runtime returned HTTP {response.status_code} without C-04 or error",
            )
        finally:
            async with self._computing_control_lock:
                self._computing_requests_in_flight.discard(request.request_id)

    def _parse_compute_status(
        self, payload: Mapping[str, Any], *, message_type_in_payload: bool = True
    ) -> ComputeSessionStatus:
        if message_type_in_payload and payload.get("message_type") != _COMPUTE_SESSION_STATUS:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "message_type must be COMPUTE_SESSION_STATUS",
                field="message_type",
            )
        request_id = self._require_nonempty_string(
            payload.get("request_id"), "request_id", ErrorCode.RUNTIME_REJECTED
        )
        status = self._require_nonempty_string(
            payload.get("status"), "status", ErrorCode.RUNTIME_REJECTED
        )
        if status not in _COMPUTE_STATUSES:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "status is not a defined computing session status",
                field="status",
            )
        cause = payload.get("cause")
        if not isinstance(cause, str):
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "cause must be a string",
                field="cause",
            )
        session_id = payload.get("compute_service_session_id")
        revision = payload.get("status_revision")
        if session_id is not None:
            session_id = self._require_nonempty_string(
                session_id,
                "compute_service_session_id",
                ErrorCode.RUNTIME_REJECTED,
            )
            if not isinstance(revision, str) or not revision.isdecimal():
                raise AgentSdkError(
                    ErrorCode.RUNTIME_REJECTED,
                    "status_revision must be a uint64 decimal string",
                    field="status_revision",
                )
            parsed_revision = int(revision)
            if not 0 <= parsed_revision <= 0xFFFFFFFFFFFFFFFF:
                raise AgentSdkError(
                    ErrorCode.RUNTIME_REJECTED,
                    "status_revision exceeds uint64",
                    field="status_revision",
                )
        elif revision is not None:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "status_revision requires compute_service_session_id",
                field="status_revision",
            )
        missing = payload.get("missing_fields", [])
        if not isinstance(missing, list) or not all(
            isinstance(item, str) and item for item in missing
        ):
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "missing_fields must be an array of strings",
                field="missing_fields",
            )
        if status == "CLARIFICATION_REQUIRED" and "missing_fields" not in payload:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "CLARIFICATION_REQUIRED requires missing_fields",
                field="missing_fields",
            )
        result = payload.get("result")
        if result is not None and not isinstance(result, Mapping):
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "result must be an object",
                field="result",
            )
        return ComputeSessionStatus(
            message_type=_COMPUTE_SESSION_STATUS,
            request_id=request_id,
            compute_service_session_id=session_id,
            status_revision=revision,
            status=status,
            cause=cause,
            missing_fields=tuple(missing),
            result=dict(result) if isinstance(result, Mapping) else None,
        )

    async def _remember_compute_status(self, status: ComputeSessionStatus) -> bool:
        session_id = status.compute_service_session_id
        if (
            session_id is not None
            and session_id in self._computing_closed_session_ids
            and status.status not in _COMPUTE_TERMINAL_STATUSES
        ):
            self._log(
                logging.INFO,
                "computing_status_ignored_after_local_close",
                compute_service_session_id=session_id,
                status=status.status,
            )
            return False
        if session_id is not None and status.status_revision is not None:
            current = self._computing_statuses.get(session_id)
            if current is not None and current.status_revision is not None:
                if int(status.status_revision) <= int(current.status_revision):
                    current_for_request = self._computing_statuses_by_request.get(
                        status.request_id
                    )
                    if (
                        current_for_request is None
                        or current_for_request.compute_service_session_id != session_id
                        or current_for_request.status_revision is None
                        or int(status.status_revision)
                        > int(current_for_request.status_revision)
                    ):
                        self._computing_statuses_by_request[status.request_id] = status
                    return False
            self._computing_statuses[session_id] = status
        self._computing_statuses_by_request[status.request_id] = status
        async with self._computing_session_changed:
            self._computing_session_changed.notify_all()
        return True

    async def _handle_compute_session_status(
        self, payload: Mapping[str, Any]
    ) -> None:
        await self._remember_compute_status(
            self._parse_compute_status(payload, message_type_in_payload=False)
        )

    @staticmethod
    def _require_object(value: Any, field: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                f"{field} must be an object",
                field=field,
            )
        return value

    @staticmethod
    def _require_uint(value: Any, field: str, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                f"{field} must be an unsigned integer",
                field=field,
            )
        return value

    def _parse_compute_connect_config(
        self, payload: Mapping[str, Any]
    ) -> ComputingSession:
        session_id = self._require_nonempty_string(
            payload.get("compute_service_session_id"),
            "compute_service_session_id",
            ErrorCode.RUNTIME_REJECTED,
        )
        instance_id = self._require_nonempty_string(
            payload.get("compute_instance_id"),
            "compute_instance_id",
            ErrorCode.RUNTIME_REJECTED,
        )
        binding_ref = self._require_nonempty_string(
            payload.get("binding_ref"), "binding_ref", ErrorCode.RUNTIME_REJECTED
        )
        try:
            role = ComputeRole(payload.get("role"))
        except ValueError as exc:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "role must be consumer or producer",
                field="role",
            ) from exc
        receiver_agent_id = self._require_nonempty_string(
            payload.get("receiver_agent_id"),
            "receiver_agent_id",
            ErrorCode.RUNTIME_REJECTED,
        )
        service_endpoint = self._require_service_endpoint(
            payload.get("service_endpoint"), "service_endpoint"
        )
        raw_binding = self._require_object(payload.get("network_binding"), "network_binding")
        raw_snssai = self._require_object(raw_binding.get("snssai"), "network_binding.snssai")
        raw_data_plane = self._require_object(
            raw_binding.get("runtime_data_plane"),
            "network_binding.runtime_data_plane",
        )
        binding = ComputeNetworkBinding(
            pdu_session_id=self._require_uint(
                raw_binding.get("pdu_session_id"),
                "network_binding.pdu_session_id",
                255,
            ),
            dnn=self._require_nonempty_string(
                raw_binding.get("dnn"),
                "network_binding.dnn",
                ErrorCode.RUNTIME_REJECTED,
            ),
            snssai=Snssai(
                sst=self._require_uint(
                    raw_snssai.get("sst"), "network_binding.snssai.sst", 255
                ),
                sd=self._parse_optional_sd(raw_snssai.get("sd")),
            ),
            ue_ipv4=self._require_ipv4(
                raw_binding.get("ue_ipv4"), "network_binding.ue_ipv4"
            ),
            runtime_data_plane=RuntimeDataPlane(
                access_type=self._require_nonempty_string(
                    raw_data_plane.get("access_type"),
                    "network_binding.runtime_data_plane.access_type",
                    ErrorCode.RUNTIME_REJECTED,
                ),
                session_selection=self._require_nonempty_string(
                    raw_data_plane.get("session_selection"),
                    "network_binding.runtime_data_plane.session_selection",
                    ErrorCode.RUNTIME_REJECTED,
                ),
            ),
        )
        raw_parameters = self._require_object(
            payload.get("connection_parameters"), "connection_parameters"
        )
        parameters = ComputeConnectionParameters(
            media_connections_path=self._require_absolute_path(
                raw_parameters.get("media_connections_path"),
                "connection_parameters.media_connections_path",
            ),
            transport=self._require_nonempty_string(
                raw_parameters.get("transport"),
                "connection_parameters.transport",
                ErrorCode.RUNTIME_REJECTED,
            ),
            recognition_target_path_template=self._optional_absolute_path(
                raw_parameters.get("recognition_target_path_template"),
                "connection_parameters.recognition_target_path_template",
            ),
            video_codec=self._optional_nonempty_string(
                raw_parameters.get("video_codec"),
                "connection_parameters.video_codec",
            ),
        )
        if parameters.transport != "WEBRTC":
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "connection_parameters.transport must be WEBRTC",
                field="connection_parameters.transport",
            )
        recognition_path = parameters.recognition_target_path_template
        if recognition_path is not None and recognition_path.count(
            "{compute_service_session_id}"
        ) != 1:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "recognition_target_path_template must contain "
                "{compute_service_session_id} exactly once",
                field="connection_parameters.recognition_target_path_template",
            )
        expires_at = self._parse_optional_datetime(payload.get("expires_at"))
        return ComputingSession(
            compute_service_session_id=session_id,
            compute_instance_id=instance_id,
            binding_ref=binding_ref,
            role=role,
            receiver_agent_id=receiver_agent_id,
            service_endpoint=service_endpoint,
            network_binding=binding,
            connection_parameters=parameters,
            expires_at=expires_at,
        )

    async def _handle_compute_connect_config(
        self, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        try:
            session = self._parse_compute_connect_config(payload)
        except AgentSdkError:
            return self._raw_compute_config_ack(payload, False, "invalid-request")
        key = (
            session.binding_ref,
            session.role.value,
            session.receiver_agent_id,
        )
        if self._identity_removal_in_progress:
            return self._compute_config_ack(session, False, "session-closed")
        if session.compute_service_session_id in self._computing_closed_session_ids:
            return self._compute_config_ack(session, False, "session-closed")
        if key in self._computing_close_results:
            return self._compute_config_ack(session, False, "session-closed")
        status = self._computing_statuses.get(session.compute_service_session_id)
        if status is not None and status.status in _COMPUTE_TERMINAL_STATUSES:
            return self._compute_config_ack(session, False, "session-closed")
        current = self._computing_sessions.get(session.compute_service_session_id)
        if current is not None:
            if current == session:
                return self._compute_config_ack(session, True, "")
            return self._compute_config_ack(session, False, "config-conflict")
        binding_match = next(
            (
                configured
                for configured in self._computing_sessions.values()
                if (
                    configured.binding_ref,
                    configured.role.value,
                    configured.receiver_agent_id,
                )
                == key
            ),
            None,
        )
        if binding_match is not None:
            return self._compute_config_ack(session, False, "config-conflict")
        try:
            await self._validate_and_install_compute_binding(session)
        except AgentSdkError as exc:
            cause = str(exc.details.get("cause") or "config-rejected")
            return self._compute_config_ack(session, False, cause)
        status = self._computing_statuses.get(session.compute_service_session_id)
        profile_matches = (
            self._profile is not None
            and self._profile.agent_id == session.receiver_agent_id
        )
        if (
            self._identity_removal_in_progress
            or not profile_matches
            or (status is not None and status.status in _COMPUTE_TERMINAL_STATUSES)
        ):
            assert self._routes is not None
            await self._routes.replace_group_peers(
                self._computing_route_key(session.binding_ref), set()
            )
            return self._compute_config_ack(session, False, "session-closed")
        self._computing_sessions[session.compute_service_session_id] = session
        async with self._computing_session_changed:
            self._computing_session_changed.notify_all()
        return self._compute_config_ack(session, True, "")

    async def _validate_and_install_compute_binding(
        self, session: ComputingSession
    ) -> None:
        if self._profile is None or session.receiver_agent_id != self._profile.agent_id:
            self._raise_compute_binding_error(
                "binding-mismatch", "receiver_agent_id does not match the local Agent"
            )
        binding = session.network_binding
        if (
            binding.runtime_data_plane.access_type != "HTTP3_CONNECT_IP"
            or binding.runtime_data_plane.session_selection != "EXACT_PDU_SESSION_ID"
        ):
            self._raise_compute_binding_error(
                "data-plane-access-unsupported", "unsupported Runtime data-plane mapping"
            )
        ue_info = self._ue_info
        if ue_info is None:
            assert self._runtime is not None
            ue_info = await self._runtime.get_ue_info()
            self._ue_info = ue_info
        sessions = ue_info.get("pdu_sessions")
        matching = [
            item
            for item in sessions or ()
            if isinstance(item, Mapping)
            and item.get("pdu_session_id") == binding.pdu_session_id
            and item.get("state") == "active"
            and item.get("type") == "IPv4"
        ]
        if len(matching) != 1:
            self._raise_compute_binding_error(
                "pdu-session-not-found", "configured PDU Session is not active"
            )
        pdu = matching[0]
        raw_snssai = pdu.get("snssai")
        local_sd = raw_snssai.get("sd") if isinstance(raw_snssai, Mapping) else None
        local_sst = raw_snssai.get("sst") if isinstance(raw_snssai, Mapping) else None
        try:
            local_ip = str(ipaddress.IPv4Address(str(pdu.get("ipv4"))))
        except ValueError:
            self._raise_compute_binding_error(
                "network-binding-mismatch", "local PDU Session has an invalid IPv4 address"
            )
        if (
            pdu.get("dnn") != binding.dnn
            or local_sst != binding.snssai.sst
            or local_sd != binding.snssai.sd
            or local_ip != binding.ue_ipv4
            or self._config is None
            or self._config.agent_tun_ip != binding.ue_ipv4
        ):
            self._raise_compute_binding_error(
                "network-binding-mismatch", "C-02 does not match the local PDU Session"
            )
        accesses = ue_info.get("data_plane_accesses")
        matching_accesses = [
            item
            for item in accesses or ()
            if isinstance(item, Mapping)
            and item.get("access_type") == binding.runtime_data_plane.access_type
            and item.get("session_selection") == binding.runtime_data_plane.session_selection
        ]
        if len(matching_accesses) != 1:
            self._raise_compute_binding_error(
                "data-plane-access-unsupported", "no unique matching data-plane access"
            )
        template = matching_accesses[0].get("endpoint_template")
        if not isinstance(template, str) or template.count("{pdu_session_id}") != 1:
            self._raise_compute_binding_error(
                "data-plane-access-unsupported", "invalid data-plane endpoint template"
            )
        expanded = template.replace("{pdu_session_id}", str(binding.pdu_session_id))
        try:
            parsed_access = urlsplit(expanded)
        except ValueError:
            parsed_access = None
        if (
            parsed_access is None
            or parsed_access.scheme != "https"
            or not parsed_access.hostname
            or parsed_access.username is not None
            or parsed_access.fragment
            or self._masque is None
            or not self._masque.connected
        ):
            self._raise_compute_binding_error(
                "data-plane-access-unsupported", "CONNECT-IP data plane is unavailable"
            )
        await self._install_compute_route(session)

    async def _install_compute_route(self, session: ComputingSession) -> None:
        await self._replace_compute_routes(session)

    async def _replace_compute_routes(
        self, session: ComputingSession, extra_addresses: set[str] | None = None
    ) -> None:
        host = urlsplit(session.service_endpoint).hostname
        assert host is not None and self._routes is not None
        try:
            addresses = {str(ipaddress.IPv4Address(host))}
        except ValueError:
            resolved = await asyncio.to_thread(
                socket.getaddrinfo, host, None, socket.AF_INET, socket.SOCK_STREAM
            )
            addresses = {item[4][0] for item in resolved}
        if not addresses:
            self._raise_compute_binding_error(
                "config-rejected", "service_endpoint host cannot be resolved"
            )
        addresses.update(extra_addresses or ())
        await self._routes.replace_group_peers(
            self._computing_route_key(session.binding_ref), addresses
        )

    @staticmethod
    def _raise_compute_binding_error(cause: str, message: str) -> None:
        raise AgentSdkError(
            ErrorCode.RUNTIME_REJECTED, message, details={"cause": cause}
        )

    @staticmethod
    def _network_binding_body(binding: ComputeNetworkBinding) -> dict[str, Any]:
        snssai: dict[str, Any] = {"sst": binding.snssai.sst}
        if binding.snssai.sd is not None:
            snssai["sd"] = binding.snssai.sd
        return {
            "pdu_session_id": binding.pdu_session_id,
            "dnn": binding.dnn,
            "snssai": snssai,
            "ue_ipv4": binding.ue_ipv4,
            "runtime_data_plane": {
                "access_type": binding.runtime_data_plane.access_type,
                "session_selection": binding.runtime_data_plane.session_selection,
            },
        }

    @classmethod
    def _compute_config_ack(
        cls, session: ComputingSession, accepted: bool, cause: str
    ) -> Mapping[str, Any]:
        return {
            "compute_service_session_id": session.compute_service_session_id,
            "compute_instance_id": session.compute_instance_id,
            "binding_ref": session.binding_ref,
            "role": session.role.value,
            "receiver_agent_id": session.receiver_agent_id,
            "network_binding": cls._network_binding_body(session.network_binding),
            "accepted": accepted,
            "cause": cause,
        }

    @staticmethod
    def _raw_compute_config_ack(
        payload: Mapping[str, Any], accepted: bool, cause: str
    ) -> Mapping[str, Any]:
        fields = (
            "compute_service_session_id",
            "compute_instance_id",
            "binding_ref",
            "role",
            "receiver_agent_id",
            "network_binding",
        )
        return {
            **{field: payload[field] for field in fields if field in payload},
            "accepted": accepted,
            "cause": cause,
        }

    async def _handle_compute_session_close(
        self, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        field_names = (
                "compute_service_session_id",
                "compute_instance_id",
                "binding_ref",
                "role",
                "receiver_agent_id",
        )
        try:
            fields = {
                name: self._require_nonempty_string(
                    payload.get(name), name, ErrorCode.RUNTIME_REJECTED
                )
                for name in field_names
            }
        except AgentSdkError:
            return {
                **{
                    name: payload[name]
                    for name in field_names
                    if isinstance(payload.get(name), str) and payload[name]
                },
                "closed": False,
                "cause": "invalid-request",
            }
        close_cause = payload.get("cause")
        if not isinstance(close_cause, str):
            return {**fields, "closed": False, "cause": "invalid-request"}
        if fields["role"] not in {role.value for role in ComputeRole}:
            return {**fields, "closed": False, "cause": "binding-mismatch"}
        key = (fields["binding_ref"], fields["role"], fields["receiver_agent_id"])
        previous = self._computing_close_results.get(key)
        if previous is not None:
            return previous
        session = self._computing_sessions.get(fields["compute_service_session_id"])
        response_base = {
            name: fields[name]
            for name in (
                "compute_service_session_id",
                "compute_instance_id",
                "binding_ref",
                "role",
                "receiver_agent_id",
            )
        }
        if (
            session is None
            or session.compute_instance_id != fields["compute_instance_id"]
            or session.binding_ref != fields["binding_ref"]
            or session.role.value != fields["role"]
            or session.receiver_agent_id != fields["receiver_agent_id"]
        ):
            result = {**response_base, "closed": False, "cause": "binding-mismatch"}
            return result
        self._computing_closing.add(session.compute_service_session_id)
        try:
            pending = self._computing_pending_media.pop(
                session.compute_service_session_id, None
            )
            if pending is not None:
                try:
                    await pending.prepared.abort()
                except Exception:
                    pass
            media = self._computing_media.get(session.compute_service_session_id)
            if media is not None:
                try:
                    if session.role is ComputeRole.PRODUCER:
                        await media.managed.stop()  # type: ignore[attr-defined]
                    else:
                        await media.managed.close()  # type: ignore[attr-defined]
                except Exception:
                    # C-05 still tears down the local binding. Remote media cleanup
                    # converges through CMF UnbindComputeSession.
                    pass
            self._computing_media.pop(session.compute_service_session_id, None)
            assert self._routes is not None
            await self._routes.replace_group_peers(
                self._computing_route_key(session.binding_ref), set()
            )
            self._computing_sessions.pop(session.compute_service_session_id, None)
            self._computing_closed_session_ids.add(session.compute_service_session_id)
            result = {**response_base, "closed": True, "cause": ""}
        except Exception:
            result = {**response_base, "closed": False, "cause": "runtime-unhealthy"}
        self._computing_close_results[key] = result
        async with self._computing_session_changed:
            self._computing_session_changed.notify_all()
        return result

    async def _wait_for_computing_session(
        self, compute_service_session_id: str, timeout_seconds: float
    ) -> ComputingSession:
        session_id = self._require_nonempty_string(
            compute_service_session_id,
            "compute_service_session_id",
            ErrorCode.INVALID_ARGUMENT,
        )

        async def wait() -> ComputingSession:
            async with self._computing_session_changed:
                while True:
                    status = self._computing_statuses.get(session_id)
                    if status is not None and status.status in _COMPUTE_TERMINAL_STATUSES:
                        raise AgentSdkError(
                            ErrorCode.COMPUTING_SESSION_INVALID,
                            f"computing session ended in state {status.status}",
                        )
                    if session_id in self._computing_sessions:
                        return self._computing_sessions[session_id]
                    await self._computing_session_changed.wait()

        try:
            return await asyncio.wait_for(wait(), timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise AgentSdkError(
                ErrorCode.TIMEOUT,
                f"timed out waiting for C-02 for computing session {session_id}",
                retryable=True,
            ) from exc

    async def _recover_compute_statuses(self) -> None:
        for create_request in tuple(self._compute_create_requests.values()):
            known = self._computing_statuses_by_request.get(create_request.request_id)
            if known is not None and known.compute_service_session_id is not None:
                known = self._computing_statuses.get(
                    known.compute_service_session_id, known
                )
            if known is not None and known.status in _COMPUTE_TERMINAL_STATUSES:
                continue
            recovery = ComputeSessionRequest(
                message_type="COMPUTE_SESSION_REQUEST",
                request_type=ComputeRequestType.QUERY,
                input_format=ComputeInputFormat.STRUCTURED,
                request_id=str(uuid.uuid4()),
                target_request_id=create_request.request_id,
            )
            try:
                await self._send_compute_request(recovery, 30.0)
            except Exception as exc:
                self._log(
                    logging.WARNING,
                    "compute_status_recovery_failed",
                    request_id=create_request.request_id,
                    error=str(exc),
                )

    @staticmethod
    def _parse_optional_sd(value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) != 6:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "network_binding.snssai.sd must be six hexadecimal characters",
                field="network_binding.snssai.sd",
            )
        try:
            int(value, 16)
        except ValueError as exc:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "network_binding.snssai.sd must be six hexadecimal characters",
                field="network_binding.snssai.sd",
            ) from exc
        return value

    @classmethod
    def _require_ipv4(cls, value: Any, field: str) -> str:
        text = cls._require_nonempty_string(value, field, ErrorCode.RUNTIME_REJECTED)
        try:
            return str(ipaddress.IPv4Address(text))
        except ValueError as exc:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                f"{field} must be an IPv4 literal",
                field=field,
            ) from exc

    @classmethod
    def _require_service_endpoint(cls, value: Any, field: str) -> str:
        text = cls._require_nonempty_string(value, field, ErrorCode.RUNTIME_REJECTED)
        try:
            parsed = urlsplit(text)
            port = parsed.port
        except ValueError as exc:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED, f"{field} is invalid", field=field
            ) from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port is not None and not 1 <= port <= 65535
        ):
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                f"{field} must be an absolute HTTP or HTTPS URI",
                field=field,
            )
        return text.rstrip("/")

    @classmethod
    def _require_absolute_path(cls, value: Any, field: str) -> str:
        text = cls._require_nonempty_string(value, field, ErrorCode.RUNTIME_REJECTED)
        if not text.startswith("/") or urlsplit(text).scheme or urlsplit(text).netloc:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                f"{field} must be an absolute path",
                field=field,
            )
        return text

    @classmethod
    def _optional_absolute_path(cls, value: Any, field: str) -> str | None:
        return None if value is None else cls._require_absolute_path(value, field)

    @classmethod
    def _optional_nonempty_string(cls, value: Any, field: str) -> str | None:
        return None if value is None else cls._require_nonempty_string(
            value, field, ErrorCode.RUNTIME_REJECTED
        )

    @staticmethod
    def _computing_route_key(binding_ref: str) -> str:
        return f"computing:{binding_ref}"

    @staticmethod
    def _media_connections_url(session: ComputingSession) -> str:
        return urljoin(
            session.service_endpoint.rstrip("/") + "/",
            session.connection_parameters.media_connections_path,
        )

    @staticmethod
    def _recognition_target_url(session: ComputingSession) -> str:
        template = session.connection_parameters.recognition_target_path_template
        if template is None:
            raise AgentSdkError(
                ErrorCode.COMPUTING_SESSION_INVALID,
                "C-02 does not provide recognition_target_path_template",
                field="connection_parameters.recognition_target_path_template",
            )
        path = template.replace(
            "{compute_service_session_id}",
            quote(session.compute_service_session_id, safe=""),
        )
        return urljoin(session.service_endpoint.rstrip("/") + "/", path)

    @staticmethod
    def _control_actions_url(session: ComputingSession) -> str:
        return urljoin(session.service_endpoint.rstrip("/") + "/", "/v1/control-actions")

    @staticmethod
    def _require_nonempty_string(
        value: Any,
        field: str,
        error_code: ErrorCode,
    ) -> str:
        if not isinstance(value, str) or not value.strip():
            raise AgentSdkError(
                error_code,
                f"{field} must be a non-empty string",
                field=field,
            )
        return value.strip()

    @classmethod
    def _require_ip_address(
        cls,
        value: Any,
        field: str,
        error_code: ErrorCode,
    ) -> str:
        text = cls._require_nonempty_string(value, field, error_code)
        try:
            return str(ipaddress.ip_address(text))
        except ValueError as exc:
            raise AgentSdkError(
                error_code,
                f"{field} must be an IP address",
                field=field,
            ) from exc

    def _require_media_adapter(self) -> MediaOffloadAdapter:
        if self._media_offload_adapter is None:
            from .webrtc import AiortcMediaOffloadAdapter

            self._media_offload_adapter = AiortcMediaOffloadAdapter()
        return self._media_offload_adapter

    async def _operation(
        self, method: str, path: str, body: Mapping[str, Any]
    ) -> OperationResult:
        self._require_ready()
        assert self._runtime is not None
        response = await self._runtime.request(method, path, body)
        return OperationResult(
            bool(response.get("success", True)),
            str(response.get("operation_id", "")),
            str(response.get("message", "")),
        )

    def _apply_capability_updates(
        self,
        published_vcs: Sequence[Mapping[str, Any]],
        update_items: Sequence[Mapping[str, Any]],
        credentials: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        if not update_items:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "update_items must contain at least one item",
                field="update_items",
            )
        result = [dict(item) for item in published_vcs]
        supplied = [dict(item) for item in credentials]

        def credential_id(credential: Mapping[str, Any]) -> str | None:
            value = credential.get("id")
            return value if isinstance(value, str) and value else None

        def add_or_replace(credential: Mapping[str, Any]) -> None:
            identifier = credential_id(credential)
            if identifier is None:
                result.append(dict(credential))
                return
            for index, current in enumerate(result):
                if credential_id(current) == identifier:
                    result[index] = dict(credential)
                    return
            result.append(dict(credential))

        for credential in supplied:
            add_or_replace(credential)

        for index, item in enumerate(update_items):
            if not isinstance(item, Mapping):
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    "each update item must be an object",
                    field=f"update_items[{index}]",
                )
            update_type = item.get("update_type")
            skill_name = item.get("skill_name")
            if update_type not in {"add_skill", "remove_skill"}:
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    "update_type must be add_skill or remove_skill",
                    field=f"update_items[{index}].update_type",
                )
            if not isinstance(skill_name, str) or not skill_name:
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    "skill_name must be a non-empty string",
                    field=f"update_items[{index}].skill_name",
                )
            reference_id = item.get("reference_vc_id")
            if update_type == "add_skill":
                if not isinstance(reference_id, str) or not reference_id:
                    raise AgentSdkError(
                        ErrorCode.INVALID_ARGUMENT,
                        "add_skill requires reference_vc_id",
                        field=f"update_items[{index}].reference_vc_id",
                    )
                referenced = next(
                    (
                        credential
                        for credential in supplied
                        if credential_id(credential) == reference_id
                    ),
                    None,
                )
                if referenced is None:
                    raise AgentSdkError(
                        ErrorCode.INVALID_ARGUMENT,
                        "reference_vc_id was not provided in credentials",
                        field=f"update_items[{index}].reference_vc_id",
                    )
                add_or_replace(referenced)
                if (
                    self._credential_skill_name(referenced) != skill_name
                    and not any(
                        self._credential_skill_name(credential) == skill_name
                        for credential in result
                    )
                ):
                    if self._profile is None:
                        raise AgentSdkError(
                            ErrorCode.AGENT_STATE_INVALID,
                            "local profile is unavailable while updating the Agent Card snapshot",
                        )
                    for generated in issue_test_capability_vcs(
                        agent_id=self._profile.agent_id,
                        agent_name=self._profile.agent_name,
                        capabilities=[skill_name],
                    ):
                        add_or_replace(generated)
            else:
                result = [
                    credential
                    for credential in result
                    if self._credential_skill_name(credential) != skill_name
                    and (
                        not isinstance(reference_id, str)
                        or credential_id(credential) != reference_id
                    )
                ]
        if not result:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "capability update would produce an empty Agent Card",
                field="update_items",
            )
        return result

    @staticmethod
    def _ensure_credentials_rebindable(
        credentials: Sequence[Mapping[str, Any]],
        *,
        old_agent_id: str,
    ) -> None:
        for credential in credentials:
            claims = credential.get("claims")
            claims_mapping = claims if isinstance(claims, Mapping) else {}
            is_network_ability = any(
                field in claims_mapping
                for field in ("network_abilities", "abilities", "agent_attribute")
            ) and "skill_name" not in claims_mapping
            is_reissuable_test_capability = (
                credential.get("issuer") == TEST_CAPABILITY_ISSUER_DID
                and isinstance(claims_mapping.get("skill_name"), str)
            )
            if (
                claims_mapping.get("agent_id") == old_agent_id
                and not is_network_ability
                and not is_reissuable_test_capability
            ):
                raise AgentSdkError(
                    ErrorCode.CREDENTIAL_EXPIRED,
                    "a requested capability credential is bound to the current Agent ID "
                    "but cannot be reissued after identity replacement",
                    field="credentials",
                )

    async def _refresh_rebound_credentials(
        self,
        credentials: Sequence[Mapping[str, Any]],
        *,
        old_agent_id: str,
        new_profile: AgentProfile,
    ) -> list[dict[str, Any]]:
        refreshed: list[dict[str, Any]] = []
        network_refreshed = False
        for credential in credentials:
            claims = credential.get("claims")
            claims_mapping = claims if isinstance(claims, Mapping) else {}
            is_network_ability = any(
                field in claims_mapping
                for field in ("network_abilities", "abilities", "agent_attribute")
            ) and "skill_name" not in claims_mapping
            if is_network_ability:
                if not network_refreshed:
                    ability = await self.get_network_ability(new_profile.agent_id)
                    refreshed.append(dict(ability.ability_vc))
                    network_refreshed = True
                continue
            if (
                credential.get("issuer") == TEST_CAPABILITY_ISSUER_DID
                and isinstance(claims_mapping.get("skill_name"), str)
            ):
                refreshed.extend(
                    issue_test_capability_vcs(
                        agent_id=new_profile.agent_id,
                        agent_name=new_profile.agent_name,
                        capabilities=[str(claims_mapping["skill_name"])],
                    )
                )
                continue
            if claims_mapping.get("agent_id") == old_agent_id:
                raise AgentSdkError(
                    ErrorCode.CREDENTIAL_EXPIRED,
                    "an existing capability credential is bound to the deregistered Agent ID "
                    "and cannot be reissued by the SDK",
                    field="credentials",
                )
            refreshed.append(dict(credential))
        return refreshed

    @staticmethod
    def _credential_skill_name(credential: Mapping[str, Any]) -> str | None:
        claims = credential.get("claims")
        if not isinstance(claims, Mapping):
            return None
        skill_name = claims.get("skill_name")
        return skill_name if isinstance(skill_name, str) else None

    async def _authenticate_control_request(
        self, path: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        authentication = await self._control_request_authenticator.authenticate(
            path, body
        )
        overlap = set(body).intersection(authentication)
        if overlap:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "control request authenticator overwrote business fields: "
                f"{sorted(overlap)}",
            )
        return {**body, **authentication}

    @staticmethod
    def _validate_identity_application(
        owner: str,
        name: str,
        description: str,
        metadata: Mapping[str, Any] | None,
    ) -> None:
        for field, value, maximum in (
            ("owner", owner, 128),
            ("name", name, 128),
            ("description", description, 512),
        ):
            if not isinstance(value, str) or not (1 <= len(value) <= maximum):
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"{field} length must be in 1..{maximum}",
                    field=field,
                )
        if not isinstance(metadata, Mapping):
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "metadata must be a JSON object",
                field="metadata",
            )
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in metadata.items()
        ):
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "metadata keys and values must be strings",
                field="metadata",
            )
        for field in ("region", "os", "version"):
            value = metadata.get(field)
            if not isinstance(value, str) or not value:
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"metadata.{field} must be a non-empty string",
                    field=f"metadata.{field}",
                )

    @staticmethod
    def _normalize_identity_metadata(metadata: Mapping[str, Any]) -> dict[str, str]:
        required = ("region", "os", "version")
        ordered_keys = (*required, *sorted(set(metadata).difference(required)))
        return {key: str(metadata[key]) for key in ordered_keys}

    @staticmethod
    def _require_response_object(
        response: Mapping[str, Any], field: str
    ) -> Mapping[str, Any]:
        value = response.get(field)
        if not isinstance(value, Mapping):
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                f"Runtime response field {field} must be an object",
                field=field,
            )
        return value

    @staticmethod
    def _parse_optional_datetime(
        value: Any,
        *,
        error_code: ErrorCode = ErrorCode.RUNTIME_REJECTED,
    ) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AgentSdkError(
                error_code,
                "Response data contains an invalid RFC3339 timestamp",
            ) from exc
        if parsed.tzinfo is None:
            raise AgentSdkError(
                error_code,
                "Response data contains an RFC3339 timestamp without a timezone",
            )
        return parsed.astimezone(timezone.utc)

    @logged_async
    async def get_group_snapshot(self, group_id: str) -> GroupConfigSnapshot | None:
        if self._groups is None:
            return None
        return await self._groups.snapshot(group_id)

    async def close(self) -> None:
        started = time.perf_counter()
        self._log(logging.INFO, "function_enter", function="close", arguments={})
        if self._state in {"CLOSING", "CLOSED"}:
            self._log(
                logging.INFO,
                "function_exit",
                function="close",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                result=None,
            )
            return
        try:
            self._state = "CLOSING"
            self._computing_closing.update(self._computing_sessions)
            self._computing_closing.update(self._computing_pending_media)
            # Drain the inbound HTTP server while routes, MASQUE and the TUN are
            # still alive.  In particular, an application may call close as
            # soon as its A2A listener returns; closing MASQUE first can discard
            # the listener's HTTP response before it reaches the sender.
            if self._server is not None:
                await self._server.close()
            if self._pump_task is not None:
                self._pump_task.cancel()
                await asyncio.gather(self._pump_task, return_exceptions=True)
                self._pump_task = None
            for pending in tuple(self._computing_pending_media.values()):
                try:
                    await pending.prepared.abort()
                except Exception:
                    pass
            self._computing_pending_media.clear()
            for record in tuple(self._computing_media.values()):
                try:
                    if record.session.role is ComputeRole.PRODUCER:
                        await record.managed.stop()  # type: ignore[attr-defined]
                    else:
                        await record.managed.close()  # type: ignore[attr-defined]
                except Exception:
                    pass
            if self._groups is not None:
                await self._groups.close()
            if self._routes is not None:
                await self._routes.close()
            if self._masque is not None:
                await self._masque.close()
            if self._runtime is not None:
                await self._runtime.close()
            await self._sandbox_transport.close()
            if self._tun is not None:
                await self._tun.close()
            if self._media_offload_adapter is not None:
                await self._media_offload_adapter.close()
            self._compute_requests.clear()
            self._compute_create_requests.clear()
            self._computing_requests_in_flight.clear()
            self._computing_closed_session_ids.clear()
            self._computing_statuses.clear()
            self._computing_statuses_by_request.clear()
            self._computing_sessions.clear()
            self._computing_media.clear()
            self._computing_pending_media.clear()
            self._computing_media_locks.clear()
            self._computing_closing.clear()
            self._computing_close_results.clear()
            self._identity_removal_in_progress = False
            self._received_a2a_message_ids.clear()
            self._ue_info = None
            self._state = "CLOSED"
        except Exception as exc:
            self._log(
                logging.ERROR,
                "function_error",
                exc_info=True,
                function="close",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                error_type=type(exc).__name__,
                error=str(exc),
                error_code=getattr(getattr(exc, "code", None), "value", None),
            )
            raise
        else:
            self._log(
                logging.INFO,
                "function_exit",
                function="close",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                result=None,
            )
        finally:
            close_logger(self._logger)
