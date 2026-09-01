from __future__ import annotations

import asyncio
import functools
import inspect
import ipaddress
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    RuntimeTransport,
    TunDevice,
    RemoteVideoStream,
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
    DiscoveredAgent,
    GroupConfigSnapshot,
    GroupInfo,
    MessageReceipt,
    NetworkAbility,
    NetworkMessageAction,
    NetworkMessageType,
    OffloadingSession,
    OperationResult,
    SdkInitResult,
)
from .rest_server import AiohttpLocalServer
from .routes import GroupRouteManager, Pyroute2RouteBackend, RouteBackend
from .runtime import HttpPeerMessenger, HttpRuntimeTransport
from .security import (
    DeviceControlRequestAuthenticator,
    DeviceMessageSigner,
    DeviceSigningIdentityStore,
    DisabledMessageSignatureVerifier,
    DisabledProofVerifier,
)
from .tun import LinuxTunDevice, validate_ip_packet

TunFactory = Callable[[str, str, int], Awaitable[TunDevice]]
MasqueFactory = Callable[[SdkConfig], ConnectIpTransport]
RuntimeFactory = Callable[[str, int], RuntimeTransport]
ServerFactory = Callable[[], LocalServer]
RouteBackendFactory = Callable[[SdkConfig, TunDevice], RouteBackend]

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
        media_offload_adapter: MediaOffloadAdapter | None = None,
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
                local_address=config.local_vlan_ip,
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
        self._media_offload_adapter = media_offload_adapter
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
        self._offloading_sessions: dict[str, OffloadingSession] = {}

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
        local_vlan_ip: str,
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
                "local_vlan_ip": local_vlan_ip,
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
                local_vlan_ip=local_vlan_ip,
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
            agent_tun_ip = await self._runtime.get_ue_agent_ip()
            config = SdkConfig.validate(
                agent_runtime_ip=agent_runtime_ip,
                agent_runtime_port=agent_runtime_port,
                local_vlan_ip=local_vlan_ip,
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
                physical_ip=config.local_vlan_ip,
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
            await self._runtime.start_downlink(self._handle_runtime_downlink)
            result = SdkInitResult(
                runtime_connected=True,
                masque_connected=self._masque.connected,
                local_tcp_endpoint=f"{config.local_vlan_ip}:{config.local_tcp_port}",
                local_udp_endpoint=f"{config.local_vlan_ip}:{config.local_udp_port}",
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
    ) -> NetworkMessageAction:
        self._log(
            logging.INFO,
            "runtime_downlink_dispatch",
            message_type=message_type,
            transaction_id=transaction_id,
        )
        if message_type == "ACN_AGENT_GROUPING_INVITATION":
            return await self._handle_group_invitation(payload)
        if message_type == "ACN_AGENT_GROUPING_NOTIFICATION":
            return await self._handle_group_config(payload)
        if self._network_listener is None:
            return NetworkMessageAction.REJECT
        return await self._network_listener.on_network_message(
            NetworkMessageType.UNKNOWN, payload
        )

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
        await self._groups.commit(candidate, local_agent_id=self._profile.agent_id)
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
        await self._group_listener.on_group_message(group_id, sender_id, user_payload)

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
        self._clear_agent_state()
        return OperationResult(
            True,
            "",
            "Local Agent state reset to NO_IDENTITY; network identity was not changed",
        )

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
        info = GroupInfo(str(response["group_id"]), group_name)
        self._group_info[info.group_id] = info
        return info

    @logged_async
    async def create_offloading_session(
        self,
        agent_id: str,
        workload_type: str,
        sandbox_id: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> OffloadingSession:
        self._require_ready()
        if timeout_seconds <= 0:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "timeout_seconds must be greater than zero",
                field="timeout_seconds",
            )
        assert self._runtime is not None
        path = "/compute/v1/offloading-sessions"
        request: dict[str, Any] = {
            "request_id": str(uuid.uuid4()),
            "agent_id": agent_id,
            "workload_type": workload_type,
        }
        if sandbox_id is not None:
            request["preferred_sandbox_id"] = sandbox_id
        body = await self._authenticate_control_request(path, request)
        response = await self._runtime.request(
            "POST",
            path,
            body,
        )
        expires_at = response.get("expires_at")
        parsed_expires_at = None
        if isinstance(expires_at, str) and expires_at:
            parsed_expires_at = datetime.fromisoformat(
                expires_at.replace("Z", "+00:00")
            )
        session = OffloadingSession(
            session_id=str(response["session_id"]),
            sandbox_id=str(response.get("sandbox_id", "")),
            state=str(response.get("state", "CONNECTING")),
            expires_at=parsed_expires_at,
            metadata=dict(response),
        )
        if self._media_offload_adapter is not None:
            await asyncio.wait_for(
                self._media_offload_adapter.connect(
                    session, response, timeout_seconds
                ),
                timeout=timeout_seconds,
            )
            session.state = "CONNECTED"
        self._offloading_sessions[session.session_id] = session
        return session

    @logged_async
    async def start_video_upload(
        self,
        session_id: str,
        camera_id: int = 0,
        width: int = 1920,
        height: int = 1080,
        fps: int = 30,
        bitrate_kbps: int = 4000,
    ) -> VideoUploadHandle:
        session = self._require_offloading_session(session_id)
        adapter = self._require_media_adapter()
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
        return await adapter.start_video_upload(
            session,
            camera_id=camera_id,
            width=width,
            height=height,
            fps=fps,
            bitrate_kbps=bitrate_kbps,
        )

    @logged_async
    async def get_processed_video_stream(
        self,
        session_id: str,
        timeout_seconds: float = 10.0,
    ) -> RemoteVideoStream:
        if timeout_seconds <= 0:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "timeout_seconds must be greater than zero",
                field="timeout_seconds",
            )
        session = self._require_offloading_session(session_id)
        adapter = self._require_media_adapter()
        return await asyncio.wait_for(
            adapter.get_processed_video_stream(session, timeout_seconds),
            timeout=timeout_seconds,
        )

    def _require_offloading_session(self, session_id: str) -> OffloadingSession:
        self._require_ready()
        session = self._offloading_sessions.get(session_id)
        if session is None or session.state != "CONNECTED":
            raise AgentSdkError(
                ErrorCode.OFFLOADING_SESSION_NOT_FOUND,
                f"connected offloading session {session_id} was not found",
            )
        return session

    def _require_media_adapter(self) -> MediaOffloadAdapter:
        if self._media_offload_adapter is None:
            raise AgentSdkError(
                ErrorCode.OFFLOADING_SESSION_NOT_FOUND,
                "no WebRTC media adapter is configured",
            )
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
    def _parse_optional_datetime(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "Runtime response contains an invalid RFC3339 timestamp",
            ) from exc

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
            if self._groups is not None:
                await self._groups.close()
            if self._routes is not None:
                await self._routes.close()
            if self._masque is not None:
                await self._masque.close()
            if self._runtime is not None:
                await self._runtime.close()
            if self._tun is not None:
                await self._tun.close()
            if self._media_offload_adapter is not None:
                await self._media_offload_adapter.close()
            self._offloading_sessions.clear()
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
