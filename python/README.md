# Agent Connect SDK（Linux/Python）客户使用指南

`agent-connect-sdk` 为 Linux 端 Agent 提供统一的控制面和数据面能力：应用只需要调用 SDK 函数，不需要感知对端 IP、TCP 端口、TUN 路由或 MASQUE 封装细节。

SDK 收到 AgentRuntime 通过 `ACN_AGENT_GROUPING_NOTIFICATION` 透传的 `acf_group_config` 后，会自动缓存 `group_id + agent_id -> agent_ip + service_endpoints + skills`，并自动维护对端 `/32` 或 `/128` 主机路由。相同时间戳且规范化配置相同的重复通知会直接 ACK，不重复写路由或通知应用；时间戳更新的通知正常提交并通知应用。应用发送时提供群组、目标 Agent、消息类型、任务 ID 和业务 JSON，不传 URL、IP、端口或路由。

## 1. 交付物和运行要求

建议向客户交付：

- `agent_connect_sdk-0.17.5-py3-none-any.whl`：只包含端侧 Client 的 SDK wheel。
- `examples/full_flow_demo.py`：不依赖真实网络的安装和全流程自检。
- `examples/linux_agent.py`：连接真实 AgentRuntime、TUN 和 MASQUE Proxy 的端侧常驻示例。
- `examples/interactive_linux_agent.py`：复用真实 Linux 全流程参数，每按一次回车只调用下一个 SDK 接口。
- `examples/agent_a_test.py`：A 发现 B、建组、申请正式算力会话，并验证处理后视频首帧。
- `examples/agent_b_test.py`：B 发布算力能力、自动接受邀请，并按 A 下发的 session ID 循环上传仓库内置测试视频。
- `examples/assets/video-offload-test.mp4`：用于 A/B 端到端联调的 8 秒 720p H.264 合成测试片。
- `examples/masque_two_instance_test.py`：在两个隔离的 Ubuntu 实例中验证 A 经 MASQUE/5GC 向 B 发送消息，B 在控制台和本地文件记录收包证据。
- `docker/arm64/`：在 x86 主机使用 Buildx 制作 `linux/arm64` 运行镜像，并提供 A/B 环境变量启动脚本；完整用法见 `docker/arm64/README.md`。

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
dist/agent_connect_sdk-0.17.5-py3-none-any.whl
```

文件名中的发行名使用下划线是 Python wheel 的标准规范；安装和查询时的项目名仍是 `agent-connect-sdk`。

### 2.2 客户安装

客户不需要源码，直接安装 wheel：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ./agent_connect_sdk-0.17.5-py3-none-any.whl
```

确认安装结果：

```bash
python -c 'import agent_sdk; print(agent_sdk.__version__)'
python -m pip show agent-connect-sdk
```

如果客户拿到的是源码目录，并准备运行本章的双实例 MASQUE 测试脚本，可以进入
`python` 目录后直接安装 `requirements.txt`：

```bash
cd python
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

该文件列出 `aiohttp`、`aioquic`、`cryptography`、`httpx` 和 `pyroute2`，并通过
`-e .` 安装当前源码中的 `agent_sdk`，因此 examples 无需手工设置
`PYTHONPATH`。源码的 PEP 517/PEP 660 构建只要求 `setuptools>=68`，不要求软件源
额外提供 `wheel` 包。它只包含客户运行依赖；执行仓库测试时仍使用：

```bash
python -m pip install -e '.[test]'
```

`pip` 会自动安装 `aiohttp`、`aioquic`、`cryptography`、`httpx` 和 `pyroute2` 等依赖。如果客户环境不能访问公网，应同时交付依赖 wheel，并使用：

```bash
python -m pip install --no-index --find-links ./wheelhouse \
  ./agent_connect_sdk-0.17.5-py3-none-any.whl
```

发布方可以这样生成离线依赖目录：

```bash
python -m pip download --dest wheelhouse \
  ./dist/agent_connect_sdk-0.17.5-py3-none-any.whl
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

设备 B 只需替换 `masque_authorization`。MASQUE 外层源地址由操作系统根据
`masque_server_url` 的路由自动选择；应用不传物理网卡 IP。

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

### 3.4 单接口失败隔离与业务异常处理

SDK 在 `READY` 状态下调用身份、能力、发现、建组、消息或算力接口时，单次
HTTP 超时、Runtime 拒绝、对端返回错误或本地参数错误只会使当前调用抛出
`AgentSdkError`，不会自动执行 `close()`，也不会关闭 TUN、MASQUE、Runtime
WebSocket 或本地 A2A HTTP 服务。应用捕获本次异常后，可以继续调用其他接口，
也可以根据 `exc.retryable` 决定是否由业务侧重试：

```python
from agent_sdk import AgentSdkError

try:
    agents = await sdk.discover_agents(
        agent_id=profile.agent_id,
        task_description="find a text agent",
        required_skills=["text"],
    )
except AgentSdkError as exc:
    logger.error(
        "discovery failed: code=%s retryable=%s",
        exc.code.value,
        exc.retryable,
    )

# SDK 仍为 READY；后续接口可正常调用。
ability = await sdk.get_network_ability(profile.agent_id)
```

