"""Group-scoped L5 work on two Agent-scoped MOQT role connections."""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from typing import Any

from moq.acn.client import AcnClient, RequestFailed
from moq.acn.contracts import Limits, SESSION_REPLACED, full_track, catalog, encode_json

from . import L5Config


class L5Manager:
    def __init__(self, config: L5Config, agent_id: str, source_ip: str, routes, logger=None, listener=None):
        self.config, self.agent_id, self.source_ip, self.routes = config, agent_id, source_ip, routes
        self.logger = logger or logging.getLogger(__name__)
        self.listener = listener
        self.limits = Limits(local_timeout=config.local_timeout, recovery_timeout=config.recovery_timeout)
        self.groups = {}
        self.clients = {}
        self.connecting = {}
        self.role_blocked = set()
        self.tasks = {}
        self.blocked = set()
        self.requests = {}
        self.tracks = {}
        self.wanted = set()
        self.catalogs = {}
        self.catalog_waiters = {}
        self.catalog_revisions = {}
        self.publish_revisions = {}
        self.pending_catalogs = {}
        self.catalog_changes = {}
        self.catalog_locks = {}
        self.last_catalog = {}
        self.statuses = {}
        self.closed = False
        self.background = set()
        self.route_key = f"\x00acn-l5-relay:{id(self)}"

    def _emit(self, event, **details):
        payload = {"event": event, **details}
        self.logger.info("L5 %s", payload)
        if self.listener:
            try:
                value = self.listener(payload)
                if inspect.isawaitable(value):
                    task = asyncio.create_task(value)
                    task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            except Exception:
                self.logger.exception("L5 application listener failed")

    def configure(self, snapshot):
        current = self.groups.get(snapshot.group_id)
        if current and current.group_version == snapshot.group_version:
            return
        self.invalidate(snapshot.group_id, release=False)
        self.groups[snapshot.group_id] = snapshot
        self.wanted = {key for key in self.wanted if key[0] != snapshot.group_id or key[2] in snapshot.members_by_agent_id}
        self._start_group(snapshot.group_id)

    def _start_group(self, group):
        self._spawn((group, "publisher", self.agent_id, "group_catalog"), self._prepare_publisher)
        for member in self.groups[group].members_by_agent_id:
            if member != self.agent_id:
                self._spawn((group, "subscriber", member, "group_catalog"), self._prepare_directory)
        for key in self.wanted:
            if key[0] == group:
                self._spawn(key, self._subscribe)

    def _valid(self, group, version):
        return not self.closed and group in self.groups and self.groups[group].group_version == version

    def _background(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return task

    def _spawn(self, key, operation, explicit=False):
        if self.closed or key[0] not in self.groups:
            return None
        if explicit:
            self.blocked.discard(key)
            self.role_blocked.discard(key[1])
        if key in self.blocked:
            return None
        existing = self.tasks.get(key)
        if existing and not existing.done():
            return existing
        version = self.groups[key[0]].group_version
        self.statuses[key] = "PREPARING"

        async def run():
            deadline = time.monotonic() + self.config.recovery_timeout
            try:
                while self._valid(key[0], version):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("L5 preparation/recovery deadline exceeded")
                    try:
                        result = await asyncio.wait_for(operation(key, version, deadline), remaining)
                        if self._valid(key[0], version):
                            self.statuses[key] = "READY"
                            self._emit("ready", group_id=key[0], group_version=version, direction=key[1], member_agent_id=key[2], track=key[3])
                        return result
                    except RequestFailed as exc:
                        if not exc.retry_interval:
                            raise
                        delay = max(.1, (exc.retry_interval - 1) / 1000)
                    except (OSError, ConnectionError):
                        delay = .5
                    await asyncio.sleep(min(delay + random.uniform(0, .1), max(0, deadline - time.monotonic())))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._valid(key[0], version):
                    self.blocked.add(key)
                    self.statuses[key] = "UNAVAILABLE"
                    current = self.requests.pop(key, None)
                    if current:
                        await current[0].cancel(current[1])
                    if key[1] == "subscriber" and key[3] == "group_catalog":
                        self.catalogs.pop((key[0], key[2]), None)
                    self._emit("unavailable", group_id=key[0], group_version=version, direction=key[1], member_agent_id=key[2], track=key[3], reason=str(exc), explicit_retry_required=True)
                return None
        task = asyncio.create_task(run())
        self.tasks[key] = task
        return task

    async def _role(self, role, deadline):
        current = self.clients.get(role)
        if current and current.bound:
            return current
        if role in self.role_blocked:
            raise RequestFailed("ROLE_UNAVAILABLE", "role requires application explicit retry")
        pending = self.connecting.get(role)
        if pending is None or pending.done():
            async def connect():
                end = time.monotonic() + self.config.recovery_timeout
                try:
                    await self.routes.replace_group_peers(self.route_key, {self.config.relay_ipv4})
                    while time.monotonic() < end and not self.closed:
                        client = AcnClient(self.config.relay_ipv4, self.config.relay_port, self.agent_id, role,
                            source_ip=self.source_ip, limits=self.limits)
                        client.on_object = lambda request, value, bound=client: self._object(role, bound, request, value)
                        client.on_failure = lambda request, error, bound=client: self._request_failed(role, bound, request, error)
                        client.on_close = lambda code, bound=client: self._disconnected(role, bound, code)
                        try:
                            await client.connect(min(self.config.local_timeout, end - time.monotonic()))
                            if self.closed:
                                await client.close()
                                raise ConnectionError("L5 manager closed")
                            self.clients[role] = client
                            if role == "publisher":
                                self.publish_revisions.clear()
                            return client
                        except RequestFailed as exc:
                            await client.close()
                            if not exc.retry_interval:
                                raise
                        except (OSError, ConnectionError, TimeoutError):
                            await client.close()
                        await asyncio.sleep(min(.5, max(0, end - time.monotonic())))
                    raise TimeoutError("L5 role connection deadline exceeded")
                except BaseException:
                    self.role_blocked.add(role)
                    raise
            pending = self.connecting[role] = asyncio.create_task(connect())
            pending.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return await asyncio.wait_for(asyncio.shield(pending), max(.001, deadline - time.monotonic()))

    async def _request(self, key, version, deadline):
        client = await self._role(key[1], deadline)
        existing = self.requests.get(key)
        if existing and existing[0] is client and existing[1].identifier in client.requests:
            return client, existing[1]
        try:
            request = await client.request(full_track(key[0], version, key[2], key[3]), max(.001, deadline - time.monotonic()),
                on_created=lambda pending: self.requests.__setitem__(key, (client, pending)))
        except BaseException:
            current = self.requests.get(key)
            if current and current[0] is client:
                self.requests.pop(key, None)
            raise
        if not self._valid(key[0], version):
            await client.cancel(request)
            raise asyncio.CancelledError()
        self.requests[key] = client, request
        return client, request

    async def _prepare_publisher(self, key, version, deadline):
        for name in self.tracks.get(key[0], {}):
            business = (key[0], "publisher", self.agent_id, name)
            await self._request(business, version, deadline)
            self.statuses[business] = "READY"
        await self._request(key, version, deadline)
        await self._publish_catalog(key[0], version, deadline)

    async def _publish_catalog(self, group, version, deadline):
        async with self.catalog_locks.setdefault(group, asyncio.Lock()):
            await self._publish_catalog_locked(group, version, deadline)

    async def _publish_catalog_locked(self, group, version, deadline):
        client, request = await self._request((group, "publisher", self.agent_id, "group_catalog"), version, deadline)
        # Coalesce only local, not-yet-submitted changes before allocating revision.
        delay = max(0, self.last_catalog.get(group, 0) + .5 - time.monotonic())
        if delay:
            await asyncio.sleep(delay)
        if not self._valid(group, version):
            return
        revision = self.publish_revisions.get(group, 0) + 1
        self.publish_revisions[group] = revision
        self.last_catalog[group] = time.monotonic()
        snapshot = {"group_id": group, "group_version": version, "publisher_agent_id": self.agent_id,
                    "directory_revision": revision, "tracks": [{"name": name, "content_type": content}
                        for name, content in sorted(self.tracks.get(group, {}).items())]}
        await client.send(request, snapshot, timeout=max(.001, deadline - time.monotonic()))

    async def _prepare_directory(self, key, version, deadline):
        self.catalog_revisions.pop((key[0], key[2]), None)
        waiter = asyncio.get_running_loop().create_future()
        self.catalog_waiters[key] = waiter
        await self._request(key, version, deadline)
        try:
            await asyncio.wait_for(waiter, min(self.config.local_timeout, max(.001, deadline - time.monotonic())))
            self.statuses[key] = "READY"
            self._emit("catalog", group_id=key[0], member_agent_id=key[2], catalog=self.catalogs[(key[0], key[2])])
        except TimeoutError as exc:
            current = self.requests.pop(key, None)
            if current:
                await current[0].cancel(current[1])
            raise ConnectionError("first catalog snapshot timed out") from exc
        finally:
            if self.catalog_waiters.get(key) is waiter:
                del self.catalog_waiters[key]

    async def _subscribe(self, key, version, deadline):
        # Subscription is explicit; a catalog alone does not mark business ready.
        await self._request(key, version, deadline)

    def _object(self, role, client, request, value):
        key = next((key for key, current in self.requests.items() if current == (client, request)), None)
        if key is None or key in self.blocked or key[0] not in self.groups:
            return
        version = self.groups[key[0]].group_version
        if request.track.namespace != full_track(key[0], version, key[2], key[3]).namespace:
            return
        if key[3] == "group_catalog":
            try:
                catalog(encode_json(value), key[0], version, key[2], self.limits)
                previous = self.catalog_revisions.get((key[0], key[2]))
                if previous and (value["directory_revision"] < previous[0] or
                    (value["directory_revision"] == previous[0] and value != previous[1])):
                    raise ValueError("catalog revision conflict")
                if previous and value["directory_revision"] == previous[0]:
                    return
                self.catalog_revisions[(key[0], key[2])] = value["directory_revision"], value
                self.catalogs[(key[0], key[2])] = value
                waiter = self.catalog_waiters.get(key)
                if waiter is not None and not waiter.done():
                    waiter.set_result(value)
                names = {item["name"] for item in value["tracks"]}
                for subscribed in list(self.wanted):
                    if subscribed[:3] == key[:3] and subscribed[3] not in names:
                        self.statuses[subscribed] = "UNAVAILABLE"
                        current = self.requests.pop(subscribed, None)
                        if current:
                            self._background(current[0].cancel(current[1]))
                if self.statuses.get(key) == "READY":
                    self._emit("catalog", group_id=key[0], member_agent_id=key[2], catalog=value)
            except ValueError as exc:
                self._request_failed(role, client, request, exc)
                self._background(client.cancel(request))
        else:
            self._emit("message", group_id=key[0], group_version=version, publisher_agent_id=key[2], track=key[3], payload=value)

    def _request_failed(self, role, client, request, error):
        key = next((key for key, current in self.requests.items() if current == (client, request)), None)
        if key is None:
            return
        self.requests.pop(key, None)
        self.statuses[key] = "UNAVAILABLE"
        self._emit("unavailable", group_id=key[0], direction=role, member_agent_id=key[2], track=key[3], reason=str(error))
        if key[3] == "group_catalog" and role == "subscriber":
            self.catalogs.pop((key[0], key[2]), None)
        operation = self._prepare_publisher if role == "publisher" else self._prepare_directory if key[3] == "group_catalog" else self._subscribe
        self._spawn(key, operation)

    def _disconnected(self, role, client, code):
        if self.clients.get(role) is not client:
            return
        self.clients.pop(role, None)
        if code == SESSION_REPLACED:
            self.role_blocked.add(role)
        for key in list(self.statuses):
            if key[1] == role:
                self.statuses[key] = "UNAVAILABLE"
        self._emit("role_disconnected", direction=role, code=code, explicit_retry_required=code == SESSION_REPLACED)
        if code != SESSION_REPLACED:
            for group in self.groups:
                if role == "publisher":
                    self._spawn((group, role, self.agent_id, "group_catalog"), self._prepare_publisher)
                else:
                    for member in self.groups[group].members_by_agent_id:
                        if member != self.agent_id:
                            self._spawn((group, role, member, "group_catalog"), self._prepare_directory)
                    for key in self.wanted:
                        if key[0] == group:
                            self._spawn(key, self._subscribe)

    async def register_track(self, group, name, content_type="application/json"):
        self._require_group(group)
        if not name or name == "group_catalog" or len(name.encode()) > self.limits.track_name_bytes or not content_type:
            raise ValueError("invalid business Track")
        tracks = self.tracks.setdefault(group, {})
        if name not in tracks and len(tracks) >= self.limits.business_tracks:
            raise ValueError("business Track limit exceeded")
        tracks[name] = content_type
        key = (group, "publisher", self.agent_id, name)
        task = self._spawn(key, self._subscribe)
        if task:
            await asyncio.shield(task)
        if key not in self.requests or self.statuses.get(key) != "READY":
            raise RuntimeError("L5 publication unavailable; explicit retry may be required")
        self._queue_catalog(group)

    def _queue_catalog(self, group):
        self.catalog_changes[group] = self.catalog_changes.get(group, 0) + 1
        previous = self.pending_catalogs.get(group)
        if previous and not previous.done():
            return
        version = self.groups[group].group_version
        async def publish():
            try:
                deadline = time.monotonic() + self.config.recovery_timeout
                while self._valid(group, version):
                    change = self.catalog_changes[group]
                    await self._publish_catalog(group, version, deadline)
                    if change == self.catalog_changes[group]:
                        break
            except Exception as exc:
                self._emit("unavailable", group_id=group, direction="publisher", track="group_catalog", reason=str(exc))
        self.pending_catalogs[group] = asyncio.create_task(publish())

    async def unregister_track(self, group, name):
        self._require_group(group)
        self.tracks.get(group, {}).pop(name, None)
        key = (group, "publisher", self.agent_id, name)
        current = self.requests.pop(key, None)
        if current:
            await current[0].cancel(current[1])
        self._queue_catalog(group)

    async def subscribe(self, group, publisher, name):
        self._require_group(group)
        if (publisher == self.agent_id or publisher not in self.groups[group].members_by_agent_id
                or not isinstance(name, str) or not name or name == "group_catalog"
                or len(name.encode("utf-8")) > self.limits.track_name_bytes):
            raise ValueError("invalid business subscription")
        key = (group, "subscriber", publisher, name)
        self.wanted.add(key)
        task = self._spawn(key, self._subscribe)
        if task:
            await asyncio.shield(task)
        if key not in self.requests or self.statuses.get(key) != "READY":
            raise RuntimeError("L5 subscription unavailable; explicit retry may be required")

    async def unsubscribe(self, group, publisher, name):
        key = (group, "subscriber", publisher, name)
        self.wanted.discard(key)
        task = self.tasks.pop(key, None)
        if task:
            task.cancel()
        current = self.requests.pop(key, None)
        if current:
            await current[0].cancel(current[1])
        self.statuses.pop(key, None)

    async def publish(self, group, name, value, datagram=False):
        self._require_group(group)
        key = (group, "publisher", self.agent_id, name)
        current = self.requests.get(key)
        if name == "group_catalog" or name not in self.tracks.get(group, {}) or not current or self.statuses.get(key) != "READY":
            raise RuntimeError("business Track is not ready")
        await current[0].send(current[1], value, datagram=datagram)

    def retry(self, group, direction=None):
        self._require_group(group)
        if direction not in (None, "publisher", "subscriber"):
            raise ValueError("invalid L5 direction")
        for key in list(self.statuses):
            if (key[0] == group and key[2] in self.groups[group].members_by_agent_id
                    and self.statuses[key] != "READY" and (direction is None or key[1] == direction)):
                operation = self._prepare_publisher if key[1] == "publisher" and key[3] == "group_catalog" else self._prepare_directory if key[3] == "group_catalog" else self._subscribe
                self._spawn(key, operation, explicit=True)

    def _require_group(self, group):
        if self.closed or group not in self.groups:
            raise RuntimeError("no active ACN group for L5")

    def invalidate(self, group, release=True):
        self.groups.pop(group, None)
        for key, task in list(self.tasks.items()):
            if key[0] == group:
                task.cancel()
                del self.tasks[key]
        pending = self.pending_catalogs.pop(group, None)
        if pending:
            pending.cancel()
        for key, current in list(self.requests.items()):
            if key[0] == group:
                self.requests.pop(key, None)
                self._background(current[0].cancel(current[1]))
        for key in list(self.catalogs):
            if key[0] == group:
                del self.catalogs[key]
                self.catalog_revisions.pop(key, None)
        if release:
            self.tracks.pop(group, None)
            self.wanted = {key for key in self.wanted if key[0] != group}
        for key in list(self.statuses):
            if key[0] == group:
                self.statuses[key] = "UNAVAILABLE"
        if release and not self.groups:
            self._background(self._release_roles())

    async def _release_roles(self):
        if self.groups and not self.closed:
            return
        for task in self.connecting.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.connecting.values(), return_exceptions=True)
        for client in self.clients.values():
            await client.close()
        self.clients.clear()
        await self.routes.replace_group_peers(self.route_key, set())

    async def close(self):
        self.closed = True
        pending = list(self.tasks.values()) + list(self.pending_catalogs.values())
        for group in list(self.groups):
            self.invalidate(group, release=False)
        for task in self.pending_catalogs.values():
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self._release_roles()
        await asyncio.gather(*list(self.background), return_exceptions=True)
