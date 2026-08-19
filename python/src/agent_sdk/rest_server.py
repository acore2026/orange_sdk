from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Mapping

from aiohttp import web

from .errors import AgentSdkError, ErrorCode
from .models import NetworkMessageAction


class AiohttpLocalServer:
    def __init__(self) -> None:
        self._runner: web.AppRunner | None = None
        self._sites: list[web.BaseSite] = []
        self._physical_ip = ""
        self._agent_ip = ""

    async def start(
        self,
        *,
        physical_ip: str,
        agent_ip: str,
        tcp_port: int,
        udp_port: int,
        on_group_config: Callable[[Mapping[str, Any]], Awaitable[NetworkMessageAction]],
        on_group_invitation: Callable[
            [Mapping[str, Any]], Awaitable[NetworkMessageAction]
        ],
        on_a2a_message: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> None:
        del udp_port  # UDP application transport is a separate extension point.
        self._physical_ip = physical_ip
        self._agent_ip = agent_ip
        app = web.Application(client_max_size=1024 * 1024)

        async def parse(request: web.Request) -> Mapping[str, Any]:
            try:
                payload = await request.json()
            except Exception as exc:
                raise web.HTTPBadRequest(text="invalid JSON") from exc
            if not isinstance(payload, Mapping):
                raise web.HTTPBadRequest(text="JSON object required")
            return payload

        def local_address(request: web.Request) -> str:
            sockname = request.transport.get_extra_info("sockname")
            return str(sockname[0]) if sockname else ""

        async def group_config(request: web.Request) -> web.Response:
            if local_address(request) != self._physical_ip:
                raise web.HTTPForbidden(text="Runtime callback must use physical ingress")
            try:
                action = await on_group_config(await parse(request))
                return web.json_response({"action": action.value})
            except AgentSdkError as exc:
                return web.json_response(
                    {"action": "REJECT", "code": exc.code.value}, status=400
                )
            except Exception:
                return web.json_response(
                    {"action": "REJECT", "code": "CALLBACK_FAILED"}, status=500
                )

        async def invitation(request: web.Request) -> web.Response:
            if local_address(request) != self._physical_ip:
                raise web.HTTPForbidden(text="Runtime callback must use physical ingress")
            try:
                action = await on_group_invitation(await parse(request))
                return web.json_response({"action": action.value})
            except Exception:
                return web.json_response(
                    {"action": "REJECT", "code": "CALLBACK_FAILED"}, status=500
                )

        async def a2a(request: web.Request) -> web.Response:
            if local_address(request) != self._agent_ip:
                raise web.HTTPForbidden(text="A2A message must use Agent TUN ingress")
            try:
                await on_a2a_message(await parse(request))
                return web.json_response({"ack": True})
            except AgentSdkError as exc:
                return web.json_response(
                    {"ack": False, "code": exc.code.value}, status=400
                )
            except Exception:
                return web.json_response(
                    {"ack": False, "code": "CALLBACK_FAILED"}, status=500
                )

        app.router.add_post("/agent/group-moq-info", group_config)
        app.router.add_post("/agent/group-invitation", invitation)
        app.router.add_post("/A2A/message", a2a)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        try:
            for address in dict.fromkeys((physical_ip, agent_ip)):
                site = web.TCPSite(self._runner, address, tcp_port)
                await site.start()
                self._sites.append(site)
        except OSError as exc:
            await self.close()
            raise AgentSdkError(
                ErrorCode.LOCAL_PORT_IN_USE,
                f"cannot bind SDK REST server: {exc}",
            ) from exc

    async def close(self) -> None:
        for site in reversed(self._sites):
            await site.stop()
        self._sites.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
