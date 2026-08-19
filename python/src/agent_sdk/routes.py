from __future__ import annotations

import asyncio
from collections import Counter
from ipaddress import ip_address
from typing import Protocol

from .errors import AgentSdkError, ErrorCode


class RouteBackend(Protocol):
    async def add(self, cidr: str) -> None: ...

    async def remove(self, cidr: str) -> None: ...


class GroupRouteManager:
    def __init__(self, backend: RouteBackend, static_routes: tuple[str, ...] = ()) -> None:
        self._backend = backend
        self._static_routes = set(static_routes)
        self._group_peers: dict[str, set[str]] = {}
        self._refs: Counter[str] = Counter()
        self._lock = asyncio.Lock()

    @staticmethod
    def _host_route(ip: str) -> str:
        parsed = ip_address(ip)
        return f"{parsed}/{32 if parsed.version == 4 else 128}"

    async def install_static(self) -> None:
        installed: list[str] = []
        try:
            for route in sorted(self._static_routes):
                await self._backend.add(route)
                installed.append(route)
        except Exception as exc:
            for route in reversed(installed):
                try:
                    await self._backend.remove(route)
                except Exception:
                    pass
            raise AgentSdkError(
                ErrorCode.ROUTE_CONFIG_FAILED,
                f"failed to install static routes: {exc}",
            ) from exc

    async def replace_group_peers(self, group_id: str, peer_ips: set[str]) -> None:
        new_routes = {self._host_route(ip) for ip in peer_ips}
        async with self._lock:
            old_routes = self._group_peers.get(group_id, set())
            to_add_refs = new_routes - old_routes
            to_drop_refs = old_routes - new_routes
            kernel_add = {
                route
                for route in to_add_refs
                if self._refs[route] == 0 and route not in self._static_routes
            }
            kernel_remove = {
                route
                for route in to_drop_refs
                if self._refs[route] == 1 and route not in self._static_routes
            }
            added: list[str] = []
            removed: list[str] = []
            try:
                for route in sorted(kernel_add):
                    await self._backend.add(route)
                    added.append(route)
                for route in sorted(kernel_remove):
                    await self._backend.remove(route)
                    removed.append(route)
            except Exception as exc:
                for route in reversed(removed):
                    try:
                        await self._backend.add(route)
                    except Exception:
                        pass
                for route in reversed(added):
                    try:
                        await self._backend.remove(route)
                    except Exception:
                        pass
                raise AgentSdkError(
                    ErrorCode.ROUTE_CONFIG_FAILED,
                    f"failed to reconcile group routes: {exc}",
                ) from exc

            for route in to_add_refs:
                self._refs[route] += 1
            for route in to_drop_refs:
                self._refs[route] -= 1
                if self._refs[route] <= 0:
                    del self._refs[route]
            if new_routes:
                self._group_peers[group_id] = new_routes
            else:
                self._group_peers.pop(group_id, None)

    async def close(self) -> None:
        async with self._lock:
            routes = set(self._refs) | self._static_routes
            for route in sorted(routes):
                try:
                    await self._backend.remove(route)
                except Exception:
                    pass
            self._group_peers.clear()
            self._refs.clear()

    @property
    def allowed_host_routes(self) -> frozenset[str]:
        return frozenset(self._refs) | frozenset(self._static_routes)


class MemoryRouteBackend:
    def __init__(self) -> None:
        self.routes: set[str] = set()
        self.operations: list[tuple[str, str]] = []

    async def add(self, cidr: str) -> None:
        self.routes.add(cidr)
        self.operations.append(("add", cidr))

    async def remove(self, cidr: str) -> None:
        self.routes.discard(cidr)
        self.operations.append(("remove", cidr))


class Pyroute2RouteBackend:
    def __init__(self, interface_name: str, source_ip: str) -> None:
        self._interface_name = interface_name
        self._source_ip = source_ip

    async def _run(self, operation: str, cidr: str) -> None:
        def apply() -> None:
            from pyroute2 import IPRoute

            with IPRoute() as ipr:
                indices = ipr.link_lookup(ifname=self._interface_name)
                if not indices:
                    raise RuntimeError(f"interface {self._interface_name} not found")
                kwargs = {"dst": cidr, "oif": indices[0]}
                if ip_address(self._source_ip).version == 4:
                    kwargs["prefsrc"] = self._source_ip
                if operation == "add":
                    ipr.route("replace", **kwargs)
                else:
                    try:
                        ipr.route("del", **kwargs)
                    except Exception:
                        return

        await asyncio.to_thread(apply)

    async def add(self, cidr: str) -> None:
        await self._run("add", cidr)

    async def remove(self, cidr: str) -> None:
        await self._run("remove", cidr)
