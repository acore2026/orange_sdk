# Agent Connect SDK ARM64 镜像

该镜像面向 `linux/arm64`（aarch64），包含当前工作区的 Linux/Python SDK 源码、
Python 运行依赖、网络诊断工具，以及 A/B 两个角色的启动脚本。Android AAR 不在
Linux 容器中运行。

## 在 x86 主机构建

```bash
cd /root/lpx/sdk/python
./docker/arm64/build-image.sh
```

默认生成：

```text
镜像：agent-connect-sdk:0.17.9-arm64
归档：dist/arm64/agent-connect-sdk-0.17.9-linux-arm64.tar.gz
校验：dist/arm64/agent-connect-sdk-0.17.9-linux-arm64.tar.gz.sha256
```

ARM64 目标机导入：

```bash
sha256sum -c agent-connect-sdk-0.17.9-linux-arm64.tar.gz.sha256
gzip -dc agent-connect-sdk-0.17.9-linux-arm64.tar.gz | docker load
docker image inspect --format '{{.Os}}/{{.Architecture}}' agent-connect-sdk:0.17.9-arm64
```

## 同一宿主机运行 A 和 B

A、B 必须各自运行在独立容器网络命名空间中，不能使用 `--network host`。否则两者
会共享宿主机的 TUN、路由和端口，`10.60.0.2` 与 `10.60.0.3` 还可能被宿主机直接
路由，绕过 CONNECT-IP 和 UPF。

随包提供的 `docker-compose.same-host.yml` 创建专用 bridge：

```text
Agent A 容器 172.29.100.2 -> 宿主机 172.29.100.1:8088 / UDP 8443
Agent B 容器 172.29.100.3 -> 宿主机 172.29.100.1:8089 / UDP 8444
```

容器内的 `10.60.0.2/32`、`10.60.0.3/32` 由各自 Runtime 的
`GET /v1/ue/info` 返回，SDK 再在各自容器里创建 TUN。宿主机没有这些内层地址；
宿主机只承载发往 MASQUE 的外层 QUIC/UDP，因此 A2A 数据仍由对应 UE PDU Session
送入核心网和 UPF。

Runtime 必须监听宿主机可达地址（例如 `0.0.0.0:8088`、`0.0.0.0:8089`），MASQUE
必须监听并发布 UDP `8443`、`8444`；只监听 `127.0.0.1` 时 bridge 容器无法访问。
Agent B 默认循环读取镜像内
`/opt/agent-sdk/examples/assets/video-offload-test.mp4`，不要求宿主机提供摄像头设备。

目标 ARM 主机只需执行两个宿主机脚本。建议先启动 B，再启动 A：

```bash
./start-agent-b.sh fresh-register
./start-agent-a.sh fresh-register
```

脚本会在缺少 `.env` 时从 `same-host.env.example` 自动创建，校验 Compose 配置，按需校验并
导入同目录或 `../../dist/arm64` 中的 ARM64 镜像归档，然后创建网络、重建并启动对应容器。
如果原容器仍在运行或已经停止，都会重建并重新启动。脚本分别等待 `B_READY` 和
`GROUP_CREATED`，失败时直接打印容器日志并返回非零状态。因此正常启动不再需要手工执行
`docker compose create/up/logs`、`docker network inspect` 或 `docker run curl`。

首次运行前如需修改地址或网段，可以先执行 `cp same-host.env.example .env` 并编辑 `.env`；
如果默认配置适用，直接运行启动脚本即可。`AGENT_IMAGE_ARCHIVE` 可显式指定镜像归档，
`AGENT_START_TIMEOUT` 可修改就绪等待秒数。

AgentRuntime 地址就是在 `.env` 中指定：A 默认使用
`AGENT_A_RUNTIME_IP=172.29.100.1`、端口 `8088`；B 使用相同宿主机 bridge 网关、
端口 `8089`。对应 MASQUE URL 分别为 UDP `8443` 和 `8444`。
`0.0.0.0` 只是 Runtime 的服务端监听通配地址，不能作为 SDK 的目标地址。
`172.29.100.1` 只存在于运行这些容器的目标 ARM 主机，其他机器无法直接访问；
ICMP ping 也不是 Runtime 可用性的判断标准，应以上面的容器内 HTTP 请求为准。

