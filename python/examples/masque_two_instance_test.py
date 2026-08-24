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
from typing import Any, Mapping

import httpx
from aiohttp import web

from agent_sdk.masque import AioquicConnectIpTransport
from agent_sdk.routes import Pyroute2RouteBackend
from agent_sdk.tun import LinuxTunDevice, validate_ip_packet


def _json_object(value: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON message: {exc}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("message must be a JSON object")
    return parsed


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
    logger = logging.getLogger(f"masque_direct_test.{role}.{os.getpid()}")
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


class MessageServer:
    def __init__(
        self,
        *,
        role: str,
        local_agent_ip: str,
        peer_agent_ip: str,
        port: int,
        logger: logging.Logger,
        received_event: asyncio.Event,
    ) -> None:
        self._role = role
        self._local_agent_ip = str(ip_address(local_agent_ip))
        self._peer_agent_ip = str(ip_address(peer_agent_ip))
        self._port = port
        self._logger = logger
        self._received_event = received_event
        self._runner: web.AppRunner | None = None
        self.last_message: Mapping[str, Any] | None = None

    async def _message(self, request: web.Request) -> web.Response:
        remote = request.remote
        try:
            normalized_remote = str(ip_address(remote)) if remote else ""
        except ValueError:
            normalized_remote = ""
        if normalized_remote != self._peer_agent_ip:
            _emit(
                self._logger,
                self._role,
                "MESSAGE_REJECTED",
                reason="unexpected_source_ip",
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
        _emit(
            self._logger,
            self._role,
            "MESSAGE_RECEIVED",
            method="POST",
            path="/message",
            source_ip=normalized_remote,
            local_url=f"http://{_http_host(self._local_agent_ip)}:{self._port}/message",
            payload=dict(payload),
        )
        self._received_event.set()
        return web.json_response({"status": "OK"})

    async def start(self) -> None:
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_post("/message", self._message)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._local_agent_ip, self._port)
        try:
            await site.start()
        except Exception:
            await self.close()
            raise
        _emit(
            self._logger,
            self._role,
            "MESSAGE_SERVER_LISTENING",
            url=f"http://{_http_host(self._local_agent_ip)}:{self._port}/message",
        )

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


async def _pump_uplink(
    *,
    tun: LinuxTunDevice,
    masque: AioquicConnectIpTransport,
    local_agent_ip: str,
    peer_agent_ip: str,
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
        if source != local_agent_ip or destination != peer_agent_ip:
            _emit(
                logger,
                role,
                "IP_PACKET_DROPPED",
                direction="uplink",
                reason="outside_test_pair",
                source_ip=source,
                destination_ip=destination,
            )
            continue
        await masque.send_packet(packet)


async def _write_downlink(
    packet: bytes,
    *,
    tun: LinuxTunDevice,
    local_agent_ip: str,
    peer_agent_ip: str,
    mtu: int,
    logger: logging.Logger,
    role: str,
) -> None:
    try:
        source, destination = validate_ip_packet(packet, mtu)
    except ValueError as exc:
        _emit(logger, role, "IP_PACKET_DROPPED", direction="downlink", reason=str(exc))
        return
    if source != peer_agent_ip or destination != local_agent_ip:
        _emit(
            logger,
            role,
            "IP_PACKET_DROPPED",
            direction="downlink",
            reason="outside_test_pair",
            source_ip=source,
            destination_ip=destination,
        )
        return
    await tun.write(packet)


async def _post_message(
    args: argparse.Namespace,
    logger: logging.Logger,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    url = f"http://{_http_host(args.peer_agent_ip)}:{args.message_port}/message"
    _emit(
        logger,
        args.role,
        "MESSAGE_SENDING",
        method="POST",
        url=url,
        payload=dict(args.message),
    )
    async with httpx.AsyncClient(
        timeout=args.message_timeout,
        trust_env=False,
        transport=transport,
    ) as client:
        response = await client.post(url, json=dict(args.message))
        response.raise_for_status()
        result = response.json()
    if not isinstance(result, Mapping) or result.get("status") != "OK":
        raise RuntimeError(f"peer returned an invalid response: {result!r}")
    _emit(
        logger,
        args.role,
        "MESSAGE_DELIVERED",
        url=url,
        response=dict(result),
    )


def _apply_role_defaults(args: argparse.Namespace) -> argparse.Namespace:
    role = args.role.upper()
    args.role = role
    suffix = role.lower()
    args.tun_name = args.tun_name or f"agent_tun_{suffix}"
    args.state_dir = args.state_dir or f"./state/masque-direct-{suffix}"
    args.log_file = args.log_file or f"./logs/masque-direct-{suffix}.log"
    return args


def _validate_args(args: argparse.Namespace) -> None:
    ip_address(args.local_vlan_ip)
    local_agent_ip = ip_address(args.local_agent_ip)
    peer_agent_ip = ip_address(args.peer_agent_ip)
    if local_agent_ip.version != peer_agent_ip.version:
        raise ValueError("local and peer Agent IPs must use the same address family")
    if local_agent_ip == peer_agent_ip:
        raise ValueError("local and peer Agent IPs must be different")
    if args.message_timeout <= 0:
        raise ValueError("message timeout must be greater than zero")
    if not 1 <= args.message_port <= 65535:
        raise ValueError("message port must be in 1..65535")
    if args.send_delay < 0:
        raise ValueError("send delay must be zero or greater")
    if args.role == "B" and args.receive_timeout < 0:
        raise ValueError("receive timeout must be zero or greater")
    if args.post_receive_linger < 0:
        raise ValueError("post-receive linger must be zero or greater")
    current_netns = _netns_id()
    if (
        args.peer_netns_id is not None
        and current_netns is not None
        and args.peer_netns_id == current_netns
    ):
        raise ValueError(
            "A and B are in the same Linux network namespace; local routing can "
            "bypass MASQUE, so this result would not prove the server path"
        )


async def run_instance(
    args: argparse.Namespace,
    *,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    args = _apply_role_defaults(args)
    _validate_args(args)
    args.local_agent_ip = str(ip_address(args.local_agent_ip))
    args.peer_agent_ip = str(ip_address(args.peer_agent_ip))

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
        local_agent_ip=args.local_agent_ip,
        peer_agent_ip=args.peer_agent_ip,
    )

    tun: LinuxTunDevice | None = None
    route_backend: Pyroute2RouteBackend | None = None
    route_installed = False
    masque: AioquicConnectIpTransport | None = None
    pump_task: asyncio.Task[None] | None = None
    message_server: MessageServer | None = None
    received_event = asyncio.Event()
    try:
        tun = await LinuxTunDevice.create(
            args.tun_name,
            _host_cidr(args.local_agent_ip),
            args.tun_mtu,
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
            assert tun is not None
            await _write_downlink(
                packet,
                tun=tun,
                local_agent_ip=args.local_agent_ip,
                peer_agent_ip=args.peer_agent_ip,
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
                local_agent_ip=args.local_agent_ip,
                peer_agent_ip=args.peer_agent_ip,
                mtu=args.tun_mtu,
                logger=logger,
                role=args.role,
            ),
            name=f"masque-direct-uplink-{args.role.lower()}",
        )
        route_backend = Pyroute2RouteBackend(tun.name, args.local_agent_ip)
        await route_backend.add(_host_cidr(args.peer_agent_ip))
        route_installed = True
        _emit(
            logger,
            args.role,
            "PEER_ROUTE_READY",
            peer_cidr=_host_cidr(args.peer_agent_ip),
            tun_name=tun.name,
        )

        message_server = MessageServer(
            role=args.role,
            local_agent_ip=args.local_agent_ip,
            peer_agent_ip=args.peer_agent_ip,
            port=args.message_port,
            logger=logger,
            received_event=received_event,
        )
        await message_server.start()

        if args.role == "A":
            if args.send_delay:
                await asyncio.sleep(args.send_delay)
            await _post_message(args, logger, transport=http_transport)
            _emit(logger, args.role, "TEST_PASSED", proof="MESSAGE_DELIVERED")
        else:
            _emit(
                logger,
                args.role,
                "WAITING_FOR_MESSAGE",
                url=(
                    f"http://{_http_host(args.local_agent_ip)}:"
                    f"{args.message_port}/message"
                ),
                receive_timeout_seconds=args.receive_timeout,
            )
            try:
                if args.receive_timeout == 0:
                    await received_event.wait()
                else:
                    await asyncio.wait_for(received_event.wait(), args.receive_timeout)
            except TimeoutError as exc:
                raise TimeoutError("B timed out waiting for POST /message") from exc
            _emit(logger, args.role, "TEST_PASSED", proof="MESSAGE_RECEIVED")
            if args.post_receive_linger:
                await asyncio.sleep(args.post_receive_linger)
    finally:
        try:
            if message_server is not None:
                try:
                    await message_server.close()
                except Exception as exc:
                    _emit(
                        logger,
                        args.role,
                        "CLEANUP_ERROR",
                        resource="message_server",
                        error=str(exc),
                    )
            if route_backend is not None and route_installed:
                try:
                    await route_backend.remove(_host_cidr(args.peer_agent_ip))
                except Exception as exc:
                    _emit(
                        logger,
                        args.role,
                        "CLEANUP_ERROR",
                        resource="peer_route",
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
            "Direct MASQUE path test without Agent ID or group APIs. Start B "
            "first; A POSTs JSON to http://<B Agent IP>:4001/message."
        )
    )
    value.add_argument("--role", required=True, type=str.upper, choices=("A", "B"))
    value.add_argument("--local-vlan-ip", required=True)
    value.add_argument("--local-agent-ip", required=True)
    value.add_argument("--peer-agent-ip", required=True)
    value.add_argument("--masque-url", required=True)
    value.add_argument("--masque-token")
    value.add_argument("--message-port", type=int, default=4001)
    value.add_argument("--tun-name")
    value.add_argument("--tun-mtu", type=int, default=1280)
    value.add_argument(
        "--message",
        type=_json_object,
        default={"type": "text", "content": "hello B from A through MASQUE"},
    )
    value.add_argument("--message-timeout", type=float, default=10.0)
    value.add_argument("--send-delay", type=float, default=1.0)
    value.add_argument(
        "--receive-timeout",
        type=float,
        default=300.0,
        help="role B wait time in seconds; use 0 to wait forever",
    )
    value.add_argument(
        "--post-receive-linger",
        type=float,
        default=2.0,
        help="seconds B keeps /message alive after receipt so status=OK is flushed",
    )
    value.add_argument(
        "--peer-netns-id",
        type=int,
        help=(
            "network namespace ID printed by the peer; equal IDs are rejected "
            "because they cannot prove the MASQUE path"
        ),
    )
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
        print(f"MASQUE DIRECT TEST FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
