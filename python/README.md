# Agent Connect SDK（Linux/Python）客户使用指南

`agent-connect-sdk` 为 Linux 端 Agent 提供统一的控制面和数据面能力：应用只需要调用 SDK 函数，不需要感知对端 IP、TCP 端口、TUN 路由或 MASQUE 封装细节。

SDK 收到 AgentRuntime 通过 `ACN_AGENT_GROUPING_NOTIFICATION` 透传的 `acf_group_config` 后，会自动缓存 `group_id + agent_id -> agent_ip + tcp_port + udp_port`，并自动维护对端 `/32` 或 `/128` 主机路由。应用发送时提供群组、目标 Agent、消息类型、任务 ID 和业务 JSON，不传 IP、端口或路由。

## 1. 交付物和运行要求

建议向客户交付：

- `agent_connect_sdk-0.14.0-py3-none-any.whl`：只包含端侧 Client 的 SDK wheel。
- `examples/full_flow_demo.py`：不依赖真实网络的安装和全流程自检。
- `examples/linux_agent.py`：连接真实 AgentRuntime、TUN 和 MASQUE Proxy 的端侧常驻示例。
- `examples/masque_two_instance_test.py`：在两个隔离的 Ubuntu 实例中验证 A 经 MASQUE/5GC 向 B 发送消息，B 在控制台和本地文件记录收包证据。

本仓库不交付 MASQUE Server、AgentRuntime、UERANSIM 适配器、服务器证书或服务器
启动命令。服务器侧如何解封装、选择 UE 和接入 5GC 由外部系统负责。

运行环境：

- Linux x86_64 或 aarch64，Python 3.10 及以上。
- 端侧具备 `/dev/net/tun`，进程具有 root 或 `CAP_NET_ADMIN` 权限。
- 外部系统已经提供可用的 MASQUE CONNECT-IP 地址和必要的鉴权信息。
- 端侧可以通过 UDP 访问该地址，示例端口为 `4433`。
- 端侧可以通过 HTTP 访问 AgentRuntime。

Android/RayNeoOS 使用 AAR 和 `VpnService`，不使用本 wheel，参见仓库 `android/README.md`。

## 2. 构建、检查和安装 wheel

### 2.1 SDK 发布方构建

在源码的 `python` 目录执行：

```bash
python3 -m venv .venv-build
. .venv-build/bin/activate
python -m pip install --upgrade pip build twine
python -m build --wheel
python -m twine check dist/*.whl
```

输出文件为：

```text
dist/agent_connect_sdk-0.14.0-py3-none-any.whl
```

文件名中的发行名使用下划线是 Python wheel 的标准规范；安装和查询时的项目名仍是 `agent-connect-sdk`。

### 2.2 客户安装

客户不需要源码，直接安装 wheel：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ./agent_connect_sdk-0.14.0-py3-none-any.whl
```

确认安装结果：

```bash
python -c 'import agent_sdk; print(agent_sdk.__version__)'
python -m pip show agent-connect-sdk
```

`pip` 会自动安装 `aiohttp`、`aioquic`、`cryptography`、`httpx` 和 `pyroute2` 等依赖。如果客户环境不能访问公网，应同时交付依赖 wheel，并使用：

```bash
python -m pip install --no-index --find-links ./wheelhouse \
  ./agent_connect_sdk-0.14.0-py3-none-any.whl
```

发布方可以这样生成离线依赖目录：

```bash
python -m pip download --dest wheelhouse \
  ./dist/agent_connect_sdk-0.14.0-py3-none-any.whl
```

### 2.3 安装后先跑全流程自检

自检使用真实 `AgentSdk` 核心和内存适配器，不创建系统 TUN，也不连接真实 AgentRuntime、MASQUE 或 UERANSIM：

```bash
agent-sdk-self-check
```

最后出现以下内容即表示 wheel 导入和主要 API 全部可用：

```text
FULL FLOW DEMO PASSED
```

自检日志保存在当前目录的 `logs/agent-sdk-self-check.log`。

该示例依次调用：`init`、`apply_identity`、`get_network_ability`、`register_capabilities`、`update_capabilities`、`discover_agents`、`create_group`、群组通知处理、`send_message`、消息接收、计算/视频卸载和 `deregister_identity`。

## 3. 组网和 MASQUE 配置

下面的地址只是部署示例，均通过配置传入，SDK 源码没有硬编码这些地址。

| 节点 | 物理 IP | Agent/UE IP | UERANSIM 接口 |
|---|---|---|---|
| 设备 A | `192.168.1.10` | `8.8.8.7` | 服务器 `uesimtun0` |
| 设备 B | `192.168.2.10` | `8.8.8.8` | 服务器 `uesimtun1` |
| 服务器 | `192.168.3.10` | — | MASQUE Proxy + UERANSIM |

A 向 B 发送消息时的内层报文是 `8.8.8.7 -> 8.8.8.8`，完整路径为：

```text
设备 A 应用
  -> A 的 agent_tun0
  -> A 的 CONNECT-IP/QUIC 隧道
  -> 服务器 MASQUE Proxy
  -> uesimtun0
  -> 5GC UPF
  -> uesimtun1
  -> B 的 CONNECT-IP/QUIC 隧道
  -> B 的 agent_tun0
  -> 设备 B 的 /A2A/message
