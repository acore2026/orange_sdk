# ACN AGW SDK 接入

AGW 是可选扩展，默认关闭。现有身份、发现、邀请、群组确认、L3 A2A、WebRTC
及 CONNECT-IP 接口保持独立；H-GROUP 成功和 N-13 ACK 不代表 AGW 已就绪。
应用显式选择 AGW，不自动双发或回退 L3。

SDK 中配置类统一为 `AGWConfig`，Python 构造参数为 `agw_config`，Android 为
`agwConfig`；公共接口分别使用 `*_agw_*` 和 `*AGW*` 命名。旧接口不提供兼容
别名。Python 可选安装项为 `[agw]`；升级时需要同时更新调用代码和 Android AAR。

本次为 SDK 命名调整，MOQT 编码、ACN NAS 和 ACF 管理协议保持现有格式。核心网
仍使用其已有配置项 `FREE6GC_ACF_L5_ENABLED` 等启用 Relay 授权同步，不能仅因
SDK 改名就把核心网环境变量替换为不存在的名字。

## 部署前提

- 核心网部署支持 ACN AGW 管理接口的 AGW Relay，ACF 开启 AGW；所选 PDU 会话
  的 UPF 具备到 Relay N6 的双向路径及 UE 地址池回程路由。
- 部署人员提供本轮实际 Relay N6 IPv4 与 UDP 端口。容器地址变化后重新生成
  配置并重新启动相关 SDK 业务；不新增 NAS 地址发现。
- 每个 SDK 对应一个 Agent，SDK 内部按角色维护 Publisher 与 Subscriber 两条
  MOQT 连接，多个群组共享各自角色连接。MOQT 流量复用现有 TUN/CONNECT-IP/PDU。
- MOQ 保持 `moq-00`、现有 SETUP 编码和 QUIC/TLS，客户端关闭服务证书校验。
  Agent 身份采用 ACF 同步成员与客户端 Agent ID 声明的样机信任模型。

## Python

先构建并安装本项目配套 `moq-py` 仓库的 `moq-transport` wheel，再安装 SDK 的
`[agw]` extra。未开启 AGW 的应用不需要安装 MOQ 包。

`moq-transport` 默认包含 ACN 所需依赖，无需指定 `[acn]`；安装包不会启动
Relay，普通 MOQ 与 AGW 仍由各自入口显式启动。

```python
from agent_sdk import AgentSdk, AGWConfig

sdk = AgentSdk(agw_config=AGWConfig(relay_ipv4="172.30.0.20", relay_port=4433))
sdk.register_agw_listener(lambda event: print(event))
# 按既有流程初始化、注册身份/Profile，注册网络消息监听器并建立 ACN 群组。
# SDK 收到有效群组配置后在后台建立两个角色连接、发布并订阅成员 group_catalog。
await sdk.register_agw_track(group_id, "status", "application/json")
await sdk.subscribe_agw_track(group_id, other_agent_id, "status")
await sdk.publish_agw_message(group_id, "status", {"state": "ready"})
```

`get_agw_catalog(group_id, publisher_agent_id)` 返回当前有效完整目录或 None。
`get_agw_state(group_id)` 分别提供角色方向、成员和 Track 的状态。
`unregister_agw_track` 撤销发布并更新自身目录；`unsubscribe_agw_track` 只取消指定订阅。
`retry_agw(group_id, direction=None)` 由应用显式开启失败工作的下一轮尝试；不重置
其他群组的到期状态，也不重新创建已经有效的共享角色连接。

## Android

```kotlin
val sdk = AgentSdk.create(
    vpnService,
    agwConfig = AGWConfig(relayIpv4 = "172.30.0.20", relayPort = 4433),
)
val registration = sdk.registerAGWListener { event -> /* 处理目录、消息及状态 */ }
// 先完成既有初始化和 ACN 群组流程。
sdk.registerAGWTrack(groupId, "status", "application/json")
sdk.subscribeAGWTrack(groupId, otherAgentId, "status")
sdk.publishAGWMessage(groupId, "status", buildJsonObject { put("state", "ready") })
```

对应查询与清理 API 为 `getAGWCatalog`、`getAGWState`、`unregisterAGWTrack`、
`unsubscribeAGWTrack` 和 `retryAGW`。Android 群组版本使用 ULong，支持完整 uint64
范围；同版本重放、失效及路由提交与 Python 语义一致。AGW 原生代码位于独立的
`sdks/android/native/masque_core/agw/` 包，通过专用 JNI 方法接入；不修改外层
CONNECT-IP 协议，也不新增 JVM QUIC 依赖。发布 AAR 前按既有脚本重建四种 ABI。

