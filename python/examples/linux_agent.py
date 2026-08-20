from __future__ import annotations

import argparse
import asyncio

from agent_sdk import AgentProfile, AgentSdk, NetworkMessageAction, NetworkMessageType
from agent_sdk.security import (
    DemoAcceptAllProofVerifier,
    DemoControlRequestAuthenticator,
    DemoMessageSignatureVerifier,
    DemoMessageSigner,
)


class NetworkListener:
    async def on_network_message(self, message_type, payload):
        if message_type is NetworkMessageType.GROUP_INVITATION:
            print("group invitation:", payload)
            return NetworkMessageAction.ACCEPT
        if message_type is NetworkMessageType.GROUP_CONFIG:
            print("group config received:", payload["group_id"])
            return NetworkMessageAction.ACK
        return NetworkMessageAction.REJECT


class GroupListener:
    async def on_group_message(self, group_id, sender_agent_id, payload):
        print(f"A2A {group_id=} {sender_agent_id=}: {payload}")


async def main(args) -> None:
    # DemoAcceptAllProofVerifier only checks proof presence. Replace it with the
    # deployment's trusted JWS verifier before using this example in production.
    sdk = AgentSdk(
        proof_verifier=DemoAcceptAllProofVerifier(),
        control_request_authenticator=DemoControlRequestAuthenticator(),
        message_signer=DemoMessageSigner(),
        message_signature_verifier=DemoMessageSignatureVerifier(),
    )
    sdk.set_local_profile_for_restore(
        AgentProfile(args.agent_id, args.agent_name, {})
    )
    sdk.register_network_message_listener(NetworkListener())
    sdk.register_group_message_listener(GroupListener())
    try:
        result = await sdk.init(
            args.runtime_ip,
            args.runtime_port,
            args.local_vlan_ip,
            args.tcp_port,
            args.udp_port,
            masque_server_url=args.masque_url,
            masque_authorization=(
                f"Bearer {args.masque_token}" if args.masque_token else None
            ),
            tun_name=args.tun_name,
            tun_mtu=args.tun_mtu,
            log_file_path=args.log_file,
            log_level=args.log_level,
            log_max_bytes=args.log_max_bytes,
            log_backup_count=args.log_backup_count,
        )
        print("SDK ready:", result)
        await asyncio.Event().wait()
    finally:
        await sdk.close()


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--runtime-ip", required=True)
    value.add_argument("--runtime-port", type=int, default=8080)
    value.add_argument("--local-vlan-ip", required=True)
    value.add_argument("--tcp-port", type=int, default=4001)
    value.add_argument("--udp-port", type=int, default=28443)
    value.add_argument("--agent-id", required=True)
    value.add_argument("--agent-name", required=True)
    value.add_argument("--masque-url", required=True)
    value.add_argument("--masque-token")
    value.add_argument("--tun-name", default="agent_tun0")
    value.add_argument("--tun-mtu", type=int, default=1280)
    value.add_argument("--log-file", default="./logs/agent-sdk.log")
    value.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default="INFO",
    )
    value.add_argument("--log-max-bytes", type=int, default=10 * 1024 * 1024)
    value.add_argument("--log-backup-count", type=int, default=5)
    return value


if __name__ == "__main__":
    asyncio.run(main(parser().parse_args()))
