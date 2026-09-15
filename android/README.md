# Android / RayNeoOS Agent SDK

雷鸟 X3 Pro 专用包的 Windows USB 驱动、ADB、脚本安装、VPN 授权和现场排障步骤见
[《雷鸟 X3 Pro Agent App 安装说明》](../雷鸟眼镜App安装说明.md)。

The Android library mirrors the Python SDK's group-cache and endpoint rules:

- `AgentVpnService` creates the Agent TUN without root.
- `acf_group_config` is decoded into an immutable snapshot keyed by
  `group_id + agent_id`.
- An identical `acf_group_config` replay with the same timestamp is ACKed without
  route writes, generation changes, or an application callback. A notification
  with a newer timestamp is committed and delivered to the application normally.
- A2A HTTP calls the cached complete `service_endpoints` URL without rewriting
  scheme, authority, port, or path; `agent_ip` is used only for the VPN route.
- Group changes rebuild VPN routes and atomically swap the TUN fd in the native
  MASQUE core. Every TUN replacement also recreates the local TCP/UDP ingress
  server after the native packet pump has taken ownership of the new fd. Route-key
  changes whose aggregate destination set is unchanged do not rebuild the TUN.
- Runtime downlink uses a client WebSocket with Ping-based failure detection and bounded
  exponential reconnect; A2A uses the Agent TUN HTTP listener.

## Callback registration APIs

The SDK exposes two independent callback-registration APIs. They are not callback
parameters of `initialize` or `sendMessage`. Register both immediately after creating
`AgentSdk` and before calling `initialize`, so that application handlers are already
available when initialization starts the Runtime downlink WebSocket and local
`/A2A/message` service.

```kotlin
val sdk = AgentSdk.create(vpnService)

val networkRegistration = sdk.registerNetworkMessageListener(
    NetworkMessageListener { messageType, payload ->
        when (messageType) {
            // Returning ACCEPT or REJECT decides whether this Agent accepts the invitation.
            NetworkMessageType.GROUP_INVITATION -> NetworkMessageAction.ACCEPT

            // The SDK has already committed the group cache and routes before this callback.
            NetworkMessageType.GROUP_CONFIG -> NetworkMessageAction.ACK
            NetworkMessageType.UNKNOWN -> NetworkMessageAction.REJECT
        }
    },
)

val groupRegistration = sdk.registerGroupMessageListener(
    GroupMessageListener { groupId, senderAgentId, payload ->
        // Receives the business JSON sent by the peer's sendMessage call.
    },
)

sdk.initialize(/* AgentRuntime, local ports, and MASQUE configuration */)

// When the application no longer needs callbacks:
groupRegistration.close()
networkRegistration.close()
```

`registerNetworkMessageListener` handles both incoming group invitations and committed
group-configuration notifications. It allows one active listener; registering another
before closing the existing handle returns `LISTENER_ALREADY_REGISTERED`. An invitation
received without this listener is rejected. A valid group configuration received without
it is still committed and ACKed, but the application is not notified.

`registerGroupMessageListener` receives peer A2A messages after the SDK validates the
target, group membership, and message structure. If no group listener is registered, the
incoming request fails and the sender does not receive a successful delivery receipt.
Both methods return `AutoCloseable`; calling `close()` unregisters that listener.

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
ES256 JWS profile. Its `valid_from` is one calendar year before the actual
issuance time; `proof.created` remains the issuance time and `valid_until`
continues to be calculated from that issuance time. Pre-issued VCs supplied via
`credentials` are never modified. The SDK also derives the top-level `service_endpoints` from
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
`registerCapabilities()`。状态2再次申请身份时会先注销旧身份；状态2/3调用参数less
`resetAgent()` 时，SDK 只删除本地 Profile/Card 记录并回到状态1，不发送去注册消息、
不修改网侧身份；状态1调用同样不发 HTTP 并幂等成功。存在进行中的算力请求、非终态会话或
C-02配置时，`resetAgent()`和`deregisterIdentity()`都会保留Profile并返回
`AGENT_STATE_INVALID`；应用必须先取消或释放会话，并等待C-05完成本地媒体与路由清理。
状态3调用 `updateCapabilities()` 时直接请求
`POST /arf/v1/agent-cards-update`，成功后身份不变并保持状态3。状态3再次调用
`registerCapabilities()` 表示替换整张 Card：SDK 在本地校验后先注销旧身份、重新申请，
再发布新的完整 Card；成功后从 `sdk.localProfile` 读取可能变化的 `agentId`。