Android 的 `group_version` 和群组失效处理属于当前 ACN 基础契约，关闭 AGW 时也
执行，与 Python 和当前核心网保持一致；不兼容缺少 `group_version` 的旧配置。
有效群组配置或失效通知提交后，应用监听器异常只记录日志，仍返回 ACK，不回滚
成员或路由；相同配置重放不重复调用应用监听器。此行为不影响邀请阶段由应用决定
ACCEPT/REJECT。

## 目录、消息与状态

- `group_catalog` 为 SDK 保留名称，应用不能直接发布或订阅它。SDK 自动订阅群组
  其他成员的具体目录 Track，收到完整快照后向应用发出 `catalog` 事件。
- 业务 Track 名为 UTF-8，长度最多 128 字节，每群组每发布成员最多 64 条。
  目录按发布方完整快照更新，应用选择需要的具体业务 Track 订阅。
- 业务发送参数为 JSON Object，单 Object 默认最多 65536 字节，默认可靠 Stream。
  过短 JSON 仅追加合法空白，保持既有 MOQ 编码。显式 Datagram 超过协商/路径限制
  或无法确认限制时失败，不拆包、不自动切换 Stream。
- `ready` 和 `unavailable` 按群组、方向、成员和 Track 呈现，`message` 交付业务
  JSON。发送返回只表示本地提交或传输进展，应用业务 ACK、去重和超时由应用定义。
- 首次接入及故障恢复默认共享 30 秒总期限，局部等待默认 5 秒。到期后保持不可用，
  应用必须显式重试。配置重放、Relay/PDU 恢复不重新启动已到期工作。
- 群组版本更新重建该组目录及发布订阅，保留共享角色连接和其他群组；群组失效、
  注销及 SDK 关闭清理相应资源。SDK 重启不持久化或自动恢复历史群组。

## 回归验证

Python 的 `tests/test_agw.py` 使用真实本地 QUIC Relay 验证目录、消息、共享路由
和重试到期。原生 Go 测试可设置 `ACN_MOQ_PY_ROOT` 指向配套 `moq-py`，验证与
Python Relay 的真实双向 QUIC 互通。测试使用本地套接字，不替代 Android 真机
VPN、SDK→CONNECT-IP→RAN→UPF→Relay 的部署验收。

## A/B/C 真实终端测试

三个独立终端使用 `examples/python/agw/agw_agent_a.py`、`agw_agent_b.py` 和
`agw_agent_c.py`，每个进程都创建真实 `AgentSdk` 并配置 `AGWConfig`。该测试不
需要共享目录、A 手工输入 B/C 的 Agent ID、或另建协调服务。现场操作、完整参数、
日志及清理说明见 [A/B/C 全流程测试手册](../../examples/python/agw/AGW终端全流程测试.md)。

简要执行顺序：先在 B、C 上分别启动 `agw_agent_b.py` 和 `agw_agent_c.py`，再在 A
上启动 `agw_agent_a.py`；三端指定相同的新 `--run-id` 和本终端 Runtime/MASQUE
地址，以及三端共同使用的 Relay N6 IPv4。B/C 发布批次专属技能，A 分别按技能调用
`discover_agents`，再由 `create_group` 完成组网。三端通过正常邀请和群组配置流程
收到完整成员表，依靠 `group_catalog` 发布订阅流程发现业务 Track。

A 发布唯一 `message_id` 的 `DATA` 到 `status`，B/C 收到后分别在 `receipt` Track
回传 `RECEIVED`；A 等待双端业务回执后才报告该消息多播确认。完成后由 A 注销，
B/C 等待成员变更通知再注销。样例只接收本轮三名 Agent 成员并验证群组、版本、
发布者、Track、序号和回执；L3 A2A 日志不计作 AGW 成功。

测试使用 Python SDK 自带的实验能力凭证签发密钥；若核心网没有信任 SDK 随包实验
公钥，应设置 `--test-vc-private-key`，并先在核心网信任库登记对应公钥。该测试
配置仅限隔离实验，不用于生产。每端的运行状态放入以 run-id 和 role 分离的目录；
名称冲突时脚本拒绝注销不属于本测试的已有 Agent。异常和信号退出均尝试注销当前
测试身份、清理 AGW 会话及关闭 SDK，网路不可达导致的注销失败会作为测试失败输出。

## 真实终端 A/B/C 全流程

三个独立 Linux 终端使用 `examples/python/agw/agw_agent_a.py`、`agw_agent_b.py`、
`agw_agent_c.py`，经 `AgentSdk` 公共接口执行真实身份/Profile 注册、发现、建组、
目录同步、A→B/C 多播、接收回执和注销。三端不共享目录或手动传递 Agent ID；
每次指定相同且新的 `--run-id`，先启动 B/C，再启动 A。

部署、安装命令、参数和验收输出见
[A/B/C 终端测试说明](../../examples/python/agw/AGW终端全流程测试.md)。
