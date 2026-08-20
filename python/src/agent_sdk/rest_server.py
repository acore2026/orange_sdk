from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Mapping

from aiohttp import web

from .errors import AgentSdkError, ErrorCode
from .logging_utils import log_event


class AiohttpLocalServer:
    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._runner: web.AppRunner | None = None
        self._sites: list[web.BaseSite] = []
        self._physical_ip = ""
        self._agent_ip = ""
        self._logger = logger or logging.getLogger(__name__)

    async def start(
        self,
        *,
        physical_ip: str,
        agent_ip: str,
        tcp_port: int,
        udp_port: int,
        on_a2a_message: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> None:
        del udp_port  # UDP application transport is a separate extension point.
        self._physical_ip = physical_ip
        self._agent_ip = agent_ip
        @web.middleware
        async def http_logging(request: web.Request, handler):
            request_id = uuid.uuid4().hex
            request["sdk_log_request_id"] = request_id
            peername = request.transport.get_extra_info("peername")
            log_event(
                self._logger,
                logging.INFO,
                "http_request",
                request_id=request_id,
                direction="inbound",
                peer="Agent",
                method=request.method,
                url=str(request.url),
                remote_address=peername[0] if peername else None,
                local_address=local_address(request),
                headers=dict(request.headers),
            )
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "http_response",
                    request_id=request_id,
                    direction="outbound",
                    peer="Agent",
                    method=request.method,
                    url=str(request.url),
                    status_code=exc.status,
                    body=exc.text,
                )
                raise
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.ERROR,
                    "http_error",
                    exc_info=True,
                    request_id=request_id,
                    direction="outbound",
                    peer="Agent",
                    method=request.method,
                    url=str(request.url),
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                raise
            response_body: Any = None
            if response.body:
                try:
                    response_body = json.loads(response.body)
                except (TypeError, ValueError):
                    response_body = f"<non-json-response:{len(response.body)} bytes>"
            log_event(
                self._logger,
                logging.INFO if response.status < 400 else logging.WARNING,
                "http_response",
                request_id=request_id,
                direction="outbound",
                peer="Agent",
                method=request.method,
                url=str(request.url),
                status_code=response.status,
                body=response_body,
            )
            return response

        app = web.Application(
            client_max_size=1024 * 1024,
            middlewares=[http_logging],
        )

        async def parse(request: web.Request) -> Mapping[str, Any]:
            try:
                payload = await request.json()
            except Exception as exc:
                raise web.HTTPBadRequest(text="invalid JSON") from exc
            if not isinstance(payload, Mapping):
                raise web.HTTPBadRequest(text="JSON object required")
            log_event(
                self._logger,
                logging.INFO,
                "http_request_body",
                request_id=request.get("sdk_log_request_id"),
                direction="inbound",
                peer="Agent",
                method=request.method,
                url=str(request.url),
                body=payload,
            )
            return payload

        def local_address(request: web.Request) -> str:
            sockname = request.transport.get_extra_info("sockname")
            return str(sockname[0]) if sockname else ""

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