SDK 不自动重试写接口。身份申请、能力注册、建组和消息发送发生超时时，远端
可能已经受理但响应丢失，自动重试可能产生重复业务操作，应由应用结合
`request_id`、业务状态和接口幂等语义决定。日志会记录
`interface_failure_isolated`、`sdk_state=READY` 和 `sdk_kept_running=true`。

`init()` 是例外：初始化尚未成功时没有可继续使用的业务链路，失败后 SDK 会
清理半初始化资源并进入 `CLOSED`；调用方仍可在外部条件恢复后对同一个实例再次
调用 `init()`。只有应用显式调用 `close()`，SDK 才会退出运行态。关闭时 SDK
先排空本地 A2A HTTP 服务，并保持上行转发和 MASQUE 存活到响应发送完成，避免
监听器刚返回就关闭导致对端收不到 HTTP 响应。

### 3.5 SDK 内建的消息签名和联调验签策略

用户不配置密钥，也不实现安全回调。第一次 `init()` 会生成一套独立于
MASQUE TLS 的 P-256 消息签名密钥：

- 私钥：`$XDG_STATE_HOME/agent-sdk/security/device-private-key.pem`，未设置
  `XDG_STATE_HOME` 时位于 `~/.local/state/agent-sdk/security/`。
- 公钥：同目录 `device-public-key.pem`，身份申请时 SDK 自动转换成 Base64
  DER SubjectPublicKeyInfo，填入 HTTP 请求的 `public_key`。
- 目录权限为 `0700`，两个密钥文件权限为 `0600`；后续启动复用同一密钥。
- 私钥不进入请求、返回值或日志。

签名算法固定为 P-256 ECDSA + SHA-256。身份申请的普通 `signature` 待签字节为
`ACN-H-ID-v1\0 + LP16(owner/name/publicKey/description) + U64BE(timestamp) +
LP16(完整紧凑 metadata JSON)`，签名使用 ASN.1 DER 再做标准 Base64。
`metadata` 的必填字段顺序固定为 `region/os/version`，额外字符串字段
按字段名排序；SDK 签名和 HTTP 发送使用同一个紧凑 UTF-8 JSON。
其他控制请求的 `proof.jws` 使用 `ES256`、RFC 7797 `b64=false` 的分离
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
核心网私钥不会进入 SDK。验签实现和内置公钥继续保留，便于后续恢复；当前
封闭联调 Profile 不执行 `acf_group_config.proof` 的入站验签；现行 A2A 请求本身
不包含消息级 `proof`。收到消息后直接进入原有字段校验、成员缓存或业务投递流程。
SDK 初始化日志会输出 `inbound_signature_verification_disabled` 警告。控制面出站
签名仍照常生成。该 Profile 不得用于生产环境。

## 4. 应用如何调用 SDK

### 4.1 创建 SDK 和注册监听器

```python
from agent_sdk import AgentSdk, NetworkMessageAction, NetworkMessageType


class NetworkListener:
    async def on_network_message(self, message_type, payload):
        if message_type is NetworkMessageType.GROUP_INVITATION:
            return NetworkMessageAction.ACCEPT
        if message_type is NetworkMessageType.GROUP_CONFIG:
            # 到达监听器前，SDK 已完成字段校验、缓存提交和路由安装。
            return NetworkMessageAction.ACK
        return NetworkMessageAction.REJECT


class GroupListener:
    async def on_group_message(self, group_id, sender_agent_id, payload):
        print("收到消息", group_id, sender_agent_id, payload)


sdk = AgentSdk()
unregister_network = sdk.register_network_message_listener(NetworkListener())
unregister_group = sdk.register_group_message_listener(GroupListener())
```

这两个注册函数都是公开 SDK 接口，应在创建 `AgentSdk` 后、调用 `init()` 前执行：

- `register_network_message_listener` 同时处理群组邀请和群组配置。收到
  `GROUP_INVITATION` 时返回 `ACCEPT/REJECT` 决定是否接受邀请；`GROUP_CONFIG`
  在 SDK 已提交缓存和路由后用于通知应用。
- `register_group_message_listener` 接收其他 Agent 调用 `send_message` 投递的业务
  JSON。`send_message` 自身是发送接口，不接收回调参数。

注册动作不发送 HTTP，也不建立连接；实际 Runtime 下行 WebSocket 和本地
`/A2A/message` 服务由 `init()` 启动。两个返回值都是无参注销函数，应用退出时依次
调用 `unregister_group()` 和 `unregister_network()`。

应用不传入签名器、验签器或私钥。首次 `init()` 时 SDK 自动生成 P-256
设备密钥并持久化；之后控制面请求复用该私钥签名。当前封闭联调构建不会校验
核心网下发的 `acf_group_config.proof`；A2A 按当前接口不携带消息级 proof。

### 4.2 身份、能力、发现和建群

SDK 会按 AgentRuntime 的 `IP:端口` 持久化 Agent 业务状态：

| `sdk.agent_lifecycle_state` | 含义 | 下一步 |
|---|---|---|
| `NO_IDENTITY` | 未获取数字身份 | 调用 `apply_identity()`；`reset_agent()` 幂等成功 |
| `IDENTITY_READY` | 已保存 `AgentProfile`，未发布 Agent Card | 调用 `register_capabilities()`，或 `reset_agent()` 回到状态1 |
| `CARD_PUBLISHED` | 身份和 Agent Card 均已就绪 | 直接调用 `update_capabilities()`；再次调用 `register_capabilities()` 会替换身份和整张 Card；也可 `reset_agent()` 回到状态1 |

