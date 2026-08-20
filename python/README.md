# Agent Connect SDK（Linux/Python）客户使用指南

`agent-connect-sdk` 为 Linux 端 Agent 提供统一的控制面和数据面能力：应用只需要调用 SDK 函数，不需要感知对端 IP、TCP 端口、TUN 路由或 MASQUE 封装细节。

SDK 收到 AgentRuntime 透传的 `acf_group_config` 后，会自动缓存 `group_id + agent_id -> agent_ip + tcp_port + udp_port`，并自动维护对端 `/32` 或 `/128` 主机路由。应用调用 `send_message(group_id, target_agent_id, payload)` 时只传群组 ID 和目标 Agent ID。

## 1. 交付物和运行要求

建议向客户交付：

- `agent_connect_sdk-0.4.0-py3-none-any.whl`：SDK wheel。
- `examples/full_flow_demo.py`：不依赖真实网络的安装和全流程自检。
- `examples/linux_agent.py`：连接真实 AgentRuntime、TUN 和 MASQUE Proxy 的端侧常驻示例。
- `examples/masque-proxy.example.json`：服务器 MASQUE Proxy 配置模板。

运行环境：

- Linux x86_64 或 aarch64，Python 3.10 及以上。
- 端侧具备 `/dev/net/tun`，进程具有 root 或 `CAP_NET_ADMIN` 权限。
- 服务器侧已启动 UERANSIM，能够看到对应的 `uesimtun` 接口。
- 端侧可以通过 UDP 访问 MASQUE Proxy，默认示例端口为 `4433`。
- 端侧可以通过 HTTPS 访问 AgentRuntime。

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
dist/agent_connect_sdk-0.4.0-py3-none-any.whl
```

文件名中的发行名使用下划线是 Python wheel 的标准规范；安装和查询时的项目名仍是 `agent-connect-sdk`。

### 2.2 客户安装

客户不需要源码，直接安装 wheel：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ./agent_connect_sdk-0.4.0-py3-none-any.whl
```

确认安装结果：

```bash
python -c 'import agent_sdk; print(agent_sdk.__version__)'
python -m pip show agent-connect-sdk
agent-masque-proxy --help
```

`pip` 会自动安装 `aiohttp`、`aioquic`、`cryptography`、`httpx` 和 `pyroute2` 等依赖。如果客户环境不能访问公网，应同时交付依赖 wheel，并使用：

```bash
python -m pip install --no-index --find-links ./wheelhouse \
  ./agent_connect_sdk-0.4.0-py3-none-any.whl
```

发布方可以这样生成离线依赖目录：

