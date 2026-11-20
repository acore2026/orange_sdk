# ACN AGW 三终端全流程测试

本示例在 A、B、C 三个独立 Linux 终端上运行真实 Python SDK，通过 `AgentSdk`
公共接口完成身份申请、Profile 注册、发现、建组、目录同步、多播接收和注销。
脚本不直接调用 ACF/Relay 管理接口、不注入群组配置或 ACL，也不把 L3 消息当作
AGW 消息成功。Android 端可按相同业务顺序调用对应 AGW API，本目录脚本使用 Python。

## 前置条件

- 核心网已部署 XUPF + ACN Relay，ACF 已启用授权同步，ACN/VN 扩展有效。
- 三端分别对应不同 UE/SUPI，完成 5G 接入，并具备相同 DNN（默认 `internet`）的
  活动 IPv4 PDU 会话。每端使用自己对应的 AgentRuntime HTTP 和 MASQUE 地址。
- 每端有 `/dev/net/tun` 和 root 或 `CAP_NET_ADMIN` 权限；同一主机模拟三端时，
  使用三个独立虚拟机、容器网络命名空间或等效隔离环境，不在一个共享网络空间
  同时运行三个 SDK。脚本不会启动 UE 或申请 PDU 会话。
- 运维提供实际 Relay N6 IPv4、UDP 端口和 UE 池回程路由。AGW 用户面必须经
  CONNECT-IP→Runtime→RAN→UPF→Relay 可达，不能用宿主机普通 IP 通信替代验收。
- IDM 信任当前 SDK 内置实验能力签发者。脚本将 VC0 和实验能力 VC 一并发布；
  A 还申请并发布网络能力 VC。若指定 `--test-vc-private-key`，相应公钥与签发者
  也需已在核心网信任库中配置。脚本不修改网侧信任配置或放宽验签。
- 使用本次改名后的 SDK，配置为 `AGWConfig`、`agw_config` 和 `*_agw_*` API。
  旧 L5 接口没有兼容别名。MOQT 编码及网侧配置项仍沿用当前核心网版本。

## 安装

三端均安装本项目配套的 `moq-transport` 和 SDK。源码环境示例：

```sh
python3 -m venv /opt/agw-sdk-venv
/opt/agw-sdk-venv/bin/python -m pip install '/path/to/moq-py'
/opt/agw-sdk-venv/bin/python -m pip install '/path/to/orange_sdk/sdks/python[agw]'
```

`moq-py` 默认安装 ACN 所需依赖；SDK 的 `[agw]` 仍为可选安装项，运行时由
`AGWConfig` 显式开启。安装操作不会启动 Relay。

`/path/to/` 替换为各终端实际源码位置，也可以安装配套 wheel。三端需同时复制
`agw_terminal.py` 及相应的 `agw_agent_a.py`、`agw_agent_b.py`、`agw_agent_c.py`，
包装脚本从同目录导入公共实现。使用 root 执行时明确指定此虚拟环境的 Python。

## 执行

在各终端进入 SDK 仓库的 `examples/python/agw/` 目录后执行以下命令。
运行入口、共用实现及本手册集中在此目录，自动化测试位于其 `tests/` 子目录。

每端先配置自己的接入地址，下面变量值由现场部署提供：

```sh
export RUNTIME_IP='192.168.1.10'
export RUNTIME_PORT='8088'
export MASQUE_URL='https://192.168.1.10:8443/.well-known/masque/ip'
export RELAY_N6_IP='172.30.0.20'
```

上述 IP/端口仅为示例，三端 Runtime/MASQUE 必须分别对应自己的 UE。Relay N6
地址三端相同，取核心网路由准备脚本实际输出；Relay 重建改变地址后需更新。

先在 B 端执行，等待打印 `PROFILE_READY`：

```sh
sudo /opt/agw-sdk-venv/bin/python agw_agent_b.py \
  --run-id acceptance-01 --runtime-ip "$RUNTIME_IP" --runtime-port "$RUNTIME_PORT" \
  --masque-url "$MASQUE_URL" --relay-ipv4 "$RELAY_N6_IP"
```

在 C 端执行，等待打印 `PROFILE_READY`：

```sh
sudo /opt/agw-sdk-venv/bin/python agw_agent_c.py \
  --run-id acceptance-01 --runtime-ip "$RUNTIME_IP" --runtime-port "$RUNTIME_PORT" \
  --masque-url "$MASQUE_URL" --relay-ipv4 "$RELAY_N6_IP"
```