状态文件默认位于 `$XDG_STATE_HOME/agent-sdk/agents` 或
`~/.local/state/agent-sdk/agents`，目录/文件权限为 `0700/0600`。应用只读取
`sdk.agent_lifecycle_state` 和 `sdk.local_profile`，不要自行编辑 JSON。状态2再次调用
`apply_identity()` 时，SDK 会先以 `replaced` 注销旧身份，再使用本次参数申请新身份。
状态3调用 `update_capabilities()` 时，SDK 直接调用
`POST /arf/v1/agent-cards-update`；成功后身份不变、状态仍为3，并同步更新本地完整
Card 快照。状态3再次调用 `register_capabilities()` 表示替换整张 Agent Card：SDK 先
完整校验输入，再依次调用 `deregister_identity()`、`apply_identity()`、
`register_capabilities()`。替换成功后 `agent_id` 可能变化，应重新读取
`sdk.local_profile`。参数less `reset_agent()` 是应用控制状态机回到状态1的便捷接口：
所有状态都不发 HTTP；状态2/3直接删除本地 Profile、身份申请上下文和完整 Agent Card
快照并进入 `NO_IDENTITY`，不修改网侧身份；状态1调用幂等成功。

```python
profile = await sdk.apply_identity(
    owner="customer-a",
    name="Agent A",
    description="RayNeo edge agent",
    metadata={"region": "CN", "os": "Linux", "version": "0.17.5"},
)

ability = await sdk.get_network_ability(profile.agent_id)

await sdk.register_capabilities(
    profile.agent_id,
    priority=1,
    credentials=[ability.ability_vc],
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
    agent_id=profile.agent_id,
    task_description="寻找支持文字消息的 Agent",
    required_skills=["text"],
    max_results=10,
)
# 每项包含 agent_id、service_endpoints、skills、priority

group = await sdk.create_group(
    agent_id=profile.agent_id,
    target_agent_ids=[agents[0].agent_id],
    group_name="customer-demo",
    dnn="internet",
    max_members=2,
)
```

`dnn` 是建组必填参数，必须与目标 Agent 可用的 PDU 会话数据网络一致。SDK 将其
原样放入 `group_config.dnn` 并纳入 proof，空字符串或纯空白值会在本地直接拒绝。

`credentials` 是正式用法：调用方传入已经由运营商或能力认证组织签发的 VC，
SDK 将其原样放入 `vc_list`。封闭实验环境还可直接传能力字符串；SDK 会使用
测试三方机构的 P-256 私钥为每个能力生成一张 `AgentCapabilityCredential`，再追加
到同一个 `vc_list`：

```python
await sdk.register_capabilities(
    profile.agent_id,
    priority=1,
    credentials=[ability.ability_vc],
    capabilities=["robot-control", "voice"],
    # test_vc_private_key_path 省略时使用 Wheel 内置的联调测试私钥
)
```

生成的测试 VC 使用
`did:thirdpartyissuer@6gc.mnc015.mcc234.3gppnetwork` 作为 `issuer`，签名原文
使用与现网 IDM 一致的 `JsonWebSignature2020`、
`proof_purpose=assertionMethod` 和 ES256 分离 JWS；能力名写入
`claims.skill_name`。`valid_from` 为实际签发时间前一个日历年，`proof.created` 仍为
实际签发时间，默认 `valid_until` 为实际签发时间后 365 天；调用方通过
`credentials` 提供的预签发 VC 不会被修改。
测试公私钥分别位于 Wheel 包内 `agent_sdk/certs/` 下的
`third-party-capability-public-key.pem` 和
`third-party-capability-private-key.pem`，均通过包相对资源读取。可通过
`test_vc_private_key_path` 显式覆盖私钥，但正常联调不需要配置路径。此入口仅
用于联调；正式环境应由独立能力认证服务签发 VC，再通过 `credentials` 发布。

SDK 会自动把本机 Agent TUN IP、`local_tcp_port` 和固定 `/A2A/message` 路径
组合成顶层 `service_endpoints`，例如
`http://10.60.0.2:4001/A2A/message`。该字段会与 `agent_id/priority/vc_list` 一起
进入外层 proof，用户不需要也不能在 `register_capabilities()` 中传地址。

AgentRuntime 随后通过已建立的 WebSocket 下发核心网请求：

```json
{
  "kind": "request",
  "request_id": "delivery-123",
  "message_type": "ACN_AGENT_GROUPING_INVITATION",
  "transaction_id": 49,
  "payload": {
    "group_info": {
      "target_agent_id": "agent-b",
      "group_id": "g1",
      "group_name": "task-patrol"
    },
    "group_administrator": {"agent_id": "a1"}
  }
}
```

SDK 按 `request_id` 并发处理并允许乱序返回。邀请接受从下发的
`payload.group_info.group_id` 取值，配置确认从 `payload.group_id` 取值并原样带回，例如
`{"kind":"response","request_id":"delivery-123","payload":{"group_id":"g1","result":"ACCEPT"}}`；
配置确认的 `result` 为 `ACK`。
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