```

不需要在端侧或应用层做 SNAT/DNAT。端侧 Agent TUN 使用自己的 UE IP，因此业务包进入隧道时源地址已经是 `8.8.8.7` 或 `8.8.8.8`。

### 3.1 外部服务边界

端侧客户不安装、配置或启动任何服务器程序。本 SDK 只要求部署方提供以下连接信息：

| 外部输入 | 端侧用途 |
|---|---|
| `agent_runtime_ip + agent_runtime_port` | 查询本 UE 信息并建立控制面 WebSocket |
| `masque_server_url` | 建立 HTTP/3 CONNECT-IP 数据隧道；URL 已包含服务端 UDP 端口 |
| `masque_authorization` | 外部服务要求时携带的会话鉴权值 |

MASQUE Proxy、AgentRuntime、UERANSIM、`uesimtun0/1` 和 5GC UPF 都是外部系统。
它们如何部署、如何将 CONNECT-IP 会话映射到 UE、如何配置服务器证书和用户面规则，
不属于 Wheel、AAR 或本仓库的实现范围。SDK 不提供服务器程序、配置模板、证书、
启动命令或 TUN 接入模块。

端到端联调只约定可观察结果：设备 A 的内层包进入外部系统时为
`8.8.8.7 → 8.8.8.8`，外部系统应使它经 UE A/5GC/UE B 后返回设备 B；SDK 不对
服务器内部进程、命名空间或转发实现作假设。

### 3.2 SDK 内：端侧 `init` 配置

设备 A 的初始化示例：

```python
result = await sdk.init(
    agent_runtime_ip="192.168.3.10",
    agent_runtime_port=8080,
    local_vlan_ip="192.168.1.10",
    local_tcp_port=4001,
    local_udp_port=28443,
    masque_server_url="https://192.168.3.10:4433",
    masque_authorization="Bearer replace-with-secret-for-device-a",
    tun_name="agent_tun0",
    tun_mtu=1280,
    log_file_path="/var/log/agent-sdk/agent-a.log",
    log_level="INFO",
    log_max_bytes=10 * 1024 * 1024,
    log_backup_count=5,
)
```

设备 B 只需替换本机值：

- `local_vlan_ip="192.168.2.10"`
- `masque_authorization="Bearer replace-with-secret-for-device-b"`

SDK 初始化时调用
`GET http://<agent_runtime_ip>:<agent_runtime_port>/v1/ue/info`。该请求没有
请求体，只查询 UERANSIM UE 的注册、NAS 和 PDU Session 状态，不上报
本机信息。SDK 要求 `nas.registered=true`、`nas.state=session_ready`、
`nas.security_context=true`，然后选择唯一活动的默认 IPv4 PDU Session，
将其 `ipv4` 作为 Agent TUN IP 并按点到点 TUN 配置为 `/32`。例如
`ipv4="8.8.8.7"` 对应 `result.agent_tun_cidr="8.8.8.7/32"`。外部系统必须为
该地址提供对应的 CONNECT-IP/5GC 路径，但端侧用户不配置这条服务器映射。

不要传对端 IP、端口或路由。SDK 在收到合法的 `acf_group_config` 后自动获得并维护这些信息。

`init` 参数说明：

| 参数 | 必填 | 说明 |
|---|---|---|
| `agent_runtime_ip` | 是 | AgentRuntime 物理网地址 |
| `agent_runtime_port` | 是 | AgentRuntime HTTP 端口 |
| `local_vlan_ip` | 是 | 本设备物理网 IP；用于 Runtime 上行 HTTP 和 MASQUE QUIC 外层 |
| `local_tcp_port` | 是 | 本地 `/A2A/message` TCP 监听端口 |
| `local_udp_port` | 是 | 对外公布的 UDP 业务端口 |
| `masque_server_url` | 是 | MASQUE Proxy 的 HTTPS URL，底层使用 HTTP/3/QUIC |
| `masque_authorization` | 否 | 推荐使用 `Bearer <device-token>` |
| `tun_name` | 否 | 默认 `agent_tun0` |
| `tun_mtu` | 否 | 默认 `1280`，应与 Proxy 配置一致 |
| `log_file_path` | 否 | 本地日志文件；默认 `./logs/agent-sdk.log` |
| `log_level` | 否 | `DEBUG/INFO/WARNING/ERROR/CRITICAL`，默认 `INFO` |
| `log_max_bytes` | 否 | 单个日志文件最大字节数，默认 10 MiB |
| `log_backup_count` | 否 | 轮转历史文件数量，默认 5 |

