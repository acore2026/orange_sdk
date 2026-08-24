from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from ipaddress import ip_address
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx
from aiohttp import web

from agent_sdk.masque import AioquicConnectIpTransport
from agent_sdk.routes import Pyroute2RouteBackend
from agent_sdk.runtime import HttpRuntimeTransport
from agent_sdk.tun import LinuxTunDevice, validate_ip_packet


def _netns_id() -> int | None:
    try:
        return os.stat("/proc/self/ns/net").st_ino
    except OSError:
        return None


def _host_cidr(address: str) -> str:
    parsed = ip_address(address)
    return f"{parsed}/{32 if parsed.version == 4 else 128}"


def _http_host(address: str) -> str:
    return f"[{address}]" if ip_address(address).version == 6 else address


def _emit(logger: logging.Logger, role: str, event: str, **fields: Any) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "role": role,
        "event": event,
        **fields,
    }
    logger.info(json.dumps(record, ensure_ascii=False, default=str))


def _configure_logger(role: str, file_path: str) -> logging.Logger:
    destination = Path(file_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"masque_interactive_test.{role}.{os.getpid()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    local_file = RotatingFileHandler(
        destination,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    local_file.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)
    logger.addHandler(local_file)
    return logger


class PeerState:
    def __init__(
        self,
        *,
        local_agent_ip: str,
        route_backend: Pyroute2RouteBackend,
        role: str,
        logger: logging.Logger,
    ) -> None:
        self.local_agent_ip = str(ip_address(local_agent_ip))
        self.peer_agent_ip: str | None = None
        self._route_backend = route_backend
        self._role = role
        self._logger = logger
        self._lock = asyncio.Lock()

    async def configure(self, peer_agent_ip: str) -> str:
        parsed = ip_address(peer_agent_ip)
        local = ip_address(self.local_agent_ip)
        if parsed.version != local.version:
            raise ValueError("peer Agent IP must use the local address family")
        normalized = str(parsed)
        if normalized == self.local_agent_ip:
            raise ValueError("peer Agent IP must differ from local Agent IP")

        async with self._lock:
            previous = self.peer_agent_ip
            if previous == normalized:
                return normalized
            await self._route_backend.add(_host_cidr(normalized))
            try:
                if previous is not None:
                    await self._route_backend.remove(_host_cidr(previous))
            except Exception:
                await self._route_backend.remove(_host_cidr(normalized))
                raise
            self.peer_agent_ip = normalized

        _emit(
            self._logger,
            self._role,
            "PEER_AGENT_IP_CONFIGURED",
            local_agent_ip=self.local_agent_ip,
            peer_agent_ip=normalized,
            peer_route=_host_cidr(normalized),
        )
        return normalized

    async def close(self) -> None:
        async with self._lock:
            peer = self.peer_agent_ip
            self.peer_agent_ip = None
        if peer is not None:
            await self._route_backend.remove(_host_cidr(peer))


class MessageServer:
    def __init__(
        self,
        *,
        role: str,
        peer_state: PeerState,
        port: int,
        logger: logging.Logger,
    ) -> None:
        self._role = role
        self._peer_state = peer_state
        self._port = port
        self._logger = logger
        self._runner: web.AppRunner | None = None
        self.last_message: Mapping[str, Any] | None = None

    async def _message(self, request: web.Request) -> web.Response:
        expected_peer = self._peer_state.peer_agent_ip
        if expected_peer is None:
            return web.json_response(
                {"status": "ERROR", "detail": "peer Agent IP is not configured"},
                status=409,
            )
        remote = request.remote
        try:
            normalized_remote = str(ip_address(remote)) if remote else ""
        except ValueError:
            normalized_remote = ""
        if normalized_remote != expected_peer:
            _emit(
                self._logger,
                self._role,
                "MESSAGE_REJECTED",
                reason="unexpected_source_ip",
                expected_source_ip=expected_peer,
                source_ip=remote,
            )
            return web.json_response(
                {"status": "ERROR", "detail": "unexpected source Agent IP"},
                status=403,
            )
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"status": "ERROR", "detail": "invalid JSON"}, status=400
            )
        if not isinstance(payload, Mapping):
            return web.json_response(
                {"status": "ERROR", "detail": "JSON object required"}, status=400
            )
        self.last_message = dict(payload)
        local_url = (
            f"http://{_http_host(self._peer_state.local_agent_ip)}:"
            f"{self._port}/message"
        )
        _emit(
            self._logger,
            self._role,
            "MESSAGE_RECEIVED",
            method="POST",
            path="/message",
            source_ip=normalized_remote,
            local_url=local_url,
            payload=dict(payload),
        )
        return web.json_response({"status": "OK"})

    async def start(self) -> None:
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_post("/message", self._message)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner,
            self._peer_state.local_agent_ip,
            self._port,
        )
        try:
            await site.start()
        except Exception:
            await self.close()
            raise
        _emit(
            self._logger,
            self._role,
            "MESSAGE_SERVER_LISTENING",
            url=(
                f"http://{_http_host(self._peer_state.local_agent_ip)}:"
                f"{self._port}/message"
            ),
        )

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