应用不传 URL、IP 或端口。SDK 直接调用群组配置中已缓存的完整
`service_endpoints`，不改写 scheme、authority、端口或路径。例如：

```text
service_endpoints = http://agent-b:4001/A2A/message
实际请求          = POST http://agent-b:4001/A2A/message
```

这个 HTTP 包由系统路由送入 Agent TUN，再通过 CONNECT-IP 和 5GC U 面到达对端。群组配置不存在或目标不在群组时，SDK 会拒绝发送，不会回退到用户提供的地址；当前联调 Profile 不会因 proof 验签失败拒绝消息。

### 4.4 计算和视频卸载

四种算力会话操作都使用 `init()` 中的 AgentRuntime IP 和端口：

```text
POST http://{agent_runtime_ip}:{agent_runtime_port}/v1/computing/session-requests
```

每次请求前，SDK 自动读取 `/v1/acn/status` 和 `/v1/ue/info`，确认 NAS 已就绪、
至少有一个 ACTIVE IPv4 PDU Session，并存在
`HTTP3_CONNECT_IP + EXACT_PDU_SESSION_ID` 数据面能力。CREATE 还会确认
`acn_context.group_id` 在本地为 `ACTIVE`、请求方是本机 Agent、目标 Agent 在群组中。

结构化 CREATE 示例：

```python
from agent_sdk import (
    AcnContext,
    ComputeConstraints,
    ComputeInputFormat,
    ComputeRequestType,
    ComputeResources,
    ComputeSessionRequest,
)

create_status = await sdk.create_computing_session(
    ComputeSessionRequest(
        message_type="COMPUTE_SESSION_REQUEST",
        request_type=ComputeRequestType.CREATE,
        input_format=ComputeInputFormat.STRUCTURED,
        request_id="create-glasses-001",
        acn_context=AcnContext(
            group_id=group.group_id,
            requester_agent_id=profile.agent_id,
            target_agent_id=target.agent_id,
        ),
        constraints=ComputeConstraints(
            capability_id="video_rendering",
            resources=ComputeResources(
                cpu_millicores=2000,
                memory_mib=4096,
            ),
            dnn="internet",
            allow_base_qos=True,
        ),
    )
)
session_id = create_status.compute_service_session_id
```

同步响应是 `ComputeSessionStatus`，CREATE 成功使用 HTTP 202；它只表示请求已受理，
不返回 Sandbox IP 或媒体 URL。自然语言 CREATE 使用
`input_format=NATURAL_LANGUAGE` 和非空 `text`；结构化 CREATE 使用非空
`constraints`，两者互斥。重试同一操作必须复用相同 `request_id` 和相同请求体。

QUERY、CANCEL 和 RELEASE 使用同一路径：

```python
query_status = await sdk.query_computing_session(
    ComputeSessionRequest(
        message_type="COMPUTE_SESSION_REQUEST",
        request_type=ComputeRequestType.QUERY,
        input_format=ComputeInputFormat.STRUCTURED,
        request_id="query-001",
        target_request_id="create-glasses-001",
    )
)
cancel_status = await sdk.cancel_computing_session(
    ComputeSessionRequest(
        message_type="COMPUTE_SESSION_REQUEST",
        request_type=ComputeRequestType.CANCEL,
        input_format=ComputeInputFormat.STRUCTURED,
        request_id="cancel-001",
        target_request_id="create-glasses-001",
    )
)
release_status = await sdk.release_computing_session(
    ComputeSessionRequest(
        message_type="COMPUTE_SESSION_REQUEST",
        request_type=ComputeRequestType.RELEASE,
        input_format=ComputeInputFormat.STRUCTURED,
        request_id="release-001",
        compute_service_session_id=session_id,
    )
)
```

SDK 在 `init()` 中通过公共 WebSocket 内部注册并处理
`COMPUTE_CONNECT_CONFIG`、`COMPUTE_SESSION_STATUS` 和
`COMPUTE_SESSION_CLOSE`。应用不注册算网 listener。C-02 到达后，SDK 校验
`receiver_agent_id`、PDU Session ID、DNN、S-NSSAI、`ue_ipv4` 和 Runtime 数据面能力，
为 `service_endpoint` 安装用户面路由，缓存 `binding_ref`、角色和媒体路径，并自动返回
C-03。C-04 按 `status_revision` 去重。C-05 会停止该会话的媒体对象、释放路由并自动
返回 C-06。C-02 没有 `group_id`，因此其群组授权结果以网侧 CA 为准，SDK 只复核本端
身份和网络绑定。

媒体接口只接收 `compute_service_session_id`。它们会等待对应 C-02，再从 SDK 缓存的
`service_endpoint + media_connections_path` 生成完整内部 URL。consumer 使用：

```python
stream = await sdk.get_processed_video_stream(session_id, timeout_seconds=15.0)
frame = await stream.recv()
# 不再使用时释放本地 PeerConnection，并自动 DELETE Sandbox 媒体资源
await stream.close()
```

producer 使用：