检查隔离是否生效：

```bash
docker exec agent-sdk-a ip -brief address
docker exec agent-sdk-b ip -brief address
docker exec agent-sdk-a ip route get 10.60.0.3
docker exec agent-sdk-b ip route get 10.60.0.2
```

前两个命令应显示不同的 bridge 地址和各自 TUN；后两个命令必须指向各自容器内的
`agent_tun0`，宿主机上不应出现 `10.60.0.2/32` 或 `10.60.0.3/32` 的本地接口。

`start-agent-a.sh` 和 `start-agent-b.sh` 是宿主机 Compose 启动器，注册模式是必填参数：

```bash
./start-agent-a.sh fresh-register
./start-agent-b.sh fresh-register
# 或跳过网侧注销，直接把本地生命周期硬重置到状态1：
./start-agent-a.sh force-register
./start-agent-b.sh force-register
```

`fresh-register` 会正常停止原容器，使其先注销当前身份；新容器再注销状态卷中恢复出的
遗留身份并重新注册。`force-register` 会强制停止并删除原容器，避免退出钩子发送注销，
新容器只调用本地 `reset_agent()` 清除 Profile/Card 状态，再从状态1注册。容器镜像内部的
`run-agent-a.sh` 和 `run-agent-b.sh` 负责把模式及环境变量转换为 Python 示例参数。
`AGENT_RUNTIME_IP` 必须是容器可达的 Runtime IPv4 地址。CONNECT-IP 外层源地址由
容器内的系统路由自动选择，不再配置 `LOCAL_VLAN_IP`。

通用可选变量包括 `AGENT_TCP_PORT`、`AGENT_UDP_PORT`、`AGENT_TUN_NAME`、
`AGENT_TUN_MTU`、`AGENT_NAME`、`AGENT_OWNER`、`AGENT_REGION`、
`AGENT_PRIORITY`、`AGENT_LOG_FILE`、`AGENT_LOG_LEVEL` 和
`AGENT_REGISTRATION_MODE`、`AGENT_FORCE_REGISTRATION`、`AGENT_FRESH_REGISTRATION`、
`AGENT_DEREGISTER_ON_EXIT`、`AGENT_FULL_INTERFACE_SUITE`。A 还支持
`AGENT_TARGET_ID`、`AGENT_MESSAGE_JSON`、`AGENT_GROUP_NAME`、
`AGENT_COMPUTE_CAPABILITY_ID`、CPU/内存/GPU/镜像等正式算力约束、
`AGENT_COMPUTE_TERMINAL_ACTION`、`AGENT_SANDBOX_TIMEOUT`、
`AGENT_RECOGNITION_TARGET`、`AGENT_CONTROL_TEXT` 和
`AGENT_PROCESSED_FRAME_COUNT`。B 还支持
`AGENT_WAIT_TIMEOUT`、`AGENT_VIDEO_SOURCE`、`AGENT_VIDEO_FILE`、
`AGENT_LOOP_VIDEO`、`AGENT_VIDEO_WIDTH`、
`AGENT_VIDEO_HEIGHT`、`AGENT_VIDEO_FPS`、`AGENT_VIDEO_BITRATE_KBPS`、
`AGENT_MAX_SESSIONS`、`AGENT_UPLOAD_CONTROL_DELAY` 和
`AGENT_THIRD_PARTY_PRIVATE_KEY`。

默认 `AGENT_FULL_INTERFACE_SUITE=true`。镜像通过 A/B 两个真实端侧进程覆盖当前
Python SDK 的全部公开接口：

- 初始化、三个状态属性、两个下行 listener、身份申请、Profile 恢复、本地 Reset、注销；
- 网络能力、Agent Card 注册与增量更新、Discovery、建组、群组快照和 A2A 消息；
- 独立 CREATE+CANCEL 探针，以及主会话 CREATE、QUERY、RELEASE 和 C-05 关闭等待；
- Sandbox 识别目标 PUT/GET、文本/结构化控制动作 POST/GET；
- 处理流 `recv/close` 和上传流 `pause/resume/stop`。

