from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from agent_sdk.group_cache import GroupMemberCache
from agent_sdk.routes import GroupRouteManager, MemoryRouteBackend


async def main() -> None:
    backend = MemoryRouteBackend()
    cache = GroupMemberCache(GroupRouteManager(backend))
    payload = {
        "notification_type": "acf_group_config",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "group_id": "g1",
        "members": {
            "agent1": {
                "agent_id": "did:example:a",
                "agent_name": "A",
                "capabilities": ["text"],
                "agent_ip": "8.8.8.7",
                "tcp_port": "4001",
                "udp_port": "28443",
                "did_key": "did:key:a",
            },
            "agent2": {
                "agent_id": "did:example:b",
                "agent_name": "B",
                "capabilities": ["text"],
                "agent_ip": "8.8.8.8",
                "tcp_port": "4001",
                "udp_port": "28443",
                "did_key": "did:key:b",
            },
        },
        "proof": {"jws": "demo"},
    }
    candidate = cache.build_candidate(
        payload,
        local_agent_id="did:example:a",
        local_agent_ip="8.8.8.7",
        local_tcp_port=4001,
        local_udp_port=28443,
    )
    await cache.commit(candidate, local_agent_id="did:example:a")
    target = await cache.resolve("g1", "did:example:b")
    print("TCP destination from cache:", target.agent_ip, target.tcp_port)
    print("Dynamic routes:", sorted(backend.routes))


if __name__ == "__main__":
    asyncio.run(main())

