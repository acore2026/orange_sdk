from __future__ import annotations

import asyncio
import ipaddress
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .config import SdkConfig
from .contracts import (
    ConnectIpTransport,
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
    RejectUnconfiguredMessageSignatureVerifier,
    RejectUnconfiguredMessageSigner,
    RejectUnconfiguredProofVerifier,
)
from .tun import LinuxTunDevice, validate_ip_packet

TunFactory = Callable[[str, str, int], Awaitable[TunDevice]]
MasqueFactory = Callable[[SdkConfig], ConnectIpTransport]
RuntimeFactory = Callable[[SdkConfig], RuntimeTransport]
ServerFactory = Callable[[], LocalServer]
RouteBackendFactory = Callable[[SdkConfig, TunDevice], RouteBackend]

_LOGGER = logging.getLogger(__name__)


class AgentSdk:
    def __init__(
        self,
        *,
        proof_verifier: ProofVerifier | None = None,
        message_signer: MessageSigner | None = None,
        message_signature_verifier: MessageSignatureVerifier | None = None,
        peer_messenger: PeerMessenger | None = None,
        tun_factory: TunFactory | None = None,
        masque_factory: MasqueFactory | None = None,
        runtime_factory: RuntimeFactory | None = None,
        server_factory: ServerFactory | None = None,
        route_backend_factory: RouteBackendFactory | None = None,
        media_offload_adapter: MediaOffloadAdapter | None = None,
    ) -> None:
        self._proof_verifier = proof_verifier or RejectUnconfiguredProofVerifier()
        self._message_signer = message_signer or RejectUnconfiguredMessageSigner()
        self._message_signature_verifier = (
            message_signature_verifier or RejectUnconfiguredMessageSignatureVerifier()
        )
        self._peer_messenger = peer_messenger or HttpPeerMessenger()
        self._tun_factory = tun_factory or LinuxTunDevice.create
        self._masque_factory = masque_factory or (
            lambda config: AioquicConnectIpTransport(
                server_url=config.masque_server_url,
                server_name=config.masque_server_name,
                ca_certificate_pem=config.masque_ca_certificate_pem,
                authorization=config.masque_authorization,
                local_address=config.local_vlan_ip,
            )
        )
        self._runtime_factory = runtime_factory or (
            lambda config: HttpRuntimeTransport(
                config.agent_runtime_ip, config.agent_runtime_port
            )
        )
        self._server_factory = server_factory or AiohttpLocalServer
        self._route_backend_factory = route_backend_factory or (
            lambda config, tun: Pyroute2RouteBackend(
                tun.name, config.agent_tun_ip
            )
        )
        self._media_offload_adapter = media_offload_adapter

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
        self._group_info: dict[str, GroupInfo] = {}
        self._offloading_sessions: dict[str, OffloadingSession] = {}

    @property
    def state(self) -> str:
        return self._state

    async def init(
        self,
        agent_runtime_ip: str,
        agent_runtime_port: int,
        local_vlan_ip: str,
        local_tcp_port: int,
        local_udp_port: int,
        *,
        agent_tun_cidr: str,
        masque_server_url: str,
        peer_routes: Sequence[str] = (),
        masque_server_name: str | None = None,
        masque_ca_certificate_pem: bytes | None = None,
        masque_authorization: str | None = None,
        tun_name: str = "agent_tun0",
        tun_mtu: int = 1280,
    ) -> SdkInitResult:
        if self._state not in {"NEW", "CLOSED"}:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT, f"cannot init SDK in state {self._state}"
            )
        config = SdkConfig.validate(
            agent_runtime_ip=agent_runtime_ip,
            agent_runtime_port=agent_runtime_port,
            local_vlan_ip=local_vlan_ip,
            local_tcp_port=local_tcp_port,
            local_udp_port=local_udp_port,
            agent_tun_cidr=agent_tun_cidr,
            masque_server_url=masque_server_url,
            peer_routes=tuple(peer_routes),
            masque_server_name=masque_server_name,
            masque_ca_certificate_pem=masque_ca_certificate_pem,
            masque_authorization=masque_authorization,
            tun_name=tun_name,
            tun_mtu=tun_mtu,
        )
        self._state = "INITIALIZING"
        self._config = config
        try:
            self._tun = await self._tun_factory(
                config.tun_name, config.agent_tun_cidr, config.tun_mtu
            )
            backend = self._route_backend_factory(config, self._tun)
            self._routes = GroupRouteManager(backend, config.peer_routes)
            self._groups = GroupMemberCache(self._routes)

            self._server = self._server_factory()
            await self._server.start(
                physical_ip=config.local_vlan_ip,
                agent_ip=config.agent_tun_ip,
                tcp_port=config.local_tcp_port,
                udp_port=config.local_udp_port,
                on_group_config=self._handle_group_config,
                on_group_invitation=self._handle_group_invitation,
                on_a2a_message=self._handle_a2a_message,
            )

            self._runtime = self._runtime_factory(config)
            await self._runtime.connect()
            registration_id = await self._runtime.register_endpoint(
                config.local_vlan_ip,
                config.local_tcp_port,
                config.local_udp_port,
            )

            self._masque = self._masque_factory(config)
            await self._masque.start(self._write_downlink_packet)
            await self._routes.install_static()
            self._pump_task = asyncio.create_task(
                self._pump_uplink(), name="agent-tun-uplink"
            )
            self._state = "READY"
            return SdkInitResult(
                runtime_connected=True,
                masque_connected=self._masque.connected,
                registration_id=registration_id,
                local_tcp_endpoint=f"{config.local_vlan_ip}:{config.local_tcp_port}",
                local_udp_endpoint=f"{config.local_vlan_ip}:{config.local_udp_port}",
                agent_tcp_endpoint=f"{config.agent_tun_ip}:{config.local_tcp_port}",
                agent_udp_endpoint=f"{config.agent_tun_ip}:{config.local_udp_port}",
                agent_tun_cidr=config.agent_tun_cidr,
                installed_routes=config.peer_routes,
                masque_proxy_endpoint=config.masque_server_url,
            )
        except Exception:
            await self.close()
            raise

    def _require_ready(self) -> None:
        if self._state != "READY":
            raise AgentSdkError(
                ErrorCode.SDK_NOT_INITIALIZED, "SDK is not initialized"
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
                _LOGGER.exception(
                    "group configuration notification listener failed for group %s",
                    candidate.group_id,
                )
        return NetworkMessageAction.ACK

    async def _handle_a2a_message(self, payload: Mapping[str, Any]) -> None:
        self._require_ready()
        if self._profile is None or self._group_listener is None:
            raise AgentSdkError(
                ErrorCode.GROUP_NOT_ACTIVE, "A2A listener or local identity is missing"
            )
        group_id = str(payload.get("group_id", ""))
        sender_id = str(payload.get("sender_agent_id", ""))
        target_id = str(payload.get("target_agent_id", ""))
        if target_id != self._profile.agent_id:
            raise AgentSdkError(
                ErrorCode.TARGET_NOT_IN_GROUP, "A2A message targets another agent"
            )
        assert self._groups is not None
        sender = await self._groups.resolve(group_id, sender_id)
        await self._message_signature_verifier.verify_a2a(payload, sender.did_key)
        user_payload = payload.get("payload")
        if not isinstance(user_payload, Mapping):
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT, "A2A payload must be a JSON object"
            )
        await self._group_listener.on_group_message(group_id, sender_id, user_payload)

    async def send_message(
        self,
        group_id: str,
        target_agent_id: str,
        json_message: Mapping[str, Any],
        timeout_seconds: float = 5.0,
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
        assert self._groups is not None
        target = await self._groups.resolve(group_id, target_agent_id)
        message_id = str(uuid.uuid4())
        body: dict[str, Any] = {
            "message_id": message_id,
            "group_id": group_id,
            "sender_agent_id": self._profile.agent_id,
            "target_agent_id": target_agent_id,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "payload": dict(json_message),
        }
        body["proof"] = dict(await self._message_signer.sign_a2a(body))
        response = await self._peer_messenger.send(
            target.agent_ip, target.tcp_port, body, timeout_seconds
        )
        delivered = response.get("ack") is True
        return MessageReceipt(
            message_id=message_id,
            delivered=delivered,
            delivered_at=datetime.now(timezone.utc) if delivered else None,
        )

    async def apply_identity(
        self,
        owner: str,
        name: str,
        public_key: str,
        description: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> AgentProfile:
        self._require_ready()
        assert self._runtime is not None
        response = await self._runtime.request(
            "POST",
            "/idm/v1/identity-applications",
            {
                "owner": owner,
                "name": name,
                "public_key": public_key,
                "description": description,
                "metadata": dict(metadata or {}),
            },
        )
        profile = AgentProfile(
            agent_id=str(response["agent_id"]),
            agent_name=str(response.get("agent_name", name)),
            identity_vc=dict(response.get("identity_vc", {})),
        )
        self._profile = profile
        return profile

    def set_local_profile_for_restore(self, profile: AgentProfile) -> None:
        """Restore a previously verified profile from secure local storage."""
        self._profile = profile

    async def deregister_identity(self, agent_id: str, reason: str = "") -> OperationResult:
        self._require_ready()
        assert self._runtime is not None
        response = await self._runtime.request(
            "POST",
            "/acn-agent/v1/agent-deletions",
            {"agent_id": agent_id, "reason": reason},
        )
        if self._profile and self._profile.agent_id == agent_id:
            self._profile = None
        return OperationResult(
            bool(response.get("success", True)),
            str(response.get("operation_id", "")),
            str(response.get("message", "")),
        )

    async def get_network_ability(
        self, agent_id: str, intent: str = ""
    ) -> NetworkAbility:
        self._require_ready()
        assert self._runtime is not None
        response = await self._runtime.request(
            "POST", "/idm/v1/network-ability", {"agent_id": agent_id, "intent": intent}
        )
        return NetworkAbility(
            ability_vc=dict(response.get("ability_vc", {})),
            abilities=tuple(response.get("abilities", ())),
            valid_until=None,
        )

    async def register_capabilities(
        self, agent_id: str, priority: int, credentials: Sequence[Mapping[str, Any]]
    ) -> OperationResult:
        return await self._operation(
            "POST",
            "/arf/v1/agent-cards",
            {"agent_id": agent_id, "priority": priority, "vc_list": list(credentials)},
        )

    async def update_capabilities(
        self,
        agent_id: str,
        update_items: Sequence[Mapping[str, Any]],
        credentials: Sequence[Mapping[str, Any]],
    ) -> OperationResult:
        return await self._operation(
            "POST",
            "/arf/v1/agent-cards-update",
            {
                "agent_id": agent_id,
                "update_items": list(update_items),
                "credentials": list(credentials),
            },
        )

    async def discover_agents(
        self,
        task_id: str,
        agent_id: str,
        task_description: str,
        required_skills: Sequence[str],
        discovery_scope: str = "intra_plmn",
        max_results: int = 10,
    ) -> list[DiscoveredAgent]:
        self._require_ready()
        assert self._runtime is not None
        response = await self._runtime.request(
            "POST",
            "/arf/v1/agent-discoveries",
            {
                "task_id": task_id,
                "agent_id": agent_id,
                "task_description": task_description,
                "required_skills": list(required_skills),
                "discovery_scope": discovery_scope,
                "max_results": max_results,
            },
        )
        agents = [
            DiscoveredAgent(
                agent_id=str(item["agent_id"]),
                ip=str(item.get("ip", "")),
                tcp_port=int(item.get("tcp_port", 0)),
                udp_port=int(item.get("udp_port", 0)),
                skills=tuple(item.get("skills", ())),
                priority=int(item.get("priority", 0)),
            )
            for item in response.get("agents", ())
        ]
        return sorted(agents, key=lambda item: item.priority)

    async def create_group(
        self,
        agent_id: str,
        target_agent_ids: Sequence[str],
        group_name: str,
        scope: str = "private",
        max_members: int = 10,
    ) -> GroupInfo:
        self._require_ready()
        assert self._runtime is not None
        response = await self._runtime.request(
            "POST",
            "/acf/v1/agents-grouping",
            {
                "agent_id": agent_id,
                "target_agent_ids": list(target_agent_ids),
                "group_name": group_name,
                "scope": scope,
                "max_members": max_members,
            },
        )
        info = GroupInfo(str(response["group_id"]), group_name)
        self._group_info[info.group_id] = info
        return info

    async def create_offloading_session(
        self,
        agent_id: str,
        task_type: str,
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
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "task_type": task_type,
        }
        if sandbox_id is not None:
            body["preferred_sandbox_id"] = sandbox_id
        response = await self._runtime.request(
            "POST",
            "/compute/v1/offloading-sessions",
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

    async def get_group_snapshot(self, group_id: str) -> GroupConfigSnapshot | None:
        if self._groups is None:
            return None
        return await self._groups.snapshot(group_id)

    async def close(self) -> None:
        if self._state in {"CLOSING", "CLOSED"}:
            return
        self._state = "CLOSING"
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
        if self._server is not None:
            await self._server.close()
        if self._runtime is not None:
            await self._runtime.close()
        if self._tun is not None:
            await self._tun.close()
        if self._media_offload_adapter is not None:
            await self._media_offload_adapter.close()
        self._offloading_sessions.clear()
        self._state = "CLOSED"
