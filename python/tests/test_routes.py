from __future__ import annotations

import inspect

from agent_sdk import AgentSdk, SdkInitResult
from agent_sdk.routes import GroupRouteManager, MemoryRouteBackend


def test_public_api_does_not_expose_route_configuration():
    assert "peer_routes" not in inspect.signature(AgentSdk.init).parameters
    assert "agent_tun_cidr" not in inspect.signature(AgentSdk.init).parameters
    assert "installed_routes" not in SdkInitResult.__dataclass_fields__
    assert "registration_id" not in SdkInitResult.__dataclass_fields__


async def test_init_uses_runtime_ue_assignment_for_tun(sdk_fixture):
    assert sdk_fixture["result"].agent_tun_cidr == "8.8.8.7/24"
    assert sdk_fixture["result"].agent_tcp_endpoint == "8.8.8.7:4001"
    assert sdk_fixture["tun"].cidr == "8.8.8.7/24"


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