最后在 A 端执行：

```sh
sudo /opt/agw-sdk-venv/bin/python agw_agent_a.py \
  --run-id acceptance-01 --runtime-ip "$RUNTIME_IP" --runtime-port "$RUNTIME_PORT" \
  --masque-url "$MASQUE_URL" --relay-ipv4 "$RELAY_N6_IP" --messages 3
```

三端 `--run-id` 必须一致，每轮使用新值。A 分别发现
`agw-demo/<run-id>/b` 和 `agw-demo/<run-id>/c` 技能，要求每个角色恰好对应一个
Agent；多候选时报错，不任意挑选。群组成员须恰好是当前批次的三个 Agent。
邀请的 `group_name` 匹配本轮后自动 ACCEPT；有效群组配置及时 ACK，随后才等待
AGW 就绪，避免 N-13 与 ACTIVE 授权相互等待。

## 目录与业务交换

| 发布方 | Track | 内容 | 订阅方 |
| --- | --- | --- | --- |
| A/B/C | `group_catalog` | SDK 自动生成的完整目录 | 其他成员的 SDK 自动订阅 |
| A | `status` | DATA、COMPLETE JSON Object | B、C |
| B、C | 各自的 `receipt` | READY、RECEIVED、COMPLETE_ACK | A |

Track 由 SDK 按当前 `group_id`、`group_version` 和发布者 Agent ID 构造版本化
Namespace。应用不手工编码 Namespace。每端通过公共目录查询确认另外两端的
业务 Track 已出现，再显式订阅需要的 Track。

B/C 定期发送 READY，直到开始收到数据，避免 READY 早于 A 的订阅而丢失。
A 在收到两端 READY 后，每条 DATA 只发布一次，并等待两端回传相同
`message_id` 的 RECEIVED。默认三条消息；发送成功只表示本地提交，双端业务
回执才构成该条多播验收成功。脚本检查发布者、角色、版本、批次及序号。

数据完成后，A 发送 COMPLETE，等待 B/C 的 COMPLETE_ACK，然后先注销 A。
B/C 等待网侧通知 A 已离开群组后再各自注销，避免某端提前退出使最后的回执
因成员版本变化而失效。不要求三端同时输入回车，也不共享目录、Agent ID 文件
或另建协调服务器。

## 预期输出、超时与清理

- 三端依次出现 `INITIALIZED`、`PROFILE_READY`、`GROUP_CONFIRMED`、
  `CATALOG_CONFIRMED`、`AGW_READY`。
- A 每条消息打印一次 `MULTICAST_CONFIRMED`，包含消息 ID 和两个接收方 ID；
  B/C 分别打印同一 ID 的 `MULTICAST_RECEIVED`。
- 每端正常注销后打印 `DEREGISTERED` 和 `SCENARIO_PASSED`，退出码 0。
  仅 A 发送成功或仅一端收到消息不算三端全流程通过。
- 默认整轮最多 180 秒（包括等待其他端），每轮消息/回执最多 30 秒。先启动的
  B/C 也计算整轮期限，应及时启动 A；可通过 `--timeout`、`--message-timeout`
  调整应用等待。SDK 自身到期保持 AGW 不可用的规则不变，脚本不偷偷重启其失败轮次。
- 失败退出码 1，Ctrl-C/SIGTERM 触发清理后退出码 130。Profile 注册失败、发现
  或目录超时等情况仍尝试注销已申请的身份并关闭 SDK；注销失败同样报告失败。
  断网时不能保证网侧清理完成，会保留 SDK 持久化状态用于定位和重试。
- SDK 日志默认写入 `logs/agw-<run-id>-<role>.log`；业务阶段和对象内容打印到
  控制台。可用 `--log-file` 指定文件，也可重定向控制台保留三端验收记录。
- 测试身份状态默认独立保存到
  `~/.local/state/agent-sdk/agw-demo/<run-id>/<role>/`。同批次重新启动时，只会注销
  同名测试身份后重试；不复用或删除普通应用的默认状态。不要与普通应用共用
  `--state-dir`。

可通过各脚本 `--help` 查看所有参数。B/C 默认只需同一批次、接入地址及 Relay
地址；不需要人工填写 A 的 Agent ID 或 group_id。此脚本连接实际部署环境，
本地自动测试中的模拟核心网边界不代表真实 NAS/UPF 验收已经完成。