```python
upload = await sdk.start_video_upload(
    session_id,
    camera_id=0,
    width=1280,
    height=720,
    fps=30,
    bitrate_kbps=2500,
    timeout_seconds=15.0,
)
```

两端都是 Offerer。producer 的 Offer 为 `sendonly`，consumer 的 Offer 为
`recvonly`。SDK 调用 `setLocalDescription()` 并等待 ICE gathering complete，随后向
C-02 给出的媒体集合路径发送正式
`request_id + computing_context + offer` JSON；只接受 HTTP 201、原样回显的上下文、
`media_connection_id` 和 `answer.type=answer`。应用不接触 SDP。SDK 应用 Answer 前会把
其中的 IPv4 ICE 候选加入 CONNECT-IP 路由，同时拒绝未发布 C-02 `ue_ipv4` 或夹带其他
物理网 host candidate 的本地 Offer。`upload.stop()`、`stream.close()`、C-05 和
`sdk.close()` 都会关闭本地 PeerConnection；主动关闭还会自动调用 Sandbox 的
`DELETE /v1/media-connections/{media_connection_id}`。

consumer 可以通过 C-02 的 `recognition_target_path_template` 更新或读取当前识别目标，
无需接触 Sandbox 地址和 `computing_context`：

```python
target = await sdk.update_recognition_target(
    session_id,
    request_id="recognition-001",
    text="寻找红色玩偶",
    language="zh",
)
assert target.status == "APPLIED"
print(target.target_revision, target.target.label, target.target.prompt)

current = await sdk.get_recognition_target(session_id)
```

SDK 将会话 ID 展开到 C-02 路径模板，使用同一个 UE IPv4 访问 Sandbox，并校验 HTTP
200、`request_id/computing_context` 回显、`status=APPLIED`、uint64
`target_revision` 以及 `target.label/target.prompt`。同一更新重试时，应用复用原
`request_id` 和原文本。

运行期动作同样由 SDK 直接发送到 Sandbox。文本动作由 Sandbox 规范化，结构化动作
则显式提供 `movement`、`grab` 或 `search_object` 和参数：

```python
from agent_sdk import ControlActionRequest, ControlInputType

action = await sdk.create_control_action(
    session_id,
    ControlActionRequest(
        request_id="control-search-001",
        input_type=ControlInputType.TEXT,
        text="寻找杯子",
        language="zh",
    ),
)

while action.status in {"ACCEPTED", "RUNNING", "UNKNOWN"}:
    action = await sdk.get_control_action(session_id, action.action_id)
```

创建固定调用 `POST /v1/control-actions` 并要求 HTTP 202；查询调用
`GET /v1/control-actions/{action_id}` 并要求 HTTP 200。SDK 校验请求和动作 ID、状态、
`cause`、可选结果，以及创建响应的 `computing_context` 回显。Sandbox 向 producer
Runtime 转发机器狗动作的接口不向应用暴露。

请求方是 consumer，CREATE 中的目标 Agent 是 producer。应用如需通知 producer 启动
上传，只通过现有 `send_message` 传递 `compute_service_session_id`；不要传 Sandbox IP、
端口或 URL。producer 的 SDK 已通过自己的 C-02 获得完整端点配置。

算力 HTTP 请求固定复用 `init()` 的 `agent_runtime_ip/agent_runtime_port`；SDK 不再
提供独立算力控制地址覆盖。仓库现有 Android 测试 App 与旧 Mock Server 本轮按要求
没有迁移，旧联调需继续使用此前与它们匹配的构建产物。

### 4.5 关闭

```python
try:
    # 业务逻辑
    ...
finally:
    await sdk.close()
```

仅在确实要废弃身份时调用。应用只需回到状态1时优先使用参数less Reset：

```python
result = await sdk.reset_agent()
assert result.success
assert sdk.agent_lifecycle_state is AgentLifecycleState.NO_IDENTITY
```

需要显式指定 Agent DID 或注销原因时，仍可调用
`await sdk.deregister_identity(profile.agent_id, reason="retired")`。

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

MASQUE Client 建立 CONNECT-IP 后会每 15 秒发送一次 QUIC PING 保活。因此在等待
人工配置对端 IP 或执行发送 curl 时，连接不会因为 aioquic 默认的 60 秒空闲超时
而关闭。日志出现 `masque_keep_alive_started` 表示保活任务已经启动；如果仍出现
`connect_ip_connection_closed`，应检查服务器是否配置了与 QUIC idle timeout 无关的
硬性会话时限或 UDP 中间设备是否丢弃长连接。

### 5.2 全接口真实端侧示例

`examples/linux_agent.py` 是连接真实 AgentRuntime、MASQUE 和对端 Agent 的
全流程示例，不再只执行 `init` 后常驻。它依次调用初始化、身份申请/恢复、
网络能力、能力注册/更新、发现、建群、等待群组快照、消息发送、计算卸载、
视频上传、处理后视频流、身份注销和关闭接口：

