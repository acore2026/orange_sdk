from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

from .proxy import (
    LinuxUeInterfaceAdapter,
    MasqueProxyServer,
    ProxySessionPolicy,
    TokenSessionResolver,
)
from .logging_utils import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_LEVEL,
    DEFAULT_LOG_MAX_BYTES,
    close_logger,
    configure_local_logger,
)


def load_proxy_config(path: str | Path) -> Mapping[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("MASQUE proxy config must be a JSON object")
    clients = value.get("clients")
    if not isinstance(clients, list) or not clients:
        raise ValueError("MASQUE proxy config clients must be a non-empty array")
    return value


def build_session_resolver(config: Mapping[str, Any]) -> TokenSessionResolver:
    policies: dict[str, ProxySessionPolicy] = {}
    for index, client in enumerate(config["clients"]):
        if not isinstance(client, Mapping):
            raise ValueError(f"clients[{index}] must be a JSON object")
        token = str(client["token"])
        if not token or token in policies:
            raise ValueError(f"clients[{index}].token must be non-empty and unique")
        interface_name = str(client["ue_interface"])
        policies[token] = ProxySessionPolicy(
            agent_ip=str(client["agent_ip"]),
            allowed_peer_cidrs=tuple(str(item) for item in client["allowed_peer_cidrs"]),
            mtu=int(client.get("mtu", 1280)),
            adapter_factory=lambda name=interface_name: LinuxUeInterfaceAdapter(name),
        )
    return TokenSessionResolver(policies)


async def run_masque_proxy(config_path: str | Path) -> None:
    config = load_proxy_config(config_path)
    logger = configure_local_logger(
        name="agent_sdk.masque_proxy",
        file_path=str(config.get("log_file_path", "./logs/masque-proxy.log")),
        level=str(config.get("log_level", DEFAULT_LOG_LEVEL)),
        max_bytes=int(config.get("log_max_bytes", DEFAULT_LOG_MAX_BYTES)),
        backup_count=int(
            config.get("log_backup_count", DEFAULT_LOG_BACKUP_COUNT)
        ),
    )
    server = MasqueProxyServer(
        str(config["listen_host"]),
        int(config["listen_port"]),
        str(config["certificate_path"]),
        str(config["private_key_path"]),
        build_session_resolver(config),
        logger=logger,
    )
    try:
        await server.start()
        print(f"MASQUE proxy listening on {config['listen_host']}:{config['listen_port']}")
        try:
            await asyncio.Event().wait()
        finally:
            await server.close()
    finally:
        close_logger(logger)


def masque_proxy_main() -> None:
    parser = argparse.ArgumentParser(
        description="Start the Agent SDK MASQUE CONNECT-IP proxy."
    )
    parser.add_argument("--config", required=True, help="Path to proxy JSON config")
    args = parser.parse_args()
    try:
        asyncio.run(run_masque_proxy(args.config))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
