from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address, ip_interface
from urllib.parse import urlparse

from .errors import AgentSdkError, ErrorCode


@dataclass(frozen=True, slots=True)
class SdkConfig:
    agent_runtime_ip: str
    agent_runtime_port: int
    local_vlan_ip: str
    local_tcp_port: int
    local_udp_port: int
    agent_tun_cidr: str
    masque_server_url: str
    masque_server_name: str | None
    masque_ca_certificate_pem: bytes | None
    masque_authorization: str | None
    tun_name: str
    tun_mtu: int
    log_file_path: str
    log_level: str
    log_max_bytes: int
    log_backup_count: int

    @property
    def agent_tun_ip(self) -> str:
        return str(ip_interface(self.agent_tun_cidr).ip)

    @property
    def masque_host(self) -> str:
        parsed = urlparse(self.masque_server_url)
        assert parsed.hostname is not None
        return parsed.hostname

    @property
    def masque_port(self) -> int:
        parsed = urlparse(self.masque_server_url)
        return parsed.port or 443

    @classmethod
    def validate_client_parameters(
        cls,
        *,
        agent_runtime_ip: str,
        agent_runtime_port: int,
        local_vlan_ip: str,
        local_tcp_port: int,
        local_udp_port: int,
        masque_server_url: str,
        masque_server_name: str | None,
        masque_ca_certificate_pem: bytes | None,
        masque_authorization: str | None,
        tun_name: str,
        tun_mtu: int,
        log_file_path: str,
        log_level: str,
        log_max_bytes: int,
        log_backup_count: int,
    ) -> None:
        for field_name, value in (
            ("agent_runtime_ip", agent_runtime_ip),
            ("local_vlan_ip", local_vlan_ip),
        ):
            try:
                ip_address(value)
            except ValueError as exc:
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT, str(exc), field=field_name
                ) from exc

        parsed = urlparse(masque_server_url)
        if parsed.scheme != "https" or parsed.hostname is None:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "masque_server_url must be an https URL",
                field="masque_server_url",
            )

        for name, port in (
            ("agent_runtime_port", agent_runtime_port),
            ("local_tcp_port", local_tcp_port),
            ("local_udp_port", local_udp_port),
        ):
            if not 1 <= port <= 65535:
                raise AgentSdkError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"{name} must be in 1..65535",
                    field=name,
                )
        if not 576 <= tun_mtu <= 65535:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "tun_mtu must be in 576..65535",
                field="tun_mtu",
            )

        if not tun_name or len(tun_name.encode()) >= 16:
            raise AgentSdkError(
                ErrorCode.INVALID_ARGUMENT,
                "tun_name must be non-empty and shorter than IFNAMSIZ",
                field="tun_name",
            )

    @classmethod
    def validate(
        cls,
        *,
        agent_runtime_ip: str,
        agent_runtime_port: int,
        local_vlan_ip: str,
        local_tcp_port: int,
        local_udp_port: int,
        agent_tun_cidr: str,
        masque_server_url: str,
        masque_server_name: str | None,
        masque_ca_certificate_pem: bytes | None,
        masque_authorization: str | None,
        tun_name: str,
        tun_mtu: int,
        log_file_path: str,
        log_level: str,
        log_max_bytes: int,
        log_backup_count: int,
    ) -> "SdkConfig":
        cls.validate_client_parameters(
            agent_runtime_ip=agent_runtime_ip,
            agent_runtime_port=agent_runtime_port,
            local_vlan_ip=local_vlan_ip,
            local_tcp_port=local_tcp_port,
            local_udp_port=local_udp_port,
            masque_server_url=masque_server_url,
            masque_server_name=masque_server_name,
            masque_ca_certificate_pem=masque_ca_certificate_pem,
            masque_authorization=masque_authorization,
            tun_name=tun_name,
            tun_mtu=tun_mtu,
            log_file_path=log_file_path,
            log_level=log_level,
            log_max_bytes=log_max_bytes,
            log_backup_count=log_backup_count,
        )
        try:
            normalized_tun_cidr = str(ip_interface(agent_tun_cidr))
        except ValueError as exc:
            raise AgentSdkError(
                ErrorCode.RUNTIME_REJECTED,
                "AgentRuntime returned an invalid UE address assignment",
                field="ue_ip",
            ) from exc

        return cls(
            agent_runtime_ip=agent_runtime_ip,
            agent_runtime_port=agent_runtime_port,
            local_vlan_ip=local_vlan_ip,
            local_tcp_port=local_tcp_port,
            local_udp_port=local_udp_port,
            agent_tun_cidr=normalized_tun_cidr,
            masque_server_url=masque_server_url,
            masque_server_name=masque_server_name,
            masque_ca_certificate_pem=masque_ca_certificate_pem,
            masque_authorization=masque_authorization,
            tun_name=tun_name,
            tun_mtu=tun_mtu,
            log_file_path=log_file_path,
            log_level=log_level,
            log_max_bytes=log_max_bytes,
            log_backup_count=log_backup_count,
        )