```bash
sudo -E .venv/bin/python examples/linux_agent.py \
  --runtime-ip 192.168.3.10 \
  --runtime-port 8080 \
  --agent-name 'Agent A' \
  --owner 'customer-a' \
  --masque-url https://192.168.3.10:4433/.well-known/masque/ip \
  --masque-token 'replace-with-secret-for-device-a' \
  --required-skill text \
  --group-name customer-demo \
  --dnn internet \
  --message '{"type":"text","content":"hello"}' \
  --compute-capability-id video_rendering \
  --compute-cpu-millicores 2000 \
  --compute-memory-mib 4096 \
  --log-file /var/log/agent-sdk/agent-a.log \
  --log-level INFO
```

身份申请的 `public_key` 由 SDK 从本地 P-256 设备密钥自动导出，命令行不再
接收公钥或私钥。该设备签名密钥与 MASQUE TLS 客户端密钥用途独立。
`--target-agent-id` 省略时使用发现结果的第一项；建群后脚本会等待
AgentRuntime 下发 `acf_group_config`，不会让用户填写对端 IP 或端口。

Wheel 安装依赖包含 `aiortc`。`start_video_upload()` 默认通过 Linux V4L2 打开
`/dev/video{camera_id}`，`get_processed_video_stream()` 返回可 `recv()` 和
`close()` 的 aiortc 远端视频流；应用不再实现或注入媒体 HTTP/WebRTC 适配器。

该全流程默认注销本次申请的身份。需要保留身份时传 `--keep-identity`，并把
验证后的 `AgentProfile` 安全持久化；传 `--stay-running` 可在流程完成后继续
接收消息。控制面出站签名由 SDK 内部执行；当前联调 Profile 暂停群组配置验签，
A2A 消息按现行接口不携带 proof。

### 5.3 按回车逐接口调用的真实测试 Demo

`examples/interactive_linux_agent.py` 使用与 `linux_agent.py` 完全相同的真实部署
参数和调用顺序，但每次先显示即将调用的函数、对应 HTTP 接口或本地动作；按回车
后只执行当前一步，打印返回结果后再等待下一次回车。输入 `q`、`quit` 或 `exit`
可以终止，SDK 仍会自动释放已经创建的资源。

```bash
sudo -E .venv/bin/python examples/interactive_linux_agent.py \
  --runtime-ip 192.168.3.10 \
  --runtime-port 8080 \
  --agent-name 'Agent A' \
  --owner 'customer-a' \
  --masque-url https://192.168.3.10:4433/.well-known/masque/ip \
  --required-skill text \
  --group-name customer-demo \
  --dnn internet \
  --message '{"type":"text","content":"hello"}' \
  --log-file ./logs/interactive-agent-a.log
```

示例不会直接在 asyncio 事件循环中执行阻塞式 `input()`，而是把终端读取放到工作
线程。因此等待用户按回车期间，MASQUE QUIC PING 保活、AgentRuntime 下行
WebSocket、A2A HTTP 监听仍然正常工作。交互步骤覆盖监听器注册、`init`、身份申请/
恢复、网络能力、能力注册/更新、发现、建组、群组缓存、消息发送、算力卸载、媒体
句柄操作、身份注销和 `close`。

### 5.4 Linux A/B 算力会话与视频联调

两个自动脚本都会在 `sdk.init()` 后恢复上述状态机：状态1执行身份申请和 Agent Card
发布；状态2只获取网络能力并发布 Agent Card；状态3复用保存的 Profile，跳过
`apply_identity()`、`get_network_ability()` 和 `register_capabilities()`。因此脚本重启不会
重复发布 Agent Card。只有显式增加 `--deregister-on-exit` 才在退出前回到状态1。

`agent_a_test.py` 和 `agent_b_test.py` 还支持 `--fresh-registration`。启用后，脚本只把
恢复出的 Profile 用作网侧清理句柄：先以 `reason=replaced` 调用
`deregister_identity()`，成功后再完整执行 `apply_identity()`、
`get_network_ability()` 和 `register_capabilities()`；注销失败时测试直接失败，绝不
回退到复用。ARM 测试镜像默认同时启用 `--fresh-registration` 和
`--deregister-on-exit`，普通命令行调用仍保持向后兼容的默认行为。

本测试使用两个独立脚本。B 先发布 `video_rendering` 能力并等待；A 按能力发现 B、
建立二人群组，然后执行完整的算力媒体流程：

1. A 使用正式 `ComputeSessionRequest` 调用 `create_computing_session()`；
2. A 通过群组消息只把 `compute_service_session_id` 发送给 B，消息不包含 Sandbox
   IP、端口、URL、ticket 或 token；
3. B 的 A2A 回调把 session ID 放入队列后立即返回，B 主任务再调用
   `start_video_upload()`，因此不会用 WebRTC 建连阻塞 A2A 响应；
4. A 调用 `query_computing_session()`，再调用 `get_processed_video_stream()` 并等待
   处理后视频首帧；
5. A 默认调用 `release_computing_session()`，双方 SDK 收到 C-05 后自动关闭 WebRTC
   与数据面引用并回复 C-06。B 检测上传句柄进入 `STOPPED` 后结束测试。

C-02、C-04 和 C-05 仍由 SDK 内部处理。两个脚本没有注册算力下行回调；应用只处理
群组邀请、群组配置和 A2A 业务消息。A/B 调用媒体接口时只传
`compute_service_session_id`，Sandbox 端点及媒体路径来自 SDK 的 C-02 缓存。

先在设备 B 启动：

