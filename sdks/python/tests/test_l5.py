"""Optional real-QUIC L5 integration; base SDK tests do not require moq-py."""

import asyncio
import socket
from datetime import datetime, timezone

import pytest

pytest.importorskip("moq.acn")
from moq.acn.relay import AcnRelay
from agent_sdk.l5 import L5Config
from agent_sdk.l5.manager import L5Manager
from agent_sdk.models import GroupConfigSnapshot
from agent_sdk.routes import GroupRouteManager, MemoryRouteBackend


async def until(predicate, timeout=3):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("L5 condition timed out")
        await asyncio.sleep(.01)


async def test_sdk_catalog_business_and_route_isolation():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    relay = AcnRelay("127.0.0.1", port)
    await relay.start()
    owner = object()
    relay.open_management(owner)
    relay.apply(owner, "g", 1, "PREPARED", ["A", "B"])
    relay.apply(owner, "g", 1, "ACTIVE", ["A", "B"])
    snapshot = GroupConfigSnapshot.immutable(group_id="g", version="1.0.0", notification_timestamp=datetime.now(timezone.utc),
        members={"A": None, "B": None}, group_version=1)
    routes = [GroupRouteManager(MemoryRouteBackend()) for _ in range(2)]
    events = []
    managers = [L5Manager(L5Config("127.0.0.1", port), agent, "127.0.0.1", route,
                listener=events.append) for agent, route in zip(("A", "B"), routes)]
    try:
        for manager in managers:
            manager.configure(snapshot)
        await until(lambda: managers[1].catalogs.get(("g", "A")) is not None)
        await managers[0].register_track("g", "status")
        await until(lambda: bool(managers[1].catalogs[("g", "A")]["tracks"]))
        await managers[1].subscribe("g", "A", "status")
        await managers[0].publish("g", "status", {"state": "ready"})
        await until(lambda: any(event["event"] == "message" for event in events))
        assert next(event for event in events if event["event"] == "message")["payload"] == {"state": "ready"}
        assert len(relay.bindings) == 4
        # A's Publisher reconnect resets its catalogs only. B's Subscriber
        # connection stays alive, retires the old directory request and resubscribes.
        old_publisher = managers[0].clients["publisher"]
        old_subscriber = managers[1].clients["subscriber"]
        old_publisher.protocol.close(error_code=0, reason_phrase="test Publisher reconnect")
        await until(lambda: managers[0].clients.get("publisher") is not None and managers[0].clients["publisher"] is not old_publisher)
        await until(lambda: managers[1].catalogs.get(("g", "A")) is not None)
        assert managers[1].clients["subscriber"] is old_subscriber
        for phase in ("PREPARED", "ACTIVE"):
            relay.apply(owner, "g2", 1, phase, ["A", "B"])
        second_group = GroupConfigSnapshot.immutable(group_id="g2", version="1.0.0", notification_timestamp=datetime.now(timezone.utc),
            members={"A": None, "B": None}, group_version=1)
        for manager in managers:
            manager.configure(second_group)
        await until(lambda: managers[1].catalogs.get(("g2", "A")) is not None)
        assert len(relay.bindings) == 4  # Each role serves both groups.
        relay.delete(owner, "g", 1)
        for manager in managers:
            manager.invalidate("g")
        assert managers[1].clients["subscriber"] is old_subscriber
        assert managers[1].catalogs.get(("g2", "A")) is not None
        await routes[0].replace_group_peers("other-business", {"127.0.0.1"})
        await managers[0].close()
        assert "127.0.0.1/32" in routes[0].allowed_host_routes
    finally:
        for manager in managers:
            await manager.close()
        await relay.stop()


async def test_expired_work_requires_explicit_retry_and_configuration_does_not_rearm():
    routes = GroupRouteManager(MemoryRouteBackend())
    manager = L5Manager(L5Config("127.0.0.1", 4433, recovery_timeout=.08, local_timeout=.02), "A", "127.0.0.1", routes)
    snapshot = GroupConfigSnapshot.immutable(group_id="g", version="1.0.0", notification_timestamp=datetime.now(timezone.utc),
        members={"A": None}, group_version=1)
    manager.configure(snapshot)
    key = ("g", "publisher", "A", "group_catalog")
    try:
        await until(lambda: key in manager.blocked)
        old = manager.tasks[key]
        manager.configure(snapshot)
        assert manager.tasks[key] is old and key in manager.blocked
        manager.retry("g", "publisher")
        assert manager.tasks[key] is not old
    finally:
        await manager.close()