首次建立 MASQUE 连接时，SDK 自动生成 Ed25519 客户端证书和私钥，后续启动
复用同一身份。Linux 默认保存在
`$XDG_STATE_HOME/agent-sdk/tls/`，未设置 `XDG_STATE_HOME` 时保存在
`~/.local/state/agent-sdk/tls/`；目录权限为 `0700`，证书和私钥权限为
`0600`。应用不传密钥路径，也不应读取或复制该私钥。

除 `masque_server_url` 必须使用 `https://` 以建立 HTTP/3/QUIC 外，SDK
的后续 AgentRuntime 主动上行控制接口均使用 `http://`。`init()`
不调用健康检查或端点注册接口，也不上报本机 IP、端口或 TUN 地址；
它先使用 `GET /v1/ue/info` 查询本 UE 的 PDU IPv4，再在同一 HTTP 服务
端口上建立下行 WebSocket。Agent 之间的 `/A2A/message` 使用 HTTP。

`init()` 还会主动建立以下 WebSocket，使用与上行 REST 完全相同的 Runtime
IP 和端口，不增加新的配置参数：

```text
GET http://<agent_runtime_ip>:<agent_runtime_port>/v1/acn/downlink-websocket
Connection: Upgrade
Upgrade: websocket
```

只有 WebSocket 握手成功，`init()` 才返回。该长连接负责全部核心网主动下行；
端侧不再开放 `/agent/group-invitation` 或 `/agent/group-moq-info` 回调接口。

### 3.3 本地日志

SDK 使用 UTF-8 文本日志，每行包含时间、级别、logger 名称和一个 JSON 事件。默认记录：

- 所有公开 SDK 函数的 `function_enter`、`function_exit` 和 `function_error`，包括参数、返回类型/结果、错误码和耗时。
- SDK 发往 AgentRuntime、对端 Agent 的 HTTP 请求及其响应。
- AgentRuntime WebSocket 的连接、群组通知、邀请和关联响应，以及对端
  `/A2A/message` 的 HTTP 入站请求及响应。
- 端侧 Client 的 HTTP/3 CONNECT-IP 请求、响应状态和协商结果。

查看实时日志：

```bash
tail -F /var/log/agent-sdk/agent-a.log
```

检索 HTTP 失败和函数异常：

```bash
grep -E '"event":"(http_error|function_error)"|"status_code":[45][0-9][0-9]' \
  /var/log/agent-sdk/agent-a.log
```

`authorization`、token、密码、签名、`proof/jws`、公私钥、DID key、VC 和 credentials 会写成 `[REDACTED]`。业务消息结构和非敏感字段保留，便于定位问题。日志目录必须预先允许 SDK 进程写入；无法创建日志文件时，`init()` 返回 `LOG_SETUP_FAILED`。

### 3.4 SDK 内建的消息签名和验签

用户不配置密钥，也不实现安全回调。第一次 `init()` 会生成一套独立于
MASQUE TLS 的 P-256 消息签名密钥：

- 私钥：`$XDG_STATE_HOME/agent-sdk/security/device-private-key.pem`，未设置
  `XDG_STATE_HOME` 时位于 `~/.local/state/agent-sdk/security/`。
- 公钥：同目录 `device-public-key.pem`，身份申请时 SDK 自动转换成 Base64
  DER SubjectPublicKeyInfo，填入 HTTP 请求的 `public_key`。
- 目录权限为 `0700`，两个密钥文件权限为 `0600`；后续启动复用同一密钥。
- 私钥不进入请求、返回值或日志。

签名算法固定为 P-256 ECDSA + SHA-256。身份申请的普通 `signature` 按
`ACN-H-ID-v1\0 + LP16/U64BE` 逐字段编码后签名，ASN.1 DER 再做标准 Base64；
其他控制请求和 A2A 的 `proof.jws` 使用 `ES256`、RFC 7797 `b64=false` 的分离
JWS。SDK 自动生成普通 UUID `request_id`，它只用于 HTTP 幂等关联，不进入
NAS 业务体，也不属于签名覆盖范围。