```bash
cd /path/to/orange_sdk/python
sudo -E .venv/bin/python examples/agent_b_test.py \
  --runtime-ip 192.168.3.10 \
  --runtime-port 8089 \
  --masque-url https://192.168.3.10:8444/.well-known/masque/ip \
  --capability video_rendering \
  --video-source file \
  --video-file ./examples/assets/video-offload-test.mp4 \
  --video-bitrate-kbps 2500 \
  --max-sessions 1 \
  --log-file ./logs/agent-b-test.log
```

B 默认循环读取仓库中的 8 秒 H.264 合成测试片
`examples/assets/video-offload-test.mp4`，不依赖真实摄像头。文件解码后的画面仍通过
正式 producer WebRTC 链路发送到 Sandbox。需要恢复摄像头测试时改为
`--video-source camera --camera-id 0`，并确认 V4L2 设备支持指定分辨率和帧率。
等待 B 输出 `B_READY` 后，
再在设备 A 启动：

```bash
cd /path/to/orange_sdk/python
sudo -E .venv/bin/python examples/agent_a_test.py \
  --runtime-ip 192.168.3.10 \
  --runtime-port 8088 \
  --masque-url https://192.168.3.10:8443/.well-known/masque/ip \
  --target-capability video_rendering \
  --group-name agent-a-b-test-group \
  --dnn internet \
  --message '{"type":"text","content":"hello Agent B from Agent A"}' \
  --compute-capability-id video_rendering \
  --compute-cpu-millicores 2000 \
  --compute-memory-mib 4096 \
  --frame-count 1 \
  --terminal-action release \
  --log-file ./logs/agent-a-test.log
```

A 仍会先发送原 ACN 文本测试消息，再申请算力会话。能力发现结果中存在多个相同
能力的 Agent 时，可增加 `--target-agent-id <B的Agent ID>` 精确选择。建组邀请和
群组配置是网络下行事务，两个脚本会立即处理，不额外等待回车。`--frame-count 0`
可持续接收视频直到 Ctrl+C；`--terminal-action cancel` 可改测 CANCEL；
`--terminal-action none` 只用于观察会话，不会主动回收网侧资源。CREATE 的其他正式
约束可通过 `--compute-api-version`、`--compute-image-id`、`--compute-gpu-count`、
`--compute-gpu-model`、`--compute-snssai`、`--compute-max-duration-ms`、区域及
`--[no-]allow-base-qos` 传入。

成功判据如下：

- A 输出 `TARGET_B_SELECTED`、`GROUP_CONFIG_READY`、`COMPUTING_SESSION_CREATED`、
  `COMPUTING_SESSION_QUERIED`、`PROCESSED_VIDEO_FRAME` 和
  `COMPUTING_SESSION_TERMINATED`；
- B 输出 `GROUP_INVITATION_ACCEPTED`、`GROUP_CONFIG_APPLIED` 和
  `VIDEO_UPLOAD_STARTED`、`COMPUTING_SESSION_CLOSED`；
- 算力 A2A 消息体只有 `compute_service_session_id`，端点只出现在 SDK 日志中的 C-02
  处理和 U-MEDIA 协商中。

测试能力 VC 默认由 B 使用 Wheel 内置的三方测试私钥签发，对应公私钥都位于
`agent_sdk/certs/third-party-capability-*-key.pem`，不依赖
`/root/lpx/cert/third-party`。只有需要覆盖测试私钥时才传
`--third-party-private-key`。如果需要在每个主动调用前
人工确认，可显式增加 `--prompt`；B 的 `--max-sessions 0` 会持续服务后续会话。
SDK 文件日志在 `sdk.init()` 中完成初始化，
脚本启动到 `init` 之前的状态会直接输出到终端。

如果 `sdk.init()` 报告 `MASQUE QUIC handshake timed out after 10s`，表示
`GET /v1/ue/info` 之后的 QUIC/UDP 握手未收到服务端响应，还没有进入
CONNECT-IP HTTP 协商。先查看 SDK 日志：

收到 CONNECT-IP HTTP 200 后，SDK 会继续等待服务端 HTTP/3 SETTINGS，并记录
`masque_http3_settings_received`。只有 SETTINGS 明确没有 `H3_DATAGRAM=1` 时才
返回 `peer did not negotiate HTTP/3 Datagram`，避免响应与 SETTINGS 到达顺序
造成偶发误判。

```bash
tail -f ./logs/agent-b-test.log
```

再确认操作系统存在可到达 MASQUE 服务器的路由；SDK 会使用该路由自动选择源地址：

```bash
ip addr show
ip route get <MASQUE服务器IP>
sudo tcpdump -ni any udp port <MASQUE端口>
```

服务器侧应确认对应的是 UDP 监听端口，不是同端口的 TCP 服务：

```bash
ss -lunp | grep <MASQUE端口>
```

## 6. 函数清单

