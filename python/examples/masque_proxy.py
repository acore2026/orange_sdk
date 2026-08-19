from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from agent_sdk.proxy import (
    LinuxUeInterfaceAdapter,
    MasqueProxyServer,
    ProxySessionPolicy,
    TokenSessionResolver,
)


async def main(config_path: str) -> None:
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    policies = {}
    for client in config["clients"]:
        interface_name = client["ue_interface"]
        policies[client["token"]] = ProxySessionPolicy(
            agent_ip=client["agent_ip"],
            allowed_peer_cidrs=tuple(client["allowed_peer_cidrs"]),
            mtu=int(client.get("mtu", 1280)),
            adapter_factory=lambda name=interface_name: LinuxUeInterfaceAdapter(name),
        )
    server = MasqueProxyServer(
        config["listen_host"],
        int(config["listen_port"]),
        config["certificate_path"],
        config["private_key_path"],
        TokenSessionResolver(policies),
    )
    await server.start()
    print(f"MASQUE proxy listening on {config['listen_host']}:{config['listen_port']}")
    try:
        await asyncio.Event().wait()
    finally:
        await server.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    asyncio.run(main(parser.parse_args().config))