`proof` 的线上字段名保持 `verification_method` 和 `proof_purpose`。SDK 先将
`proof` 去掉 `jws` 得到 `proofOptions`，再将完整 `proof` 从业务文档移除。两部分
分别按字段名递归排序、无多余空白的紧凑 UTF-8 JSON 规范化，签名数据固定为：

```text
proofHash    = SHA-256(canonical_json(proofOptions))
documentHash = SHA-256(canonical_json(业务文档，不含 proof 和 HTTP request_id))
verifyData   = proofHash || documentHash
JWSInput     = BASE64URL(protectedHeader) || "." || verifyData
```

`proofHash` 和 `documentHash` 各 32 字节，顺序不可交换；最终请求体再附加包含
`jws` 的完整 `proof`。

Wheel 只预置 `/root/lpx/cert/core-network/public-key.pem` 对应的核心网公钥，
SPKI DER SHA-256 指纹为
`86:D4:77:77:67:4E:79:77:88:A9:61:18:8A:C9:B8:A4:CD:34:DF:15:F4:61:FC:1C:E9:BA:89:D2:15:01:6C:CD`。
核心网私钥不会进入 SDK。`acf_group_config` 必须携带
`proof_purpose=assertionMethod` 的有效签名，否则 SDK 拒绝缓存成员和安装路由。
A2A 收包则使用已验签群组配置中的发送方 P-256 `did:key` 验签。

## 4. 应用如何调用 SDK

### 4.1 创建 SDK 和注册监听器

```python
from agent_sdk import AgentSdk, NetworkMessageAction, NetworkMessageType


class NetworkListener:
    async def on_network_message(self, message_type, payload):
        if message_type is NetworkMessageType.GROUP_INVITATION:
            return NetworkMessageAction.ACCEPT
        # GROUP_CONFIG 到达监听器前已经由 SDK 验签、缓存并安装路由。
        return NetworkMessageAction.ACK


class GroupListener:
    async def on_group_message(self, group_id, sender_agent_id, payload):
        print("收到消息", group_id, sender_agent_id, payload)


sdk = AgentSdk()
sdk.register_network_message_listener(NetworkListener())
sdk.register_group_message_listener(GroupListener())
```

应用不传入签名器、验签器或私钥。首次 `init()` 时 SDK 自动生成 P-256
设备密钥并持久化；之后控制面请求和 A2A 消息都复用该私钥签名。核心网下发
的 `acf_group_config.proof` 使用 Wheel 内置的核心网 P-256 公钥验签，对端
A2A 消息使用已验签群组配置中的 `did_key` 验签。

### 4.2 身份、能力、发现和建群

```python
profile = await sdk.apply_identity(
    owner="customer-a",
    name="Agent A",
    description="RayNeo edge agent",
    metadata={"region": "CN", "os": "Linux", "version": "0.14.0"},
)

ability = await sdk.get_network_ability(profile.agent_id)

await sdk.register_capabilities(
    profile.agent_id,
    priority=1,
    credentials=[profile.identity_vc, ability.ability_vc],
)

await sdk.update_capabilities(
    profile.agent_id,
    update_items=[
        {
            "update_type": "add_skill",
            "skill_name": "camera",
            "reference_vc_id": ability.ability_vc["id"],
        }
    ],
    credentials=[ability.ability_vc],
)

agents = await sdk.discover_agents(
    task_id="task-001",
    agent_id=profile.agent_id,
    task_description="寻找支持文字消息的 Agent",
    required_skills=["text"],
    max_results=10,
)

group = await sdk.create_group(
    agent_id=profile.agent_id,
    target_agent_ids=[agents[0].agent_id],
    group_name="customer-demo",
    max_members=2,
)
```

`credentials` 是正式用法：调用方传入已经由运营商或能力认证组织签发的 VC，
SDK 将其原样放入 `vc_list`。封闭实验环境还可直接传能力字符串；SDK 会使用
测试三方机构的 P-256 私钥为每个能力生成一张 `CapabilityCredential`，再追加
到同一个 `vc_list`：

```python
await sdk.register_capabilities(
    profile.agent_id,
    priority=1,
    credentials=[profile.identity_vc],
    capabilities=["robot-control", "voice"],
    # 省略时默认读取 ~/lpx/cert/third-party/private-key.pem
    test_vc_private_key_path="/root/lpx/cert/third-party/private-key.pem",
)
```