`example-app` 已按 Linux 的 `agent_a_test.py/agent_b_test.py` 流程实现为两页
Android 应用：

- 第 1 页“配置”：选择角色 A 或 B，填写服务器地址、Runtime HTTP 端口、
  MASQUE QUIC 端口与路径、A2A TCP/UDP 端口以及 Agent 测试参数。本机
  Wi-Fi/VLAN IP 由系统自动选择，Token 只保存在当前内存，不写入
  SharedPreferences。
- 第 2 页“日志”：逐步显示 VPN、`GET /v1/ue/info`、CONNECT-IP、身份申请、
  网络能力、Agent Card、发现、建组、群组配置和 A2A 消息结果。群组配置进入
  SDK 缓存后，页面显示“本端 → 对端”路由、消息输入框和发送按钮，A/B 两端均可
  重复手动发送。失败时停留在当前步骤，点击“重试当前接口”即可继续；SDK 不会因
  单个业务接口或单次消息发送失败退出。
- Generic 构建的运行页包含“SDK FEATURE LAB”面板：可读取群组快照，以外部 VC
  增加或删除 Agent Card 技能，查询/取消/释放算力会话；角色 B 可暂停和恢复本机
  摄像头上传，角色 A 在 consumer 媒体连接建立后可写入/读取持续识别目标，并创建/
  查询 Sandbox 控制动作。按钮按角色和实际会话状态启用，取消或释放后先等待 C-05，
  再恢复“申请算力会话”入口。

能力新增要求在面板中粘贴与技能匹配、包含非空 `id` 的完整 VC JSON；App 将该 ID
作为 `reference_vc_id` 调用 `updateCapabilities()`。能力删除不需要 VC。测试面板不
生成或伪造生产能力凭证。

角色 B 执行身份申请、获取网络能力、发布用户填写的能力，然后自动接受邀请并
等待群组配置。角色 A 执行身份申请和能力注册，按同一能力发现 B，携带 DNN 建组。
双方收到群组配置后，App 从 SDK 群组缓存选择唯一对端；用户点击“发送消息”时才
调用 `sendMessage`，不再由 A 自动发送首条消息。发送目标的 Agent ID、IP、端口和
`/A2A/message` URL 均由 SDK 解析，页面不要求用户填写。两端都不会在流程完成后自动
关闭 TUN、MASQUE 或本地消息服务；必须点击“停止”。

构建并安装：

```bash
cd android
ANDROID_HOME=/opt/android-sdk ./gradlew :example-app:assembleGenericDebug
adb install -r example-app/build/outputs/apk/generic/debug/example-app-generic-debug.apk
```

### 雷鸟 X3 Pro 发起方专用包

`rayneo` 构建面向眼镜端低操作场景，固定为发起方 A，并预置服务器
`101.245.78.174`、Runtime HTTP `8088`、MASQUE QUIC/UDP `8443` 和
`/.well-known/masque/ip`。页面不显示角色切换、服务器表单或消息输入框；应用先进入
可操作的双目页面，用户单击“启用 Agent 网络”后才开始连接和 Agent A 全流程。首次
运行会进入 Android 系统“网络连接请求”，确认一次后系统会保留本 App 的 VPN/TUN
授权。MASQUE 外层本机地址仍由 Android 路由自动选择；密钥初始化、HTTP、TUN 和
同步 JNI CONNECT-IP 握手均在后台线程执行，不阻塞眼镜 UI。

