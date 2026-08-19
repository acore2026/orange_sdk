from __future__ import annotations

from agent_sdk.routes import GroupRouteManager, MemoryRouteBackend


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