生成的测试 VC 使用
`did:thirdpartyissuer@6gc.mnc015.mcc234.3gppnetwork` 作为 `issuer`，签名原文
兼容现有 IDM 测试规则：只覆盖 `context/id/type/issuer/valid_from/valid_until/claims`
七个字段，采用排序紧凑 JSON、P-256 ECDSA/SHA-256 和 DER Base64 签名。
该私钥只从外部文件读取，不会进入 Wheel。此入口仅用于联调；正式环境应由
独立能力认证服务签发 VC，再通过 `credentials` 发布。Android 也支持相同的
能力列表输入，但必须先将测试机构私钥导入应用私有目录，详见 Android 指南。

AgentRuntime 随后通过已建立的 WebSocket 下发核心网请求：

```json
{
  "kind": "request",
  "request_id": "delivery-123",
  "message_type": "ACN_AGENT_GROUPING_INVITATION",
  "transaction_id": 49,
  "payload": {
    "group_config": {"group_name": "task-patrol"},
    "group_administrator": {"agent_id": "a1"}
  }
}
```

SDK 按 `request_id` 并发处理并允许乱序返回。用户 listener 的结果会被封装为
`{"kind":"response","request_id":"delivery-123","payload":{"result":"ACCEPT"}}`。
`transaction_id` 只用于保留原始 NAS 事务语义，不作为 SDK 缓存或路由键。

通知中的 `members` 键名只是标签。SDK 使用成员对象内的 `agent_id` 建立索引，并自动安装对端路由。应用可以等待群组激活：

```python
import asyncio

while await sdk.get_group_snapshot(group.group_id) is None:
    await asyncio.sleep(0.2)
```

### 4.3 发送消息

```python
receipt = await sdk.send_message(
    group_id=group.group_id,
    target_agent_id=agents[0].agent_id,
    json_message={"type": "text", "content": "hello"},
    timeout_seconds=5.0,
    message_type="text",
    task_id="task-001",
)
print(receipt.message_id, receipt.delivered)
```

应用不传 URL、IP 或端口。SDK 使用已缓存的目标 `agent_ip + tcp_port`，固定调用对端：

```text
POST http://<agent_ip>:<tcp_port>/A2A/message
```

这个 HTTP 包由系统路由送入 Agent TUN，再通过 CONNECT-IP 和 5GC U 面到达对端。群组配置不存在、目标不在群组或配置验签失败时，SDK 会拒绝发送，不会回退到用户提供的地址。

### 4.4 计算和视频卸载

应用需要在构造 `AgentSdk` 时提供平台对应的 `MediaOffloadAdapter`，然后调用：

```python
session = await sdk.create_offloading_session(
    profile.agent_id,
    workload_type="video_rendering",
    sandbox_id="sandbox-edge-1",
)

upload = await sdk.start_video_upload(
    session.session_id,
    camera_id=0,
    width=1280,
    height=720,
    fps=30,
    bitrate_kbps=2500,
)

stream = await sdk.get_processed_video_stream(session.session_id)
frame = await stream.recv()
await upload.stop()
```

SDK 只定义媒体适配接口，不替应用选择 `aiortc`、GStreamer 或硬件媒体栈。

### 4.5 关闭

```python
try:
    # 业务逻辑
    ...
finally:
    await sdk.close()
```

仅在确实要废弃身份时调用：

```python
await sdk.deregister_identity(profile.agent_id, reason="retired")
```

普通进程退出不要注销身份；下次启动可从安全存储恢复已验证的 `AgentProfile`，再调用 `set_local_profile_for_restore(profile)`。

## 5. 可直接运行的真实端侧示例

### 5.1 Windows/Ubuntu 双实例 MASQUE 消息测试

`examples/masque_two_instance_test.py` 是独立的透明三层链路验证脚本，不执行
身份申请、Agent 发现、建群，不需要 `agent_id/group_id`，也不等待
`acf_group_config`：

1. A、B 启动时分别请求本实例 AgentRuntime 的 `GET /v1/ue/info`，按与 SDK
   `init()` 相同的规则选择唯一活动默认 IPv4 PDU Session，将其 `ipv4` 配置为
   本机 Agent TUN IP，并在控制台和日志中输出 `UE_INFO_AGENT_TUN_IP`。
2. 两个实例分别连接自己的 AgentRuntime MASQUE/QUIC 端口，并在本机 Agent IP
   的 TCP 4001 端口启动 `POST /message`。
3. 实例 A、B 各自开放一个仅绑定 `127.0.0.1` 的测试控制接口。用户通过
   `POST /test/peer` 告诉每个实例对端 Agent IP；脚本此时才安装对端主机路由。
4. 用户对 A 调用 `POST /test/send`；A 将该请求体作为消息，向
   `http://<B Agent IP>:4001/message` 发送。B 打印并落盘 `MESSAGE_RECEIVED`，
   返回 `{"status":"OK"}`。