雷鸟 X 系列把左右两块物理屏组合成一块逻辑屏，普通单份 Android UI 会被左右眼各
显示一半。专用包按[雷鸟官方 Android 开发手册](https://rayneo.gitbook.io/rayneo-devdoc/x-xi-lie/android-kai-fa)
接入 Mercury ARDK v0.2.6：`RayNeoApplication` 初始化 `MercurySDK`，入口继承
`BaseMirrorActivity`，同一 ViewBinding 自动生成左右两份并同步更新。Manifest 带有
`com.rayneo.mercury.app=true`，因此应用可以出现在眼镜 Launcher。官方 AAR 已放入
`example-app/libs`，SHA-256 为
`5d408e2c5d80e8ae746c42abbda50012b50617005adfeb397661bec9c9be2676`。

眼镜操作遵循系统约定：前后滑或上下滑切换“主要操作/停止”焦点，单击确认，双击停止
并退出。建组和群组配置完成后，主要操作自动变成“发送测试消息”；每次单击向 Agent B
发送一条带序号的预置消息，不需要在眼镜上调用软键盘。

```bash
cd android
ANDROID_HOME=/opt/android-sdk ./gradlew :example-app:assembleRayneoDebug
adb install -r example-app/build/outputs/apk/rayneo/debug/example-app-rayneo-debug.apk
```

专用包应用 ID 为 `com.rayneo.agent.example.rayneo`，可与通用 A/B 联调包并存。
真实用户首次使用时应在眼镜中主动单击“启用 Agent 网络”并确认系统 VPN 授权，正式
部署不能依赖 ADB 绕过 Android 的首次同意。内部联调或受管设备可使用 Windows 脚本
一次完成安装、可选 VPN 预授权和启动：

```powershell
# 正常用户授权流程：启动后在眼镜中单击并确认系统请求
powershell -ExecutionPolicy Bypass -File .\install-rayneo-windows.ps1

# 仅内部联调/受管设备：ADB 预授权，启动后单击即直接连接
powershell -ExecutionPolicy Bypass -File .\install-rayneo-windows.ps1 -PreAuthorizeVpn
```

脚本默认使用 `C:\Android\platform-tools\adb.exe` 和本仓库构建出的 RayNeo Debug APK；
其他位置可通过 `-AdbPath`、`-ApkPath` 指定。VPN 授权通常在关闭 App、设备重启和
同包名 `install -r` 更新后继续保留；卸载、清除数据、用户撤销授权或授权另一 VPN
应用后，需要再次确认。

如服务器临时要求 MASQUE Token，可通过 ADB 启动参数传入；其他部署参数在该专用包中
保持锁定：

```bash
adb shell am start -n com.rayneo.agent.example.rayneo/.RayNeoMainActivity \
  --es masque_token '<TOKEN>'
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
  --es capability 'text' \
  --es message 'hello Agent A from Android B'
```

`server_ip` 和两个服务器端口是部署参数，示例 App 与 SDK 不内置这些地址。
SDK 使用系统路由自动选择能到达 MASQUE Server 的物理源地址，并在日志页显示
最终选择结果。建议先启动 B，等日志显示“Agent B 已就绪”后再启动 A。
等两端日志页均出现“群组已就绪 · 可双向发送”和手动发送区后，即可分别输入文本并
点击“发送消息”；接收方日志会显示 `A2A RECEIVE`，发送方日志会显示 `A2A SEND`
及投递回执。
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

应用需要把 Agent 控制回“未申请数字身份”的状态1时调用：

```kotlin
val result = sdk.resetAgent()
check(result.success)
check(sdk.agentLifecycleState == AgentLifecycleState.NO_IDENTITY)
```

若当前存在算力会话，先结束会话并等待SDK处理C-05：

```kotlin
sdk.releaseComputingSession(releaseRequest)
sdk.awaitComputingSessionClosed(computeServiceSessionId, timeoutSeconds = 30.0)
val result = sdk.resetAgent()
```

Generic 与 RayNeo 示例 App 的运行页均提供“重置到状态1”。为防止触控或镜腿误触，
Reset 需要连续确认两次；执行期间先释放活动算力会话并等待C-05，再清除本地状态，避免
尚未结束的算力配置在 Reset 后重新写入状态。成功后停止自动流程并关闭当前链路，不会向
AgentRuntime 发送去注册请求，也不会自动重新申请身份；本地持久化删除失败时保留原状态。
Generic 与 RayNeo App 的“停止”按钮也先释放活动算力会话并等待C-05，再以
`reason=normal`调用`deregisterIdentity`；任一步失败都会保留Profile并记录日志。

Generic 与 RayNeo 运行页也提供 `Dump 日志`。它会生成一个可分享的文本诊断包，包含
完整 App 流程日志、当前 SDK/群组端点、设备版本、网络接口与 Android 路由、进程可见的
`/proc/self/net` socket 表、本地 A2A TCP 自检、按关键标签过滤的 App/SDK logcat，以及
进程通用日志尾部。MASQUE Token
不会写入文件。RayNeo 页面只保留少量可见日志，但 Dump 独立保留最近 2000 条流程记录。
Android 10 及以上还会把文件写入公共 `Download/AgentLinkDiagnostics`；设备没有分享 App
时可直接通过 USB 文件传输或 ADB 取回。

Core-network downlink frames use `kind + request_id + message_type +
transaction_id + payload`. Each frame is handled in its own coroutine, so
responses may be returned out of order and are correlated only by `request_id`.
Invitation acceptance and group-configuration acknowledgement copy the
downlink `payload.group_info.group_id` and `payload.group_id`, respectively,
into the response payload alongside `result`, using `ACCEPT` and `ACK`.
After the initial Upgrade succeeds, Android sends a WebSocket Ping every 20 seconds and
reconnects unexpected failures/closures with delays capped at 30 seconds. Calling `close()`
cancels pending reconnects. A frame that was never delivered while the socket was disconnected
can only be recovered if AgentRuntime retains or retries it; client reconnect alone cannot replay
an unseen invitation.
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

## Computing session APIs

CREATE, QUERY, CANCEL, and RELEASE all use the AgentRuntime configured by
`initialize()`:

```text
POST http://{agentRuntimeIp}:{agentRuntimePort}/v1/computing/session-requests
```

Before each operation, the SDK checks `/v1/acn/status` and `/v1/ue/info` for
NAS readiness, an active IPv4 PDU session, and an
`HTTP3_CONNECT_IP + EXACT_PDU_SESSION_ID` access. CREATE also requires the
referenced local group to be `ACTIVE`, the requester to match the local Agent,
and the target Agent to be a member.

```kotlin
val createStatus = sdk.createComputingSession(
    ComputeSessionRequest(
        messageType = "COMPUTE_SESSION_REQUEST",
        requestType = ComputeRequestType.CREATE,
        inputFormat = ComputeInputFormat.STRUCTURED,
        requestId = "create-glasses-001",
        acnContext = AcnContext(
            groupId = group.groupId,
            requesterAgentId = profile.agentId,
            targetAgentId = peer.agentId,
        ),
        constraints = ComputeConstraints(
            capabilityId = "video_rendering",
            resources = ComputeResources(
                cpuMillicores = 2000,
                memoryMib = 4096,
            ),
            dnn = "internet",
            allowBaseQos = true,
        ),
    ),
)
val sessionId = requireNotNull(createStatus.computeServiceSessionId)
```

The synchronous result is `ComputeSessionStatus`. HTTP 202 for CREATE means the
request was accepted for asynchronous processing; it does not contain a Sandbox
address or a media URL. Natural-language CREATE uses
`inputFormat = NATURAL_LANGUAGE` with non-empty `text`; structured CREATE uses
non-empty `constraints`. A retry of the same operation must reuse the same
`requestId` and request body.

The remaining lifecycle calls accept the same model and endpoint:

```kotlin
val queryStatus = sdk.queryComputingSession(
    ComputeSessionRequest(
        messageType = "COMPUTE_SESSION_REQUEST",
        requestType = ComputeRequestType.QUERY,
        inputFormat = ComputeInputFormat.STRUCTURED,
        requestId = "query-001",
        targetRequestId = "create-glasses-001",
    ),
)
val cancelStatus = sdk.cancelComputingSession(
    ComputeSessionRequest(
        messageType = "COMPUTE_SESSION_REQUEST",
        requestType = ComputeRequestType.CANCEL,
        inputFormat = ComputeInputFormat.STRUCTURED,
        requestId = "cancel-001",
        targetRequestId = "create-glasses-001",
    ),
)
val releaseStatus = sdk.releaseComputingSession(
    ComputeSessionRequest(
        messageType = "COMPUTE_SESSION_REQUEST",
        requestType = ComputeRequestType.RELEASE,
        inputFormat = ComputeInputFormat.STRUCTURED,
        requestId = "release-001",
        computeServiceSessionId = sessionId,
    ),
)
```

`initialize()` registers the common WebSocket handler internally. It consumes
`COMPUTE_CONNECT_CONFIG`, `COMPUTE_SESSION_STATUS`, and
`COMPUTE_SESSION_CLOSE`; applications do not register compute listeners. For
C-02, the SDK validates `receiver_agent_id`, PDU Session ID, DNN, S-NSSAI,
`ue_ipv4`, and Runtime data-plane capability. It installs the Sandbox endpoint
route, caches `service_endpoint`, `binding_ref`, role, and interface paths,
then automatically returns C-03. C-04 is deduplicated by `status_revision`.
C-05 closes the SDK-owned media objects and route, then automatically returns
C-06. C-02 has no `group_id`, so the SDK trusts the CA group authorization and
revalidates only local identity and network binding.

Media calls accept only `computeServiceSessionId`. They wait for C-02 and build
the internal URL from its cached `service_endpoint + media_connections_path`.
The requester is the consumer:

```kotlin
val stream = sdk.getProcessedVideoStream(sessionId, timeoutSeconds = 120.0)
val track = stream.track
// 页面不再使用视频时：
stream.close()
```

The CREATE target is the producer:

```kotlin
val upload = sdk.startVideoUpload(
    computeServiceSessionId = sessionId,
    cameraId = "0",
    width = 640,
    height = 480,
    fps = 30,
    bitrateKbps = 2400,
    timeoutSeconds = 120.0,
)
```

SDK 模块内置 libwebrtc 媒体适配器，并通过 Gradle 声明
`io.github.webrtc-sdk:android:150.7871.01` 传递依赖。应用不传媒体 adapter、Sandbox URL、端口、SDP、
`binding_ref` 或 `media_connection_id`。两端都主动生成完整非 Trickle ICE Offer：
producer 为 `sendonly`，consumer 为 `recvonly`。SDK 等待 ICE 收集完成后 POST C-02
媒体集合路径，校验 HTTP 201 的 `request_id/computing_context` 回显和 Answer，再设置
远端描述。Android WebRTC 工厂关闭 libwebrtc 的 Android 网络监视器，使未受保护的
ICE socket 继续由非旁路 `AgentVpnService` 根据 Sandbox 路由送入 CONNECT-IP；SDK
只发布地址与 C-02 `ue_ipv4` 一致的候选，并由核心层再次检查。Answer 中的
IPv4 候选在应用前加入 CONNECT-IP 路由。`upload.stop()`、`stream.close()`、C-05 和
`sdk.close()` 负责本地关闭，主动关闭同时发送
`DELETE /v1/media-connections/{media_connection_id}` 并要求 HTTP 204。

consumer 通过同一份 C-02 配置更新或读取持续识别目标：

```kotlin
val target = sdk.updateRecognitionTarget(
    computeServiceSessionId = sessionId,
    requestId = "recognition-001",
    text = "寻找红色玩偶",
    language = "zh",
)
check(target.status == "APPLIED")
println("${target.targetRevision}: ${target.target.label}")

val current = sdk.getRecognitionTarget(sessionId)
```

SDK 内部展开 `recognition_target_path_template`，自动加入完整
`computing_context`，并校验 HTTP 200、上下文回显、`target_revision` 和目标内容。
应用重试同一更新时复用原 `requestId` 和原文本。

运行期动作使用 Sandbox 的固定控制资源；SDK 自动注入 consumer 上下文并通过同一个
UE IPv4 发送：

```kotlin
val action = sdk.createControlAction(
    sessionId,
    ControlActionRequest(
        requestId = "control-search-001",
        inputType = ControlInputType.TEXT,
        text = "寻找杯子",
        language = "zh",
    ),
)
val current = sdk.getControlAction(sessionId, action.actionId)
```

`createControlAction` 固定调用 `POST /v1/control-actions` 并要求 HTTP 202；
`getControlAction` 调用 `GET /v1/control-actions/{action_id}` 并要求 HTTP 200。
Sandbox 到 producer Runtime 的动作转发由 Runtime 内部处理，不新增应用回调。

pruned_sandbox 的文字意图接口同样不依赖 C-02 或算力会话。SDK 接受完整 URL，并把内部
分类名和参数归一化为园区业务意图与槽位：

```kotlin
val result = sdk.recognizeIntent(
    intentUrl = "http://intent.example:8011/api/v1/intent",
    text = "派机器狗巡逻A区域",
)
check(result.intent == "security patrol")
println(result.area) // A
```

当前 pruned_sandbox 的直接响应使用 `intent=patrol`、`argument=A区域`，而 9004 ASR 返回的
嵌套公共格式使用 `intent=security patrol`、`area=A`；`recognizeIntent` 兼容两种形式，并
统一返回公共名称。pruned_sandbox 默认让 8011 只监听容器内的 `127.0.0.1`，Android 实机
使用前必须由部署方提供可达的反向代理 URL，或把该服务改为可控网络内的外部监听地址。

意图驱动发现时，App 将 `security patrol` 映射为 Agent Card 已发布的 `dog-vision` skill，
并把原始文本、意图和 `area` 写入 `task_description`。当前 H-DISCOVERY 线协议包含
`request_id`、`agent_id`、`task_description`、`required_skills`、`discovery_scope`、
`max_results`、`timestamp` 和 `proof`，SDK 会补齐所有必填控制字段。协议仍缺少两类业务关联：
结构化的 `intent/slots` 字段，以及连接意图、发现、建组和算力会话的 `task_id`。在网侧协议
扩展前，槽位只能编码到 `task_description`，skill 映射由应用显式维护。

pruned_sandbox 的独立 ASR 服务使用 9004 端口，不依赖 C-02 或算力会话，因此可以在
`AgentSdk.create()` 之后、`initialize()` 之前调用：

```kotlin
val transcription = sdk.transcribeAudio(
    asrUrl = "http://101.245.78.174:9004/api/v1/transcribe",
    request = AudioTranscriptionRequest(
        audio = recordedBytes,
        fileName = "speech.m4a",
        contentType = "audio/mp4",
        sessionId = "demo-room",
        taskId = "task-001",
        source = "glasses",
        language = "zh",
    ),
)
println(transcription.text)
```

`transcribeAudio` 使用 `multipart/form-data` 上传 `file/session_id/task_id/source`，并返回
转写文本、语言概率、分段时间和处理耗时。调用方传入完整 ASR URL；SDK 不从 C-02 推导
9004 地址。支持 `wav/mp3/m4a/flac/ogg/webm`。

consumer 已收到 C-02 后，可以把录音直接创建为运行期控制动作：

```kotlin
val voiceAction = sdk.createAudioControlAction(
    computeServiceSessionId = sessionId,
    request = AudioControlActionRequest(
        requestId = "voice-action-001",
        audio = recordedBytes,
        fileName = "speech.m4a",
        contentType = "audio/mp4",
        language = "zh",
    ),
)
println(voiceAction.transcription?.text)
```

该接口使用 C-02 的 `service_endpoint` 调用 `POST /v1/audio-control-actions`，自动携带
`computing_context`，要求 HTTP 202，并把响应中的 `transcription` 与标准控制动作状态一起
返回。后续仍使用 `getControlAction` 查询动作执行结果。

If the requester tells the producer to start, the application sends only
`compute_service_session_id` through the existing `sendMessage` API. It does
not send a Sandbox address, port, URL, binding, or credential. The producer SDK
receives its full endpoint configuration through its own C-02.

Computing HTTP calls always reuse the `agentRuntimeIp/agentRuntimePort` passed
to `initialize`; the SDK no longer exposes a separate compute-control target.

### N6 / DN Mock 算力视频联调

仓库的 [`mock-video-server`](../mock-video-server/README.md) 已部署到 free6GC 的
`compose_n6`，默认地址 `172.30.0.10:28500`。该地址不再由 App 配置。算网控制面需要在
C-02 中把 `service_endpoint` 下发为该地址，并把 `media_connections_path` 下发为
`/v1/media-connections`；SDK 据此安装路由并发起 Sandbox 信令。

联调顺序：

1. 启动 A（手机或 RayNeo）和 Generic App 角色 B，等双方日志显示群组已就绪。
2. 在 A 点击“申请算力会话并接收视频”；RayNeo 上使用主操作按钮。A 调用
   `createComputingSession`，收到 HTTP 202 后只把 `compute_service_session_id` 发给 B，
   随后等待自己的 consumer C-02 并调用 `getProcessedVideoStream(sessionId)`。
3. B 收到会话 ID 后自动进入上传流程；首次使用只需确认 Android 摄像头权限。B 等待
   自己的 producer C-02，然后调用 `startVideoUpload(sessionId)`。用户不填写或处理
   Sandbox IP、端口、URL、角色、`binding_ref` 或 SDP。
4. A 可以在 B 首帧到达前完成 consumer WebRTC 并先看到占位流。出现 `VIDEO STREAM`
   和 `VIDEO FRAME frames=1` 后，预览窗显示处理后画面；右上角 `LIVE` 来自实际绘制首帧回调。
5. 在 DN 查看 `curl http://172.30.0.10:28500/debug/v1/sessions`，可以按 consumer
   核对 `frames_processed`、`packets_sent`、`bytes_sent`、`codec`、首帧状态和
   `keyframes_requested`。停止 App 时，A 先调用 RELEASE，双方媒体连接由 SDK 清理。
6. 可直接运行 `python3 mock-video-server/smoke_client.py`，验证正式 U-MEDIA 的
   consumer 先建连、producer 后建连、原 Track 切换处理帧以及 DELETE 清理。