| 函数 | 用途 | 关键返回值 |
|---|---|---|
| `init(...)` | 查询 UE/PDU 状态、建立下行 WebSocket、创建 TUN/A2A 服务并连接 MASQUE | `SdkInitResult` |
| `apply_identity(...)` | 申请身份，直接解析网元原始 `vc0` | `AgentProfile` |
| `set_local_profile_for_restore(profile)` | 恢复安全存储中的既有身份 | 无 |
| `deregister_identity(...)` | 注销身份 | `OperationResult` |
| `reset_agent()` | 参数less 本地控制接口；清除本地身份/Card 状态并回到状态1，不发送 HTTP；状态1幂等成功 | `OperationResult` |
| `get_network_ability(...)` | 获取网络能力，直接解析原始 `vc1` | `NetworkAbility` |
| `register_capabilities(...)` | 状态2发布已有 VC；状态3再次调用时先替换身份，再发布整张新 Card | `OperationResult`；替换后 Profile 从 `local_profile` 读取 |
| `update_capabilities(...)` | 状态3通过 `POST /arf/v1/agent-cards-update` 直接增删能力，不替换身份 | `OperationResult` |
| `discover_agents(...)` | 从原始 `result[].agent_card` 发现 Agent | `list[DiscoveredAgent]` |
| `create_group(..., dnn, ...)` | 使用 `target_agents + group_config` 请求建群；`dnn` 必填 | `GroupInfo` |
| `get_group_snapshot(group_id)` | 查询 SDK 已提交的只读群组快照 | `GroupConfigSnapshot | None` |
| `send_message(...)` | 按群组缓存直接调用完整 `service_endpoints` | `MessageReceipt` |
| `register_network_message_listener(listener)` | 接受/拒绝群组邀请，并接收群组配置已提交通知 | 无参注销函数 `Callable[[], None]` |
| `register_group_message_listener(listener)` | 接收其他 Agent 通过 `send_message` 投递的业务 JSON | 无参注销函数 `Callable[[], None]` |
| `create_computing_session(request, ...)` | 提交正式 CREATE；校验本地 ACTIVE 群组和双方 Agent 身份 | `ComputeSessionStatus` |
| `query_computing_session(request, ...)` | 按会话 ID 或原 CREATE request ID 查询状态 | `ComputeSessionStatus` |
| `cancel_computing_session(request, ...)` | 按会话 ID 或原 CREATE request ID 取消请求 | `ComputeSessionStatus` |
| `release_computing_session(request, ...)` | 释放指定正式算力会话 | `ComputeSessionStatus` |
| `start_video_upload(compute_service_session_id, ..., timeout_seconds=15)` | producer 等待 C-02，创建完整 sendonly Offer，内部完成 Sandbox 协商；`stop()` 自动 DELETE 媒体资源 | `VideoUploadHandle` |
| `get_processed_video_stream(compute_service_session_id, timeout_seconds=15)` | consumer 等待 C-02，创建完整 recvonly Offer，内部完成 Sandbox 协商；`close()` 自动 DELETE 媒体资源 | `RemoteVideoStream` |
| `update_recognition_target(compute_service_session_id, request_id, text, language=None, ...)` | consumer 使用 C-02 路径替换当前持续识别目标 | `RecognitionTargetStatus` |
| `get_recognition_target(compute_service_session_id, ...)` | consumer 读取当前已应用的识别目标 | `RecognitionTargetStatus` |
| `create_control_action(compute_service_session_id, request, ...)` | consumer 提交文本或结构化运行期动作 | `ControlActionStatus` |
| `get_control_action(compute_service_session_id, action_id, ...)` | consumer 查询异步动作状态，不重复执行 | `ControlActionStatus` |
| `close()` | 释放路由、TUN、HTTP/3 和监听服务 | 无 |

## 7. 常见问题定位

| 现象 | 检查项 |
|---|---|
| `TUN_CREATE_FAILED` | `/dev/net/tun` 是否存在；进程是否具备 `CAP_NET_ADMIN` |
| `LOG_SETUP_FAILED` | 日志目录是否存在或可创建；SDK 进程是否具有写权限 |
| `RUNTIME_UNREACHABLE` | AgentRuntime HTTP IP/端口和物理网络连通性 |
| `MASQUE_CONNECT_FAILED` | `masque_server_url` 的 UDP 可达性、客户端密钥目录权限和外部服务鉴权值；服务端问题交由外部系统维护方处理 |
| `CONNECT_IP_NEGOTIATION_FAILED` | 外部服务是否支持 HTTP/3 Datagram 和 CONNECT-IP |
| `GROUP_NOT_ACTIVE` | 是否已收到并成功应用 `acf_group_config` |
| `TARGET_NOT_IN_GROUP` | `target_agent_id` 是否存在于该群组最新快照 |
| `MEDIA_NEGOTIATION_FAILED` | 检查 C-02 媒体路径、Sandbox 201/204 响应、Answer SDP、指定编解码器和 ICE 用户面可达性；`retryable=true` 时再次调用同一媒体接口，SDK 会复用未决 request ID/Offer |
| `SANDBOX_REJECTED` | 检查识别目标路径、consumer 角色、Sandbox HTTP 状态和响应中的上下文回显 |
| 消息未经过 5GC | 将端侧日志和抓包交给外部 AgentRuntime/MASQUE/5GC 维护方定位；SDK 不包含服务器转发实现 |
| 日志出现证书校验关闭警告 | 当前为封闭内测安全配置；不得将该构建用于生产网络 |

开发者运行测试：

```bash
python -m pip install -e '.[test]'
pytest -q
```