该脚本只用于验证 TUN→CONNECT-IP→服务器用户面→对端 TUN 的连通性。正式业务
仍使用 `AgentSdk.send_message(group_id, target_agent_id, ...)` 和
`POST /A2A/message`，不应把验证脚本的直连 IP接口当作 SDK 北向接口。

#### 网络隔离前提

A、B 必须位于不同 Linux 网络命名空间，例如两个网络已配置完成且都能访问服务器
的 WSL Ubuntu 实例，或者两个已配置外部连通性的 `ip netns`。仅在同一个 Ubuntu
中打开两个终端、创建两个 venv 或启动两个普通进程不构成网络隔离：当
`8.8.8.7` 和 `8.8.8.8` 同时是同一内核的本地地址时，A 到 B 可能被本机路由表
直接交付，绕过 MASQUE 和 5GC。

脚本启动时会打印 `/proc/self/ns/net` 的 `netns_id`。请人工比较 A、B 的值；
只有两者不同时，收包结果才能排除同一 Linux 网络命名空间内的本地短路。两个
Python 虚拟环境不同不能替代网络命名空间隔离。

Windows PowerShell 先确认两个 WSL 发行版名称。以下命令假设它们分别为
`Ubuntu-Agent-A` 和 `Ubuntu-Agent-B`，仓库位于 Windows
`C:\work\orange_sdk`（WSL 路径 `/mnt/c/work/orange_sdk`）：

```powershell
wsl.exe --list --verbose
$SdkPythonDir = "/mnt/c/work/orange_sdk/python"
```

如果实际发行版名称或仓库路径不同，请替换后续命令中的对应值。

#### 第一步：先启动 B

下面的地址和端口只是示例。`8082` 是 B 对应的 AgentRuntime HTTP 端口，
`4434` 是 B 对应的 AgentRuntime MASQUE/QUIC 端口；本机 Agent IP 不作为参数传入：

```bash
cd python
sudo -E .venv/bin/python examples/masque_two_instance_test.py \
  --role B \
  --runtime-ip 192.168.3.10 \
  --runtime-port 8082 \
  --local-vlan-ip 192.168.2.10 \
  --masque-url https://192.168.3.10:4434/.well-known/masque/ip \
  --message-port 4001 \
  --control-port 18082
```

从 Windows PowerShell 启动 B：

```powershell
wsl.exe -d Ubuntu-Agent-B --cd $SdkPythonDir -- `
  sudo -E .venv/bin/python examples/masque_two_instance_test.py `
  --role B `
  --runtime-ip 192.168.3.10 `
  --runtime-port 8082 `
  --local-vlan-ip 192.168.2.10 `
  --masque-url https://192.168.3.10:4434/.well-known/masque/ip `
  --message-port 4001 `
  --control-port 18082
```

B 成功查询并连接后会输出本机 Agent TUN IP 和控制地址：

```jsonl
{"role":"B","event":"UE_INFO_AGENT_TUN_IP","method":"GET","url":"http://192.168.3.10:8082/v1/ue/info","agent_tun_ip":"8.8.8.8","agent_tun_cidr":"8.8.8.8/32"}
{"role":"B","event":"MESSAGE_SERVER_LISTENING","url":"http://8.8.8.8:4001/message"}
{"role":"B","event":"INSTANCE_READY","local_agent_ip":"8.8.8.8","control_url":"http://127.0.0.1:18082"}
```

保持 B 进程运行，记下日志中的 `agent_tun_ip=8.8.8.8`。不需要复制或传递任何
Agent ID。

#### 第二步：启动 A

`8081` 和 `4433` 分别替换为 A 对应的 AgentRuntime HTTP 与 MASQUE/QUIC 端口：

```bash
cd python
sudo -E .venv/bin/python examples/masque_two_instance_test.py \
  --role A \
  --runtime-ip 192.168.3.10 \
  --runtime-port 8081 \
  --local-vlan-ip 192.168.1.10 \
  --masque-url https://192.168.3.10:4433/.well-known/masque/ip \
  --message-port 4001 \
  --control-port 18081
```

另开一个 Windows PowerShell 窗口启动 A，并重新设置当前窗口变量：

```powershell
$SdkPythonDir = "/mnt/c/work/orange_sdk/python"
wsl.exe -d Ubuntu-Agent-A --cd $SdkPythonDir -- `
  sudo -E .venv/bin/python examples/masque_two_instance_test.py `
  --role A `
  --runtime-ip 192.168.3.10 `
  --runtime-port 8081 `
  --local-vlan-ip 192.168.1.10 `
  --masque-url https://192.168.3.10:4433/.well-known/masque/ip `
  --message-port 4001 `
  --control-port 18081
```

