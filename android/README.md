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
ANDROID_HOME=/opt/android-sdk ./gradlew :agent-sdk:testDebugUnitTest
ANDROID_HOME=/opt/android-sdk ./gradlew :agent-sdk:assembleRelease
ANDROID_HOME=/opt/android-sdk ./gradlew :example-app:assembleDebug
```

The AAR already packages the Android ARM64 `libmasque_core.so`; applications do
not supply a native library. The core implements HTTP/3 CONNECT-IP, ADDRESS_ASSIGN
and ROUTE_ADVERTISEMENT capsule handling, bidirectional packet pumps, and TUN fd
replacement. It binds the QUIC UDP socket to `localVlanIp` and calls
`AgentVpnService.protectQuicSocket(fd)` before connecting, preventing VPN recursion.

The server root CA and TLS name `masque.agent.internal` are compiled into the
native core. On first connection it creates an Ed25519 client certificate and
private key under the app's `noBackupFilesDir/agent-sdk/tls`; the directory uses
`0700` and key files use `0600`. Applications never pass certificate or key
parameters to `initialize`.

To rebuild the shipped ARM64 library after native source changes:

```bash
cd android/native/masque_core
ANDROID_NDK_ROOT=/opt/android-sdk/ndk/27.0.12077973 ./build-android-arm64.sh
go test ./...
```

The example takes all network values from intent extras. Example ADB launch:

```bash
adb shell am start -n com.rayneo.agent.example/.MainActivity \
  --es runtime_ip 192.168.3.10 \
  --ei runtime_port 8080 \
  --es local_vlan_ip 192.168.1.10 \
  --ei tcp_port 4001 \
  --ei udp_port 28443 \
  --es agent_id 'did:example:agent-a' \
  --es agent_name 'Agent A' \
  --es masque_token 'replace-with-device-secret' \
  --es masque_url https://192.168.3.10:4433
```

The application does not provide an Agent TUN IP. During `initialize`, the SDK
calls `POST /sdk/v1/endpoints`; AgentRuntime returns `ue_ip` and
`ue_prefix_length`, and the SDK validates that assignment before creating the
VPN TUN. The returned CIDR is available as `SdkInitResult.agentTunCidr`.

The included demo proof verifier is not suitable for production.

The bundled callback/A2A listener is HTTP/1.1 inside the CONNECT-IP path and
validates signed messages. Once the deployment's callback mTLS credential format
is fixed, replace `LocalServer`/`PeerMessenger` with the corresponding TLS
implementations; the control-plane Runtime client and MASQUE connection already
use TLS.

Camera/WebRTC calls use the `MediaOffloadAdapter` SPI. The application supplies
an adapter backed by its chosen Android WebRTC distribution; this repository's
unit tests use a deterministic fake so no camera or emulator is required.
