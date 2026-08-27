# Android / RayNeoOS Agent SDK

The Android library mirrors the Python SDK's group-cache and endpoint rules:

- `AgentVpnService` creates the Agent TUN without root.
- `acf_group_config` is decoded into an immutable snapshot keyed by
  `group_id + agent_id`.
- A2A HTTP uses cached `service_endpoints` for scheme/port/path and the verified
  `agent_ip` as the routed destination; applications never pass an endpoint.
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

This repository ships only the endpoint SDK and CONNECT-IP client. It does not
ship a MASQUE server, AgentRuntime server, UERANSIM adapter, server certificate,
or server startup/configuration command. The deployment team supplies the
external MASQUE URL and optional authorization value.

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
the SDK. The AAR retains the pinned core-network P-256 public key and verifier
implementation, but this internal interoperability build bypasses inbound
`acf_group_config.proof` and peer A2A `proof` verification. Outbound signing is
unchanged. Applications do not supply proof verifiers, authenticators, signers,
public keys, or production private keys. This profile must not be used in
production.
The explicit lab-only capability issuer import described below is the sole
exception.

Proof wire fields remain `verification_method` and `proof_purpose`. Detached
JWS signs `SHA-256(canonical(proof without jws)) ||
SHA-256(canonical(business document without proof))`, with the proof digest
first. Each digest is 32 bytes. HTTP `request_id` remains outside the signed
business document. Python and Android use the same recursively key-sorted,
compact UTF-8 JSON canonicalization and the same 64-byte cross-platform test
vector.

Only the MASQUE URL uses HTTPS/HTTP/3. Later AgentRuntime uplink control calls
use HTTP. `initialize` does not call a health-check or endpoint-registration
API and does not upload the local IP, ports, or TUN address. It first sends a
bodyless `GET /v1/ue/info`, selects the single active default IPv4 PDU Session,
and configures its `ipv4` as the Agent TUN address with a `/32` prefix. It then
opens `/v1/acn/downlink-websocket` on the same host and port; no additional port
is configured. A2A continues to use HTTP. The AAR manifest enables cleartext
traffic for this internal deployment.

## Test-only capability VC issuance

`registerCapabilities` accepts either pre-issued VCs, raw capability strings,
or both. Existing VCs remain the production path. With raw capabilities, the
SDK creates one `AgentCapabilityCredential` per string, stores the value as
`claims.skill_name`, and signs it with the ACN JsonWebSignature2020 detached
ES256 JWS profile. The SDK also derives the top-level `service_endpoints` from
the Agent TUN IP, local TCP port, and `/A2A/message`; applications do not pass
an address or URL.

For this closed lab profile, both the third-party public and private test keys
are packaged in the AAR under `certs/third-party-capability-*-key.pem`. They are
loaded as classpath-relative SDK resources; `AgentSdk.create` automatically
copies the private test key into app-private storage. Applications do not
configure or import a key path. This deliberately shared private key makes this
profile unsuitable for production.

```kotlin
val sdk = AgentSdk.create(vpnService)
sdk.registerCapabilities(
    agentId = profile.agentId,
    priority = 1,
    credentials = listOf(profile.identityVc),
    capabilities = listOf("robot-control", "voice"),
)
```

Production applications should publish VCs issued by an external capability
authority through `credentials` and must use a build without this lab key.

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

The application does not provide an Agent TUN IP. `GET /v1/ue/info` must report
`nas.registered=true`, `nas.state=session_ready`, a ready security context, and
one active default IPv4 PDU Session. The SDK uses that session's `ipv4` locally
as `/32`; the address must match the device's `agent_ip + uesimtun` mapping on
the external AgentRuntime/MASQUE/5GC system. That mapping is not configured by
the Android application or this SDK. The effective CIDR is available as
`SdkInitResult.agentTunCidr`.

Core-network downlink frames use `kind + request_id + message_type +
transaction_id + payload`. Each frame is handled in its own coroutine, so
responses may be returned out of order and are correlated only by `request_id`.
The local HTTP/1.1 listener now exposes only `/A2A/message` inside the CONNECT-IP
path; the former Runtime callback paths are not available.

Control-plane writes carry an SDK-generated plain UUID `request_id`. Identity
application uses the `ACN-H-ID-v1` domain, ordered LP16/U64BE fields, and one
LP16 containing the exact compact UTF-8 `metadata` JSON sent over HTTP. Required
metadata keys are ordered as `region/os/version`; additional string-valued keys
are sorted by name. The remaining control requests use `proof`. Applications
must provide non-empty identity `description` and `metadata.region/os/version`. Group configuration
downlink uses exactly `ACN_AGENT_GROUPING_NOTIFICATION`.

A2A calls remain address-free at the application boundary:

```kotlin
sdk.sendMessage(
    groupId = group.groupId,
    targetAgentId = peer.agentId,
    jsonMessage = buildJsonObject { put("command", "patrol") },
    messageType = "control",
    taskId = "task-patrol",
)
```

The wire body contains `src_agent_id`, `dst_agent_id`, `type`, `task_id`, and
`payload`; the receiver returns `{"status":"OK"}` after validation.

Camera/WebRTC calls use the `MediaOffloadAdapter` SPI. The application supplies
an adapter backed by its chosen Android WebRTC distribution; this repository's
unit tests use a deterministic fake so no camera or emulator is required.
