# Android / RayNeoOS Agent SDK

The Android library mirrors the Python SDK's group-cache and endpoint rules:

- `AgentVpnService` creates the Agent TUN without root.
- `acf_group_config` is decoded into an immutable snapshot keyed by
  `group_id + agent_id`.
- A2A HTTP calls the cached complete `service_endpoints` URL without rewriting
  scheme, authority, port, or path; `agent_ip` is used only for the VPN route.
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

The AAR already packages `libmasque_core.so` for `arm64-v8a`, `armeabi-v7a`,
`x86_64`, and `x86`; applications do not supply a native library. This supports
physical ARM devices as well as x86/x86_64 Android emulators. The core implements
HTTP/3 CONNECT-IP, ADDRESS_ASSIGN and ROUTE_ADVERTISEMENT capsule handling,
bidirectional packet pumps, and TUN fd replacement. It binds the QUIC UDP socket
to the source address selected by Android's route to the MASQUE server, then calls
`AgentVpnService.protectQuicSocket(fd)` before connecting, preventing VPN recursion.
Applications normally omit `localVlanIp`; the SDK performs the route lookup before
establishing its Agent TUN. An explicit `localVlanIp` remains available only as an
advanced override for controlled multi-interface tests.

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
SubjectPublicKeyInfo public key; control-plane requests are signed by the SDK,
while the current A2A contract has no message-level proof. The AAR retains the pinned core-network P-256 public key and verifier
implementation, but this internal interoperability build bypasses inbound
`acf_group_config.proof` verification. Control-plane outbound signing is unchanged.
Applications do not supply proof verifiers, authenticators, signers,
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

Group creation requires a non-blank DNN. The SDK places it in
`group_config.dnn` and includes it in the existing control-request proof:

```kotlin
val group = sdk.createGroup(
    agentId = profile.agentId,
    targetAgentIds = listOf(target.agentId),
    groupName = "patrol-group",
    dnn = "internet",
    maxMembers = 2,
)
```

To rebuild all shipped native libraries after native source changes:

```bash
cd android/native/masque_core
go test ./...
ANDROID_NDK_ROOT=/opt/android-sdk/ndk/27.0.12077973 ./build-android.sh
```

Build only selected ABIs by appending their names, for example
`./build-android.sh arm64-v8a x86_64`. Gradle's `verifyMasqueNativeAbis` task is
attached to `preBuild` and fails the AAR/APK build if any supported ABI is
missing.

## A/B 联调 App

SDK 将 Agent 业务状态按 Runtime `IP:端口` 保存在应用私有
`noBackupFilesDir/agent-sdk/agents`：`NO_IDENTITY`、`IDENTITY_READY`、
`CARD_PUBLISHED`。App 启动后读取 `sdk.agentLifecycleState` 和 `sdk.localProfile`；状态1
才申请身份，状态2才发布 Agent Card，状态3直接复用，不重复调用
`registerCapabilities()`。状态2再次申请身份时会先注销旧身份；状态2/3显式注销成功后
都会删除本地记录并回到状态1。状态3调用 `updateCapabilities()` 时，SDK 根据保存的
身份申请上下文和完整 Agent Card 先注销旧身份、重新申请，再调用
`registerCapabilities()` 发布更新后的完整 Card；成功后从 `sdk.localProfile` 读取新的
`agentId`。

`example-app` 已按 Linux 的 `agent_a_test.py/agent_b_test.py` 流程实现为两页
Android 应用：

- 第 1 页“配置”：选择角色 A 或 B，填写服务器地址、Runtime HTTP 端口、
  MASQUE QUIC 端口与路径、A2A TCP/UDP 端口以及 Agent 测试参数。本机
  Wi-Fi/VLAN IP 由系统自动选择，Token 只保存在当前内存，不写入
  SharedPreferences。
- 第 2 页“日志”：逐步显示 VPN、`GET /v1/ue/info`、CONNECT-IP、身份申请、
  网络能力、Agent Card、发现、建组、群组配置和 A2A 消息结果。失败时停留在
  当前步骤，点击“重试当前接口”即可继续；SDK 不会因单个业务接口失败退出。

角色 B 执行身份申请、获取网络能力、发布用户填写的能力，然后自动接受邀请并
等待消息。角色 A 执行身份申请和能力注册，按同一能力发现 B，携带 DNN 建组，
等待群组配置进入 SDK 缓存后调用 `sendMessage`。两端都不会在流程完成后自动
关闭 TUN、MASQUE 或本地消息服务；必须点击“停止”。

构建并安装：

```bash
cd android
ANDROID_HOME=/opt/android-sdk ./gradlew :example-app:assembleDebug
adb install -r example-app/build/outputs/apk/debug/example-app-debug.apk
```

安装后可以直接在第 1 页填写参数，也可用 intent extras 预填。角色 A 示例：

```bash
adb shell am start -n com.rayneo.agent.example/.MainActivity \
  --es role A \
  --es server_ip '<SERVER_IP>' \
  --ei runtime_port 8088 \
  --ei masque_port 8443 \
  --es masque_path '/.well-known/masque/ip' \
  --ei tcp_port 4001 \
  --ei udp_port 28443 \
  --es owner 'android-test-owner-a' \
  --es agent_name 'Agent-A' \
  --es capability 'text' \
  --es dnn 'internet' \
  --es group_name 'android-ab-test-group' \
  --es message 'hello Agent B from Android A'
```

角色 B 示例：

```bash
adb shell am start -n com.rayneo.agent.example/.MainActivity \
  --es role B \
  --es server_ip '<SERVER_IP>' \
  --ei runtime_port 8089 \
  --ei masque_port 8444 \
  --es masque_path '/.well-known/masque/ip' \
  --ei tcp_port 4001 \
  --ei udp_port 28443 \
  --es owner 'android-test-owner-b' \
  --es agent_name 'Agent-B' \
  --es capability 'text'
```

`server_ip` 和两个服务器端口是部署参数，示例 App 与 SDK 不内置这些地址。
SDK 使用系统路由自动选择能到达 MASQUE Server 的物理源地址，并在日志页显示
最终选择结果。建议先启动 B，等日志显示“Agent B 已就绪”后再启动 A。
如使用授权值，既可在页面填写，也可加 `--es masque_token '<TOKEN>'`。

The application does not provide an Agent TUN IP. `GET /v1/ue/info` must report
`nas.registered=true`, `nas.state=session_ready`, a ready security context, and
one active default IPv4 PDU Session. The SDK uses that session's `ipv4` locally
as `/32`; the address must match the device's `agent_ip + uesimtun` mapping on
the external AgentRuntime/MASQUE/5GC system. That mapping is not configured by
the Android application or this SDK. The effective CIDR is available as
`SdkInitResult.agentTunCidr`.

应用正常初始化时不传物理地址：

```kotlin
sdk.initialize(
    agentRuntimeIp = serverIp,
    agentRuntimePort = runtimePort,
    localTcpPort = 4001,
    localUdpPort = 28443,
    masqueServerUrl = masqueUrl,
)
```

SDK 的 A2A TCP/UDP 服务仅绑定 Agent TUN IP，不监听自动选择的物理源地址。
自动选择结果可从 `SdkInitResult.masqueOuterSourceIp` 读取。只有需要固定特定
网卡出口的多网卡测试才显式传入 `localVlanIp = "..."`。

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
