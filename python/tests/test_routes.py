from __future__ import annotations

import inspect

import pytest

from agent_sdk import AgentSdk, SdkInitResult
from agent_sdk.config import SdkConfig
from agent_sdk.errors import AgentSdkError, ErrorCode
from agent_sdk.routes import GroupRouteManager, MemoryRouteBackend


def test_public_api_does_not_expose_route_configuration():
    assert "peer_routes" not in inspect.signature(AgentSdk.init).parameters
    assert "agent_tun_cidr" in inspect.signature(AgentSdk.init).parameters
    assert "installed_routes" not in SdkInitResult.__dataclass_fields__
    assert "registration_id" not in SdkInitResult.__dataclass_fields__


async def test_init_uses_local_agent_tun_configuration(sdk_fixture):
    assert sdk_fixture["result"].agent_tun_cidr == "8.8.8.7/24"
    assert sdk_fixture["result"].agent_tcp_endpoint == "8.8.8.7:4001"
    assert sdk_fixture["tun"].cidr == "8.8.8.7/24"
    assert sdk_fixture["runtime"].requests == []
    assert sdk_fixture["runtime"].downlink_handler is not None


def test_config_rejects_invalid_local_agent_tun_cidr():
    with pytest.raises(AgentSdkError) as raised:
        SdkConfig.validate(
            agent_runtime_ip="192.168.3.10",
            agent_runtime_port=8080,
            local_vlan_ip="192.168.1.10",
            local_tcp_port=4001,
            local_udp_port=28443,
            agent_tun_cidr="8.8.8.7/33",
            masque_server_url="https://192.168.3.10:4433",
            masque_authorization=None,
            tun_name="agent_tun0",
            tun_mtu=1280,
            log_file_path="logs/test.log",
            log_level="INFO",
            log_max_bytes=1024,
            log_backup_count=1,
        )

    assert raised.value.code == ErrorCode.INVALID_ARGUMENT
    assert raised.value.field == "agent_tun_cidr"


async def test_route_reference_count_across_groups():
    backend = MemoryRouteBackend()
    manager = GroupRouteManager(backend)

    await manager.replace_group_peers("g1", {"8.8.8.8"})
    await manager.replace_group_peers("g2", {"8.8.8.8"})
    await manager.replace_group_peers("g1", set())

    assert backend.routes == {"8.8.8.8/32"}
    assert backend.operations.count(("add", "8.8.8.8/32")) == 1

    await manager.replace_group_peers("g2", set())
    assert backend.routes == set()
    assert backend.operations.count(("remove", "8.8.8.8/32")) == 1
