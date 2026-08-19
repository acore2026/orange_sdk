from __future__ import annotations

import json

import pytest

from agent_sdk.cli import build_session_resolver, load_proxy_config


def test_proxy_config_builds_device_specific_sessions(tmp_path):
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps(
            {
                "listen_host": "192.168.3.10",
                "listen_port": 4433,
                "certificate_path": "/tmp/cert.pem",
                "private_key_path": "/tmp/key.pem",
                "clients": [
                    {
                        "token": "token-a",
                        "agent_ip": "8.8.8.7",
                        "ue_interface": "uesimtun0",
                        "allowed_peer_cidrs": ["8.8.8.8/32"],
                    },
                    {
                        "token": "token-b",
                        "agent_ip": "8.8.8.8",
                        "ue_interface": "uesimtun1",
                        "allowed_peer_cidrs": ["8.8.8.7/32"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_proxy_config(path)
    resolver = build_session_resolver(config)

    policy_a = resolver.resolve({b"authorization": b"Bearer token-a"})
    policy_b = resolver.resolve({b"authorization": b"Bearer token-b"})
    assert policy_a is not None and policy_b is not None
    assert (policy_a.agent_ip, policy_a.adapter_factory().interface_name) == (
        "8.8.8.7",
        "uesimtun0",
    )
    assert (policy_b.agent_ip, policy_b.adapter_factory().interface_name) == (
        "8.8.8.8",
        "uesimtun1",
    )


def test_proxy_config_rejects_duplicate_tokens(tmp_path):
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "token": "same",
                        "agent_ip": "8.8.8.7",
                        "ue_interface": "uesimtun0",
                        "allowed_peer_cidrs": ["8.8.8.8/32"],
                    },
                    {
                        "token": "same",
                        "agent_ip": "8.8.8.8",
                        "ue_interface": "uesimtun1",
                        "allowed_peer_cidrs": ["8.8.8.7/32"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unique"):
        build_session_resolver(load_proxy_config(path))
