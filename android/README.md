# Android / RayNeoOS Agent SDK

The Android library mirrors the Python SDK's group-cache and endpoint rules:

- `AgentVpnService` creates the Agent TUN without root.
- `acf_group_config` is decoded into an immutable snapshot keyed by
  `group_id + agent_id`.
- A2A TCP always uses cached `agent_ip + tcp_port`.
- Group changes rebuild VPN routes and atomically swap the TUN fd in the native
  MASQUE core.
- Runtime downlink uses a client WebSocket; A2A uses the Agent TUN HTTP listener.

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

This internal-test build keeps TLS 1.3 encryption but does not verify the MASQUE
server certificate chain, validity, or name. The native core logs an explicit
warning on every connection. On first connection it creates an Ed25519 client
certificate and private key under the app's `noBackupFilesDir/agent-sdk/tls`;
the directory uses `0700` and key files use `0600`. Applications never pass
certificate or key parameters to `initialize`.

On the first `initialize`, the SDK also creates a separate P-256 message-signing
key in Android Keystore under alias `agent-sdk-device-signing-v1`. The private
key is non-exportable. Identity registration automatically sends its Base64
SubjectPublicKeyInfo public key; control-plane and A2A messages are signed by
the SDK. The AAR embeds the pinned core-network P-256 public key and uses it to
verify `acf_group_config.proof`; peer A2A messages are verified with the
`did_key` from that verified group snapshot. Applications do not supply proof
verifiers, authenticators, signers, public keys, or production private keys.
The explicit lab-only capability issuer import described below is the sole
exception.

Only the MASQUE URL uses HTTPS/HTTP/3. Health checks, endpoint registration and
all other AgentRuntime calls use HTTP. During `initialize`, the SDK opens
`/v1/acn/downlink-websocket` on the same AgentRuntime host and port used by the
uplink REST calls; no additional port is configured. A2A continues to use HTTP.
The AAR manifest enables cleartext traffic for this internal deployment.

## Test-only capability VC issuance

`registerCapabilities` accepts either pre-issued VCs, raw capability strings,
or both. Existing VCs remain the production path. With raw capabilities, the
SDK creates one `CapabilityCredential` per string, signs the same seven IDM VC
fields as the Python SDK with P-256 ECDSA/SHA-256, and appends the result to the
existing wire-level `vc_list`. AgentRuntime and the network-side HTTP contract
do not change.

Android cannot read the build host's `~/lpx/cert` at runtime. For this lab-only
case, import the test issuer key once from an application resource; the SDK
persists it under the app's `noBackupFilesDir/agent-sdk/test-capability-vc`:

```kotlin
val sdk = AgentSdk.create(vpnService)
resources.openRawResource(R.raw.test_third_party_private_key).use { input ->
    sdk.importTestCapabilityIssuerPrivateKey(input.readBytes())
}

sdk.registerCapabilities(
    agentId = profile.agentId,
    priority = 1,
    credentials = listOf(profile.identityVc),
    capabilities = listOf("robot-control", "voice"),
)
```

For the repository example, provision the ignored test resource before building:

```bash
mkdir -p android/example-app/src/main/res/raw
cp ~/lpx/cert/third-party/private-key.pem \
  android/example-app/src/main/res/raw/test_third_party_private_key.pem
```

The key is excluded from Git and the Agent SDK AAR. It is included in the local
example APK when this test resource is present, so this flow must not be used in
production. Production applications should publish VCs issued by an external
capability authority through `credentials`.

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
  --es test_capabilities 'robot-control,voice' \
  --ei priority 1 \
  --es masque_token 'replace-with-device-secret' \
  --es masque_url https://192.168.3.10:4433
```

The application does not provide an Agent TUN IP. During `initialize`, the SDK
calls `POST /sdk/v1/endpoints`; AgentRuntime returns `ue_ip` and
`ue_prefix_length`, and the SDK validates that assignment before creating the
VPN TUN. The returned CIDR is available as `SdkInitResult.agentTunCidr`.

Core-network downlink frames use `kind + request_id + message_type +
transaction_id + payload`. Each frame is handled in its own coroutine, so
responses may be returned out of order and are correlated only by `request_id`.
The local HTTP/1.1 listener now exposes only `/A2A/message` inside the CONNECT-IP
path; the former Runtime callback paths are not available.

Camera/WebRTC calls use the `MediaOffloadAdapter` SPI. The application supplies
an adapter backed by its chosen Android WebRTC distribution; this repository's
unit tests use a deterministic fake so no camera or emulator is required.
