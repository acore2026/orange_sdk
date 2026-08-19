# Android / RayNeoOS Agent SDK

The Android library mirrors the Python SDK's group-cache and endpoint rules:

- `AgentVpnService` creates the Agent TUN without root.
- `acf_group_config` is decoded into an immutable snapshot keyed by
  `group_id + agent_id`.
- A2A TCP always uses cached `agent_ip + tcp_port`.
- Group changes rebuild VPN routes and atomically swap the TUN fd in the native
  MASQUE core.
- Runtime callbacks use the physical listener; A2A uses the Agent TUN listener.

## Build and test

```bash
cd android
./gradlew :agent-sdk:testDebugUnitTest
./gradlew :example-app:assembleDebug
```

The library expects an Android ARM64 `libmasque_core.so` implementing the JNI
ABI in `NativeMasqueBridge`. This is required because Android's public Kotlin
HTTP APIs do not expose CONNECT-IP HTTP Datagrams. The native socket must call
`AgentVpnService.protectQuicSocket(fd)` before connecting.

The example takes all network values from intent extras. Example ADB launch:

```bash
adb shell am start -n com.rayneo.agent.example/.MainActivity \
  --es runtime_ip 192.168.3.10 \
  --ei runtime_port 8080 \
  --es local_vlan_ip 192.168.1.10 \
  --ei tcp_port 4001 \
  --ei udp_port 28443 \
  --es agent_tun_cidr 8.8.8.7/24 \
  --es agent_id 'did:example:agent-a' \
  --es agent_name 'Agent A' \
  --es masque_token 'replace-with-device-secret' \
  --es masque_url https://192.168.3.10:4433 \
  --es masque_server_name masque.lab.example
```

The included demo proof verifier is not suitable for production.

The bundled callback/A2A listener is HTTP/1.1 inside the CONNECT-IP path and
validates signed messages. Once the deployment's callback mTLS credential format
is fixed, replace `LocalServer`/`PeerMessenger` with the corresponding TLS
implementations; the control-plane Runtime client and MASQUE connection already
use TLS.

Camera/WebRTC calls use the `MediaOffloadAdapter` SPI. The application supplies
an adapter backed by its chosen Android WebRTC distribution; this repository's
unit tests use a deterministic fake so no camera or emulator is required.