async def _post_message(
    *,
    peer_agent_ip: str,
    message_port: int,
    message: Mapping[str, Any],
    timeout: float,
    role: str,
    logger: logging.Logger,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Mapping[str, Any]:
    url = f"http://{_http_host(peer_agent_ip)}:{message_port}/message"
    _emit(
        logger,
        role,
        "MESSAGE_SENDING",
        method="POST",
        url=url,
        payload=dict(message),
    )
    async with httpx.AsyncClient(
        timeout=timeout,
        trust_env=False,
        transport=transport,
    ) as client:
        response = await client.post(url, json=dict(message))
        response.raise_for_status()
        result = response.json()
    if not isinstance(result, Mapping) or result.get("status") != "OK":
        raise RuntimeError(f"peer returned an invalid response: {result!r}")
    _emit(logger, role, "MESSAGE_DELIVERED", url=url, response=dict(result))
    return dict(result)


class ControlServer:
    def __init__(
        self,
        *,
        role: str,
        host: str,
        port: int,
        peer_state: PeerState,
        message_port: int,
        message_timeout: float,
        logger: logging.Logger,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._role = role
        self._host = host
        self._port = port
        self._peer_state = peer_state
        self._message_port = message_port
        self._message_timeout = message_timeout
        self._logger = logger
        self._http_transport = http_transport
        self._runner: web.AppRunner | None = None

    @staticmethod
    async def _json_object(request: web.Request) -> Mapping[str, Any]:
        try:
            payload = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(text="invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise web.HTTPBadRequest(text="JSON object required")
        return payload

    async def _configure_peer(self, request: web.Request) -> web.Response:
        payload = await self._json_object(request)
        raw_peer = payload.get("peer_agent_ip")
        if not isinstance(raw_peer, str) or not raw_peer:
            raise web.HTTPBadRequest(text="peer_agent_ip must be a non-empty string")
        try:
            peer = await self._peer_state.configure(raw_peer)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response(
            {
                "status": "OK",
                "local_agent_ip": self._peer_state.local_agent_ip,
                "peer_agent_ip": peer,
                "message_url": (
                    f"http://{_http_host(peer)}:{self._message_port}/message"
                ),
            }
        )

    async def _send(self, request: web.Request) -> web.Response:
        peer = self._peer_state.peer_agent_ip
        if peer is None:
            return web.json_response(
                {"status": "ERROR", "detail": "configure peer Agent IP first"},
                status=409,
            )
        message = await self._json_object(request)
        try:
            result = await _post_message(
                peer_agent_ip=peer,
                message_port=self._message_port,
                message=message,
                timeout=self._message_timeout,
                role=self._role,
                logger=self._logger,
                transport=self._http_transport,
            )
        except Exception as exc:
            _emit(
                self._logger,
                self._role,
                "MESSAGE_SEND_FAILED",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return web.json_response(
                {"status": "ERROR", "detail": str(exc)}, status=502
            )
        return web.json_response(
            {"status": "OK", "peer_agent_ip": peer, "peer_response": result}
        )

    async def _status(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(
            {
                "status": "READY",
                "role": self._role,
                "local_agent_ip": self._peer_state.local_agent_ip,
                "peer_agent_ip": self._peer_state.peer_agent_ip,
                "masque_connected": True,
            }
        )

    async def start(self) -> None:
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_get("/test/status", self._status)
        app.router.add_post("/test/peer", self._configure_peer)
        app.router.add_post("/test/send", self._send)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        try:
            await site.start()
        except Exception:
            await self.close()
            raise
        base_url = f"http://{self._host}:{self._port}"
        _emit(
            self._logger,
            self._role,
            "CONTROL_SERVER_LISTENING",
            base_url=base_url,
            configure_peer_url=f"{base_url}/test/peer",
            send_url=f"{base_url}/test/send",
        )

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


async def _pump_uplink(
    *,
    tun: LinuxTunDevice,
    masque: AioquicConnectIpTransport,
    peer_state: PeerState,
    mtu: int,
    logger: logging.Logger,
    role: str,
) -> None:
    while True:
        packet = await tun.read()
        if not packet:
            return
        try:
            source, destination = validate_ip_packet(packet, mtu)
        except ValueError as exc:
            _emit(logger, role, "IP_PACKET_DROPPED", direction="uplink", reason=str(exc))
            continue
        peer = peer_state.peer_agent_ip
        if peer is None or source != peer_state.local_agent_ip or destination != peer:
            _emit(
                logger,
                role,
                "IP_PACKET_DROPPED",
                direction="uplink",
                reason="outside_configured_pair",
                source_ip=source,
                destination_ip=destination,
            )
            continue
        await masque.send_packet(packet)


async def _write_downlink(
    packet: bytes,
    *,
    tun: LinuxTunDevice,
    peer_state: PeerState,
    mtu: int,
    logger: logging.Logger,
    role: str,
) -> None:
    try:
        source, destination = validate_ip_packet(packet, mtu)
    except ValueError as exc:
        _emit(logger, role, "IP_PACKET_DROPPED", direction="downlink", reason=str(exc))
        return
    peer = peer_state.peer_agent_ip
    if peer is None or source != peer or destination != peer_state.local_agent_ip:
        _emit(
            logger,
            role,
            "IP_PACKET_DROPPED",
            direction="downlink",
            reason="outside_configured_pair",
            source_ip=source,
            destination_ip=destination,
        )
        return
    await tun.write(packet)


def _apply_role_defaults(args: argparse.Namespace) -> argparse.Namespace:
    args.role = args.role.upper()
    suffix = args.role.lower()
    args.tun_name = args.tun_name or f"agent_tun_{suffix}"
    args.control_port = args.control_port or (18081 if args.role == "A" else 18082)
    args.state_dir = args.state_dir or f"./state/masque-interactive-{suffix}"
    args.log_file = args.log_file or f"./logs/masque-interactive-{suffix}.log"
    return args


def _validate_args(args: argparse.Namespace) -> None:
    ip_address(args.local_vlan_ip)
    if not 1 <= args.runtime_port <= 65535:
        raise ValueError("Runtime port must be in 1..65535")
    if not 1 <= args.message_port <= 65535:
        raise ValueError("message port must be in 1..65535")
    if not 1 <= args.control_port <= 65535:
        raise ValueError("control port must be in 1..65535")
    if args.message_timeout <= 0:
        raise ValueError("message timeout must be greater than zero")


async def _query_agent_ip(
    *,
    runtime_ip: str,
    runtime_port: int,
    role: str,
    logger: logging.Logger,
    runtime_factory: Callable[..., HttpRuntimeTransport] = HttpRuntimeTransport,
) -> str:
    runtime = runtime_factory(runtime_ip, runtime_port, logger=logger)
    try:
        agent_ip = await runtime.get_ue_agent_ip()
    finally:
        await runtime.close()
    _emit(
        logger,
        role,
        "UE_INFO_AGENT_TUN_IP",
        method="GET",
        url=f"http://{runtime_ip}:{runtime_port}/v1/ue/info",
        agent_tun_ip=agent_ip,
        agent_tun_cidr=_host_cidr(agent_ip),
    )
    return agent_ip


async def run_instance(
    args: argparse.Namespace,
    *,
    runtime_factory: Callable[..., HttpRuntimeTransport] = HttpRuntimeTransport,
    http_transport: httpx.AsyncBaseTransport | None = None,
    ready_event: asyncio.Event | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    args = _apply_role_defaults(args)
    _validate_args(args)
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    previous_state_home = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = str(state_dir)
    logger = _configure_logger(args.role, args.log_file)
    _emit(
        logger,
        args.role,
        "INSTANCE_STARTING",
        pid=os.getpid(),
        netns_id=_netns_id(),
        runtime_url=f"http://{args.runtime_ip}:{args.runtime_port}",
    )

    tun: LinuxTunDevice | None = None
    peer_state: PeerState | None = None
    masque: AioquicConnectIpTransport | None = None
    pump_task: asyncio.Task[None] | None = None
    message_server: MessageServer | None = None
    control_server: ControlServer | None = None
    try:
        local_agent_ip = await _query_agent_ip(
            runtime_ip=args.runtime_ip,
            runtime_port=args.runtime_port,
            role=args.role,
            logger=logger,
            runtime_factory=runtime_factory,
        )
        tun = await LinuxTunDevice.create(
            args.tun_name,
            _host_cidr(local_agent_ip),
            args.tun_mtu,
        )
        route_backend = Pyroute2RouteBackend(tun.name, local_agent_ip)
        peer_state = PeerState(
            local_agent_ip=local_agent_ip,
            route_backend=route_backend,
            role=args.role,
            logger=logger,
        )
        masque = AioquicConnectIpTransport(
            server_url=args.masque_url,
            authorization=(
                f"Bearer {args.masque_token}" if args.masque_token else None
            ),
            local_address=args.local_vlan_ip,
            logger=logger,
        )

        async def downlink(packet: bytes) -> None:
            assert tun is not None and peer_state is not None
            await _write_downlink(
                packet,
                tun=tun,
                peer_state=peer_state,
                mtu=args.tun_mtu,
                logger=logger,
                role=args.role,
            )

        await masque.start(downlink)
        if not masque.connected:
            raise RuntimeError("MASQUE CONNECT-IP did not become connected")
        _emit(
            logger,
            args.role,
            "MASQUE_CONNECTED",
            masque_url=args.masque_url,
            tun_name=tun.name,
            tun_cidr=tun.cidr,
        )
        pump_task = asyncio.create_task(
            _pump_uplink(
                tun=tun,
                masque=masque,
                peer_state=peer_state,
                mtu=args.tun_mtu,
                logger=logger,
                role=args.role,
            ),
            name=f"masque-interactive-uplink-{args.role.lower()}",
        )
        message_server = MessageServer(
            role=args.role,
            peer_state=peer_state,
            port=args.message_port,
            logger=logger,
        )
        await message_server.start()
        control_server = ControlServer(
            role=args.role,
            host=args.control_host,
            port=args.control_port,
            peer_state=peer_state,
            message_port=args.message_port,
            message_timeout=args.message_timeout,
            logger=logger,
            http_transport=http_transport,
        )
        await control_server.start()
        _emit(
            logger,
            args.role,
            "INSTANCE_READY",
            local_agent_ip=local_agent_ip,
            control_url=f"http://{args.control_host}:{args.control_port}",
        )
        if ready_event is not None:
            ready_event.set()
        await (stop_event or asyncio.Event()).wait()
    finally:
        try:
            for resource_name, closer in (
                ("control_server", control_server.close if control_server else None),
                ("message_server", message_server.close if message_server else None),
                ("peer_route", peer_state.close if peer_state else None),
            ):
                if closer is not None:
                    try:
                        await closer()
                    except Exception as exc:
                        _emit(
                            logger,
                            args.role,
                            "CLEANUP_ERROR",
                            resource=resource_name,
                            error=str(exc),
                        )
            if pump_task is not None:
                pump_task.cancel()
                await asyncio.gather(pump_task, return_exceptions=True)
            if masque is not None:
                try:
                    await masque.close()
                except Exception as exc:
                    _emit(
                        logger,
                        args.role,
                        "CLEANUP_ERROR",
                        resource="masque",
                        error=str(exc),
                    )
            if tun is not None:
                try:
                    await tun.close()
                except Exception as exc:
                    _emit(
                        logger,
                        args.role,
                        "CLEANUP_ERROR",
                        resource="tun",
                        error=str(exc),
                    )
            _emit(logger, args.role, "INSTANCE_CLOSED")
        finally:
            if previous_state_home is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = previous_state_home


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Interactive MASQUE path test. Each instance gets its Agent TUN IP "
            "from GET /v1/ue/info; curl configures the peer and triggers sending."
        )
    )
    value.add_argument("--role", required=True, type=str.upper, choices=("A", "B"))
    value.add_argument("--runtime-ip", required=True)
    value.add_argument("--runtime-port", required=True, type=int)
    value.add_argument("--local-vlan-ip", required=True)
    value.add_argument("--masque-url", required=True)
    value.add_argument("--masque-token")
    value.add_argument("--message-port", type=int, default=4001)
    value.add_argument("--control-host", default="127.0.0.1")
    value.add_argument("--control-port", type=int)
    value.add_argument("--message-timeout", type=float, default=10.0)
    value.add_argument("--tun-name")
    value.add_argument("--tun-mtu", type=int, default=1280)
    value.add_argument("--state-dir")
    value.add_argument("--log-file")
    return value


if __name__ == "__main__":
    parsed_args = parser().parse_args()
    try:
        asyncio.run(run_instance(parsed_args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
    except Exception as error:
        print(f"MASQUE INTERACTIVE TEST FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
