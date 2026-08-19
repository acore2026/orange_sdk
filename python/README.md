# Linux Python Agent SDK

The package implements the V4.1 design: explicit initialization, Linux TUN,
MASQUE CONNECT-IP over HTTP/3 Datagram, dual REST ingress, verified
`acf_group_config` caching, dynamic peer routes, and cache-only A2A endpoint
resolution.

## Install and test

```bash
cd python
python3 -m pip install -e '.[test]'
pytest -q
```

The real Linux example requires `/dev/net/tun`, `CAP_NET_ADMIN`, a reachable
AgentRuntime, a MASQUE proxy, and a trusted CA:

```bash
sudo -E PYTHONPATH=src python3 examples/linux_agent.py \
  --runtime-ip 192.168.3.10 \
  --local-vlan-ip 192.168.1.10 \
  --agent-tun-cidr 8.8.8.7/24 \
  --agent-id 'did:example:agent-a' \
  --agent-name 'Agent A' \
  --masque-url https://192.168.3.10:4433 \
  --masque-server-name masque.lab.example \
  --masque-token "$(secret-tool lookup service agent-masque)" \
  --ca-cert /etc/agent-sdk/lab-ca.pem
```

Run the server proxy (requires `CAP_NET_RAW` and access to the configured
UERANSIM interfaces):

```bash
sudo -E PYTHONPATH=src python3 examples/masque_proxy.py \
  --config examples/masque-proxy.example.json
```

The proxy authenticates each CONNECT-IP request and maps its bearer token to
one Agent IP and one `uesimtun` interface. Replace every example secret and
address through deployment configuration.

The example proof verifier is intentionally demo-only. Production deployments
must inject a verifier backed by the configured network trust anchor.

After a valid `acf_group_config` notification arrives, the SDK verifies and
commits the member endpoint cache and peer routes internally. The optional
network listener is notified after the commit; its return value does not control
whether the configuration is accepted.

The bundled local callback/A2A server uses HTTP/1.1 inside the CONNECT-IP L3
path and relies on signed messages. The design's final callback mTLS credential
format is still an AgentRuntime integration item; deployments that have fixed
that format should inject a TLS `LocalServer` and an HTTPS `PeerMessenger`.

Video APIs are present on `AgentSdk` and delegate camera/WebRTC ownership to a
`MediaOffloadAdapter`. This keeps the SDK's northbound API stable while allowing
the deployment to choose aiortc, GStreamer WebRTC, or a hardware media stack.