A 日志应回显 `agent_tun_ip=8.8.8.7`。两个实例都会继续运行，等待下面的 curl。

#### 第三步：用 curl 告诉两个实例对端 Agent IP

在 B 所在 Ubuntu 中执行，告诉 B：A 的 Agent IP 是 `8.8.8.7`：

```bash
curl -sS -X POST http://127.0.0.1:18082/test/peer \
  -H 'Content-Type: application/json' \
  -d '{"peer_agent_ip":"8.8.8.7"}'
```

对应的 Windows PowerShell 命令（明确在 B 发行版内调用 curl）：

```powershell
wsl.exe -d Ubuntu-Agent-B -- curl -sS -X POST `
  http://127.0.0.1:18082/test/peer `
  -H 'Content-Type: application/json' `
  -d '{"peer_agent_ip":"8.8.8.7"}'
```

在 A 所在 Ubuntu 中执行，告诉 A：B 的 Agent IP 是 `8.8.8.8`：

```bash
curl -sS -X POST http://127.0.0.1:18081/test/peer \
  -H 'Content-Type: application/json' \
  -d '{"peer_agent_ip":"8.8.8.8"}'
```

对应的 Windows PowerShell 命令（明确在 A 发行版内调用 curl）：

```powershell
wsl.exe -d Ubuntu-Agent-A -- curl -sS -X POST `
  http://127.0.0.1:18081/test/peer `
  -H 'Content-Type: application/json' `
  -d '{"peer_agent_ip":"8.8.8.8"}'
```

成功响应会包含 `status=OK`、本机/对端 Agent IP 和最终 `/message` URL。此时
每个实例只为刚配置的对端 IP安装一个 `/32` 主机路由；再次调用可替换对端地址。

#### 第四步：curl A，触发 A 向 B 发送消息

在 A 所在 Ubuntu 中执行：

```bash
curl -sS -X POST http://127.0.0.1:18081/test/send \
  -H 'Content-Type: application/json' \
  -d '{"type":"text","content":"hello B from A through MASQUE"}'
```

对应的 Windows PowerShell 命令：

```powershell
wsl.exe -d Ubuntu-Agent-A -- curl -sS -X POST `
  http://127.0.0.1:18081/test/send `
  -H 'Content-Type: application/json' `
  -d '{"type":"text","content":"hello B from A through MASQUE"}'
```

这里通过 `wsl.exe -d` 在指定发行版内部执行 curl，因此不会依赖 Windows 到 WSL
的 localhost 端口转发。如果确认本机 WSL localhost 转发可用，也可以在 PowerShell
中直接用 `curl.exe` 请求相同 URL；JSON 建议继续使用单引号包围，避免 PowerShell
改写双引号。

A 实例收到控制请求后固定向 `POST http://8.8.8.8:4001/message` 发送上面的 JSON。
B 收到后应在控制台看到：

```jsonl
{"role":"B","event":"MESSAGE_RECEIVED","method":"POST","path":"/message","source_ip":"8.8.8.7","local_url":"http://8.8.8.8:4001/message","payload":{"type":"text","content":"hello B from A through MASQUE"}}
```

默认日志文件如下，也可以通过 `--log-file` 修改：

```text
logs/masque-interactive-a.log
logs/masque-interactive-b.log
```

B 侧验证命令：

```bash
grep -E 'UE_INFO_AGENT_TUN_IP|MASQUE_CONNECTED|MESSAGE_RECEIVED' \
  logs/masque-interactive-b.log
```

也可在任一实例中执行 `curl -sS http://127.0.0.1:<控制端口>/test/status` 查看
本机 Agent IP、当前对端 IP 和 MASQUE 状态。脚本为 A/B 自动使用不同的 TUN
名称、MASQUE TLS 客户端状态目录和日志文件；Agent IP只来自
`GET /v1/ue/info` 和人工 curl，不查询或推导 Agent ID。若部署启用了 MASQUE
鉴权，为各实例增加对应的 `--masque-token`。

### 5.2 全接口真实端侧示例

`examples/linux_agent.py` 是连接真实 AgentRuntime、MASQUE 和对端 Agent 的
全流程示例，不再只执行 `init` 后常驻。它依次调用初始化、身份申请/恢复、
网络能力、能力注册/更新、发现、建群、等待群组快照、消息发送、计算卸载、
视频上传、处理后视频流、身份注销和关闭接口：