A 的身份 Reset 探针会先恢复临时 Profile 再执行网侧注销，不会遗留临时身份；所有算力
会话都在正式身份注销之前完成 CANCEL 或 RELEASE。主会话只把
`compute_service_session_id` 通过 A2A 发给 B。B 默认循环读取镜像内测试 MP4，A 收到
处理帧后发送 RELEASE。成功日志还包含 `IDENTITY_LIFECYCLE_PROBE_VERIFIED`、
`COMPUTING_CANCEL_PROBE_VERIFIED`、`RECOGNITION_TARGET_VERIFIED`、
`CONTROL_ACTIONS_VERIFIED`、`VIDEO_UPLOAD_PAUSED/RESUMED/STOP_VERIFIED`。

如需改用真实摄像头，将 `AGENT_VIDEO_SOURCE=camera`，并在 compose 的 Agent B
`devices` 中增加 `/dev/video0:/dev/video0`；`AGENT_CAMERA_ID`、分辨率和帧率仅在该模式
生效。自定义本地视频应挂载进容器，并将 `AGENT_VIDEO_FILE` 指向容器内路径。

主机启动器将命令行模式写入 `AGENT_REGISTRATION_MODE` 后启动 A/B，同时保留
`AGENT_FRESH_REGISTRATION=true` 和
`AGENT_DEREGISTER_ON_EXIT=true`。每次启动时，如果状态卷中存在上次测试的身份，脚本
先向网侧注销该身份；只有注销成功才重新执行身份申请、网络能力获取和 Agent Card
发布。正常退出或收到 Docker 的 `SIGTERM` 时也会注销本次身份。旧状态卷必须保留到
新版容器至少成功启动一次，不能在升级前直接删除，否则会失去注销网侧遗留身份所需
的 Agent ID。需要临时恢复旧的复用行为时，必须显式设置
`AGENT_FRESH_REGISTRATION=false`；测试验收不应这样设置。

需要直接丢弃本地 Profile/Card 状态并重新申请身份、且不向网侧注销旧身份时，执行
`./start-agent-a.sh force-register` 或 `./start-agent-b.sh force-register`。启动器会绕过旧
容器的注销钩子，容器内脚本再传入 `--force-registration` 并调用 SDK 的本地
`reset_agent()`。
默认 Discovery skill 是意图服务 `executor` 对应的 `robot dog`；正式算力 capability
保持为 `dog-vision`。两者分别配置，不能互相替代。只需回归旧视频主链路时可设置
`AGENT_FULL_INTERFACE_SUITE=false`。

身份、Agent 状态和自动生成的 TLS 私钥保存在 `/var/lib/agent-sdk`，A、B 使用独立
持久卷；日志保存在各自 `/var/log/agent-sdk` 卷中。停止部署使用：

```bash
docker compose --env-file .env -f docker-compose.same-host.yml down
```

## 升级到 0.17.9

升级前保留 `agent-a-state` 和 `agent-b-state` 卷，让新版容器第一次启动时可以读取旧
Agent ID 并注销网侧遗留身份。不要使用 `docker compose down -v`。

```bash
docker compose --env-file .env -f docker-compose.same-host.yml \
  down --remove-orphans
docker image rm agent-connect-sdk:0.17.8-arm64

sha256sum -c agent-connect-sdk-0.17.9-linux-arm64.tar.gz.sha256
gzip -dc agent-connect-sdk-0.17.9-linux-arm64.tar.gz | docker load
docker image inspect --format '{{.Os}}/{{.Architecture}}' \
  agent-connect-sdk:0.17.9-arm64
```

将 `.env` 中的 `AGENT_IMAGE` 更新为 `agent-connect-sdk:0.17.9-arm64`，并使用本版本
随附的 Compose 文件和宿主机启动脚本。随后直接重启两个服务：

```bash
./start-agent-b.sh fresh-register
./start-agent-a.sh fresh-register
```

只有在日志确认旧身份和本轮身份均已成功注销、且明确不再需要故障恢复状态时，才可以
额外删除状态卷。