```bash
python -m pip download --dest wheelhouse \
  ./dist/agent_connect_sdk-0.4.0-py3-none-any.whl
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

### 3.1 SDK 外：服务器准备 UERANSIM

先启动核心网和两个 UERANSIM UE，确认接口和地址：

```bash
ip -br address show uesimtun0
ip -br address show uesimtun1
```

预期分别包含设备 A、B 的 UE IP。不要在服务器上为两个 `uesimtun` 创建 Linux bridge、跨接口直连路由或 NAT 规则，否则报文可能绕过核心网 U 面。

### 3.2 SDK 外：准备 MASQUE TLS 证书

证书的 SAN 必须包含客户端使用的 `masque_server_name`，客户端必须信任签发该证书的 CA。示例：

```text
/etc/agent-sdk/masque-cert.pem
/etc/agent-sdk/masque-key.pem
/etc/agent-sdk/lab-ca.pem
```

服务器防火墙需要放行 QUIC 使用的 UDP 端口：

```bash
sudo ufw allow 4433/udp
```

如果不使用 `ufw`，请在实际防火墙中配置等价规则。

### 3.3 SDK 外：配置 MASQUE Proxy

复制 `examples/masque-proxy.example.json`，每台设备使用不同的不可预测 token：

```json
{
  "listen_host": "192.168.3.10",
  "listen_port": 4433,
  "certificate_path": "/etc/agent-sdk/masque-cert.pem",
  "private_key_path": "/etc/agent-sdk/masque-key.pem",
  "log_file_path": "/var/log/agent-sdk/masque-proxy.log",
  "log_level": "INFO",
  "log_max_bytes": 10485760,
  "log_backup_count": 5,
  "clients": [
    {
      "token": "replace-with-secret-for-device-a",
      "agent_ip": "8.8.8.7",
      "ue_interface": "uesimtun0",
      "allowed_peer_cidrs": ["8.8.8.8/32"],
      "mtu": 1280
    },
    {
      "token": "replace-with-secret-for-device-b",
      "agent_ip": "8.8.8.8",
      "ue_interface": "uesimtun1",
      "allowed_peer_cidrs": ["8.8.8.7/32"],
      "mtu": 1280
    }
  ]
}
```

字段含义：

| 字段 | 含义 |
|---|---|
| `token` | CONNECT-IP 会话凭据，必须和对应端侧配置一致 |
| `agent_ip` | 该会话允许使用的唯一内层源/目的 Agent IP |
| `ue_interface` | 该设备绑定的 UERANSIM TUN；A 必须进入 `uesimtun0`，B 必须进入 `uesimtun1` |
| `allowed_peer_cidrs` | 允许通信的对端 Agent 地址范围 |
| `mtu` | 内层 IP 包最大长度，必须和端侧一致 |

Proxy 日志写入 `log_file_path`，默认每个文件最大 10 MiB、保留 5 个历史文件。

服务器安装同一个 wheel 后启动：

```bash
sudo -E agent-masque-proxy --config /etc/agent-sdk/masque-proxy.json
```

该进程需要访问 `uesimtun*` 和 raw socket，通常使用 root 启动；生产环境可按安全策略授予最小 capability。Proxy 按 token 绑定唯一 `agent_ip + uesimtun`，不会根据用户输入临时选择接口。

### 3.4 SDK 内：端侧 `init` 配置

设备 A 的初始化示例：

```python
from pathlib import Path