```bash
sudo -E .venv/bin/python examples/linux_agent.py \
  --runtime-ip 192.168.3.10 \
  --runtime-port 8080 \
  --local-vlan-ip 192.168.1.10 \
  --agent-name 'Agent A' \
  --owner 'customer-a' \
  --masque-url https://192.168.3.10:4433/.well-known/masque/ip \
  --masque-token 'replace-with-secret-for-device-a' \
  --required-skill text \
  --group-name customer-demo \
  --message '{"type":"text","content":"hello"}' \
  --sandbox-id sandbox-edge-1 \
  --log-file /var/log/agent-sdk/agent-a.log \
  --log-level INFO
```

身份申请的 `public_key` 由 SDK 从本地 P-256 设备密钥自动导出，命令行不再
接收公钥或私钥。该设备签名密钥与 MASQUE TLS 客户端密钥用途独立。
`--target-agent-id` 省略时使用发现结果的第一项；建群后脚本会等待
AgentRuntime 下发 `acf_group_config`，不会让用户填写对端 IP 或端口。

为保证示例能够实际执行所有媒体函数，文件内置了明确标记为 example-only
的 `ExampleMediaOffloadAdapter`；它不读取真实摄像头，也不代表真实 WebRTC
上传。生产联调必须替换成平台媒体适配器。

该全流程默认注销本次申请的身份。需要保留身份时传 `--keep-identity`，并把
验证后的 `AgentProfile` 安全持久化；传 `--stay-running` 可在流程完成后继续
接收消息。签名和验签全部由 SDK 内部执行。

## 6. 函数清单

| 函数 | 用途 | 关键返回值 |
|---|---|---|
| `init(...)` | 查询 UE/PDU 状态、建立下行 WebSocket、创建 TUN/A2A 服务并连接 MASQUE | `SdkInitResult` |
| `apply_identity(...)` | 申请身份，直接解析网元原始 `vc0` | `AgentProfile` |
| `set_local_profile_for_restore(profile)` | 恢复安全存储中的既有身份 | 无 |
| `deregister_identity(...)` | 注销身份 | `OperationResult` |
| `get_network_ability(...)` | 获取网络能力，直接解析原始 `vc1` | `NetworkAbility` |
| `register_capabilities(...)` | 发布已有 VC；内测时也可将能力字符串签成三方 VC 后发布 | `OperationResult` |
| `update_capabilities(...)` | `POST /arf/v1/agent-cards-update` 更新能力 | `OperationResult` |
| `discover_agents(...)` | 从原始 `result[].agent_card` 发现 Agent | `list[DiscoveredAgent]` |
| `create_group(...)` | 使用 `target_agents + group_config` 请求建群 | `GroupInfo` |
| `get_group_snapshot(group_id)` | 查询 SDK 已提交的只读群组快照 | `GroupConfigSnapshot | None` |
| `send_message(...)` | 按群组缓存自动解析地址并发送 `/A2A/message` | `MessageReceipt` |
| `create_offloading_session(...)` | 创建计算卸载会话 | `OffloadingSession` |
| `start_video_upload(...)` | 启动视频上传 | `VideoUploadHandle` |
| `get_processed_video_stream(...)` | 获取处理后视频流 | `RemoteVideoStream` |
| `close()` | 释放路由、TUN、HTTP/3 和监听服务 | 无 |

## 7. 常见问题定位

| 现象 | 检查项 |
|---|---|
| `TUN_CREATE_FAILED` | `/dev/net/tun` 是否存在；进程是否具备 `CAP_NET_ADMIN` |
| `LOG_SETUP_FAILED` | 日志目录是否存在或可创建；SDK 进程是否具有写权限 |
| `RUNTIME_UNREACHABLE` | AgentRuntime HTTP IP/端口和物理网络连通性 |
| `MASQUE_CONNECT_FAILED` | `masque_server_url` 的 UDP 可达性、客户端密钥目录权限和外部服务鉴权值；服务端问题交由外部系统维护方处理 |
| `CONNECT_IP_NEGOTIATION_FAILED` | 外部服务是否支持 HTTP/3 Datagram 和 CONNECT-IP |
| `GROUP_NOT_ACTIVE` | 是否已收到并成功验签 `acf_group_config` |
| `TARGET_NOT_IN_GROUP` | `target_agent_id` 是否存在于该群组最新快照 |
| 消息未经过 5GC | 将端侧日志和抓包交给外部 AgentRuntime/MASQUE/5GC 维护方定位；SDK 不包含服务器转发实现 |
| 日志出现证书校验关闭警告 | 当前为封闭内测安全配置；不得将该构建用于生产网络 |

开发者运行测试：

```bash
python -m pip install -e '.[test]'
pytest -q
```