result = await sdk.init(
    agent_runtime_ip="192.168.3.10",
    agent_runtime_port=8080,
    local_vlan_ip="192.168.1.10",
    local_tcp_port=4001,
    local_udp_port=28443,
    masque_server_url="https://192.168.3.10:4433",
    masque_server_name="masque.lab.example",
    masque_ca_certificate_pem=Path("/etc/agent-sdk/lab-ca.pem").read_bytes(),
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

本机 Agent TUN IP 也不由用户配置。`init` 内部调用 `POST /sdk/v1/endpoints`，AgentRuntime 在响应中返回 `ue_ip` 和 `ue_prefix_length`；SDK 验证后将其组合为 Agent TUN CIDR。例如设备 A 获得 `8.8.8.7/24`，设备 B 获得 `8.8.8.8/24`。可通过 `result.agent_tun_cidr` 查看服务器实际分配结果。

不要传对端 IP、端口或路由。SDK 在收到合法的 `acf_group_config` 后自动获得并维护这些信息。

`init` 参数说明：

| 参数 | 必填 | 说明 |
|---|---|---|
| `agent_runtime_ip` | 是 | AgentRuntime 物理网地址 |
| `agent_runtime_port` | 是 | AgentRuntime HTTPS 端口 |
| `local_vlan_ip` | 是 | 本设备物理网 IP；Runtime 回调从此地址进入 |
| `local_tcp_port` | 是 | 本地回调和 `/A2A/message` TCP 监听端口 |
| `local_udp_port` | 是 | 对外公布的 UDP 业务端口 |
| `masque_server_url` | 是 | MASQUE Proxy 的 HTTPS URL，底层使用 HTTP/3/QUIC |
| `masque_server_name` | 否 | TLS SNI/证书名称；省略时使用 URL 主机名 |
| `masque_ca_certificate_pem` | 否 | 私有 CA PEM；生产环境不能关闭证书校验 |
| `masque_authorization` | 否 | 推荐使用 `Bearer <device-token>` |
| `tun_name` | 否 | 默认 `agent_tun0` |
| `tun_mtu` | 否 | 默认 `1280`，应与 Proxy 配置一致 |
| `log_file_path` | 否 | 本地日志文件；默认 `./logs/agent-sdk.log` |
| `log_level` | 否 | `DEBUG/INFO/WARNING/ERROR/CRITICAL`，默认 `INFO` |
| `log_max_bytes` | 否 | 单个日志文件最大字节数，默认 10 MiB |
| `log_backup_count` | 否 | 轮转历史文件数量，默认 5 |

### 3.5 本地日志

SDK 使用 UTF-8 文本日志，每行包含时间、级别、logger 名称和一个 JSON 事件。默认记录：

- 所有公开 SDK 函数的 `function_enter`、`function_exit` 和 `function_error`，包括参数、返回类型/结果、错误码和耗时。
- SDK 发往 AgentRuntime、对端 Agent 的 HTTP 请求及其响应。
- AgentRuntime 群组通知、邀请和对端 `/A2A/message` 的 HTTP 入站请求及响应。
- 客户端和服务器端 HTTP/3 CONNECT-IP 请求、响应状态和协商结果。
- MASQUE Proxy 的启动和关闭。

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


sdk = AgentSdk(
    proof_verifier=my_group_config_verifier,
    control_request_authenticator=my_control_authenticator,
    message_signer=my_message_signer,
    message_signature_verifier=my_message_verifier,
)
sdk.register_network_message_listener(NetworkListener())
sdk.register_group_message_listener(GroupListener())
```

四个安全对象必须由部署方接入真实密钥和信任根。SDK 默认采用拒绝策略；`Demo*` 实现只能用于离线示例和联调环境。

### 4.2 身份、能力、发现和建群

```python
profile = await sdk.apply_identity(
    owner="customer-a",
    name="Agent A",
    public_key="did:key:...",
    description="RayNeo edge agent",
    metadata={"platform": "Ubuntu"},
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

AgentRuntime 随后把核心网通知原样转发到本机物理地址上的：

```text
POST /agent/group-moq-info
```

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
    task_type="video_rendering",
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

`examples/linux_agent.py` 用于真实网络联调。它会创建 TUN、连接 AgentRuntime 和 MASQUE Proxy，并保持本地消息服务运行：

```bash
sudo -E .venv/bin/python examples/linux_agent.py \
  --runtime-ip 192.168.3.10 \
  --runtime-port 8080 \
  --local-vlan-ip 192.168.1.10 \
  --agent-id 'did:example:agent-a' \
  --agent-name 'Agent A' \
  --masque-url https://192.168.3.10:4433 \
  --masque-server-name masque.lab.example \
  --masque-token 'replace-with-secret-for-device-a' \
  --ca-cert /etc/agent-sdk/lab-ca.pem \
  --log-file /var/log/agent-sdk/agent-a.log \
  --log-level INFO
```

该脚本中的 `DemoAcceptAllProofVerifier`、`DemoControlRequestAuthenticator` 和 `DemoMessageSigner` 仅为联调占位。生产发布前必须替换成对消息规范进行规范化、签名和验签的实现。

## 6. 函数清单

| 函数 | 用途 | 关键返回值 |
|---|---|---|
| `init(...)` | 创建 TUN、启动回调服务、注册端点并连接 MASQUE | `SdkInitResult` |
| `apply_identity(...)` | 申请身份，直接解析网元原始 `vc0` | `AgentProfile` |
| `set_local_profile_for_restore(profile)` | 恢复安全存储中的既有身份 | 无 |
| `deregister_identity(...)` | 注销身份 | `OperationResult` |
| `get_network_ability(...)` | 获取网络能力，直接解析原始 `vc1` | `NetworkAbility` |
| `register_capabilities(...)` | 注册 Agent Card/能力 | `OperationResult` |
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
| `RUNTIME_UNREACHABLE` | AgentRuntime IP/端口、HTTPS 证书和物理网络连通性 |
| `MASQUE_CONNECT_FAILED` | UDP 4433、防火墙、CA、SNI 和 token |
| `CONNECT_IP_NEGOTIATION_FAILED` | Proxy 是否支持 HTTP/3 Datagram 和 CONNECT-IP |
| `GROUP_NOT_ACTIVE` | 是否已收到并成功验签 `acf_group_config` |
| `TARGET_NOT_IN_GROUP` | `target_agent_id` 是否存在于该群组最新快照 |
| 消息未经过 5GC | token 到 `uesimtun` 映射是否正确；服务器是否存在 bridge/NAT/短路路由 |
| TLS 名称错误 | `masque_server_name` 是否出现在服务器证书 SAN 中 |

开发者运行测试：

```bash
python -m pip install -e '.[test]'
pytest -q
```
