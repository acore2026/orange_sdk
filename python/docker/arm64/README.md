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
镜像：agent-connect-sdk:0.17.6-arm64
归档：dist/arm64/agent-connect-sdk-0.17.6-linux-arm64.tar.gz
校验：dist/arm64/agent-connect-sdk-0.17.6-linux-arm64.tar.gz.sha256
```

ARM64 目标机导入：

```bash
sha256sum -c agent-connect-sdk-0.17.6-linux-arm64.tar.gz.sha256
gzip -dc agent-connect-sdk-0.17.6-linux-arm64.tar.gz | docker load
docker image inspect --format '{{.Os}}/{{.Architecture}}' agent-connect-sdk:0.17.6-arm64
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

```bash
cp same-host.env.example .env
# 如果 172.29.100.0/24 与现网冲突，先整体修改 .env 中的网段和地址。

# 创建容器和专用网络但暂不启动，然后读取目标 ARM 主机上的实际网关。
docker compose --env-file .env -f docker-compose.same-host.yml create
docker network inspect agent-sdk-access \
  --format '{{range .IPAM.Config}}subnet={{.Subnet}} gateway={{.Gateway}}{{end}}'

# 从同一个 Docker 网络验证 Runtime；返回 /v1/ue/info JSON 才算可达。
docker run --rm --network agent-sdk-access \
  agent-connect-sdk:0.17.6-arm64 \
  curl -fsS http://172.29.100.1:8088/v1/ue/info

# 先启动 B。logs --tail 读取已有日志后立即返回，不会占住当前终端。
docker compose --env-file .env -f docker-compose.same-host.yml up -d agent-b
docker compose --env-file .env -f docker-compose.same-host.yml logs --tail=100 agent-b

# 看到 B_READY 后启动 A，然后持续跟随算力会话和视频日志。
docker compose --env-file .env -f docker-compose.same-host.yml up -d agent-a
docker compose --env-file .env -f docker-compose.same-host.yml logs -f agent-a agent-b
```

`logs -f` 只是前台跟随日志；按 `Ctrl+C` 只会停止跟随，不会停止已用
`up -d` 启动的容器。需要再次查看日志时，重新执行最后一条命令即可。

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

单容器直接运行时，`start-agent-a.sh` 和 `start-agent-b.sh` 仍通过环境变量接收配置。
`AGENT_RUNTIME_IP` 必须是容器可达的 Runtime IPv4 地址。CONNECT-IP 外层源地址由
容器内的系统路由自动选择，不再配置 `LOCAL_VLAN_IP`。

通用可选变量包括 `AGENT_TCP_PORT`、`AGENT_UDP_PORT`、`AGENT_TUN_NAME`、
`AGENT_TUN_MTU`、`AGENT_NAME`、`AGENT_OWNER`、`AGENT_REGION`、
`AGENT_PRIORITY`、`AGENT_LOG_FILE`、`AGENT_LOG_LEVEL` 和
`AGENT_FORCE_REGISTRATION`、`AGENT_FRESH_REGISTRATION`、
`AGENT_DEREGISTER_ON_EXIT`。A 还支持
`AGENT_TARGET_ID`、`AGENT_MESSAGE_JSON`、`AGENT_GROUP_NAME`、
`AGENT_COMPUTE_CAPABILITY_ID`、CPU/内存/GPU/镜像等正式算力约束、
`AGENT_COMPUTE_TERMINAL_ACTION` 和 `AGENT_PROCESSED_FRAME_COUNT`。B 还支持
`AGENT_WAIT_TIMEOUT`、`AGENT_VIDEO_SOURCE`、`AGENT_VIDEO_FILE`、
`AGENT_LOOP_VIDEO`、`AGENT_VIDEO_WIDTH`、
`AGENT_VIDEO_HEIGHT`、`AGENT_VIDEO_FPS`、`AGENT_VIDEO_BITRATE_KBPS`、
`AGENT_MAX_SESSIONS` 和 `AGENT_THIRD_PARTY_PRIVATE_KEY`。

默认验收流程中，A 创建并查询算力会话，只把 `compute_service_session_id` 发给 B；
B 收到后循环读取镜像内的测试 MP4 并调用 `start_video_upload()`；A 收到一个处理帧后
发送 RELEASE；
双方内部处理 C-05，B 观察上传句柄进入 `STOPPED` 后退出。日志成功事件依次包含 A 的
`COMPUTING_SESSION_CREATED`、`PROCESSED_VIDEO_FRAME`、
`COMPUTING_SESSION_TERMINATED`，以及 B 的 `VIDEO_UPLOAD_STARTED`、
`COMPUTING_SESSION_CLOSED`。

如需改用真实摄像头，将 `AGENT_VIDEO_SOURCE=camera`，并在 compose 的 Agent B
`devices` 中增加 `/dev/video0:/dev/video0`；`AGENT_CAMERA_ID`、分辨率和帧率仅在该模式
生效。自定义本地视频应挂载进容器，并将 `AGENT_VIDEO_FILE` 指向容器内路径。

ARM 测试镜像默认设置 `AGENT_FRESH_REGISTRATION=true` 和
`AGENT_DEREGISTER_ON_EXIT=true`。每次启动时，如果状态卷中存在上次测试的身份，脚本
先向网侧注销该身份；只有注销成功才重新执行身份申请、网络能力获取和 Agent Card
发布。正常退出或收到 Docker 的 `SIGTERM` 时也会注销本次身份。旧状态卷必须保留到
新版容器至少成功启动一次，不能在升级前直接删除，否则会失去注销网侧遗留身份所需
的 Agent ID。需要临时恢复旧的复用行为时，必须显式设置
`AGENT_FRESH_REGISTRATION=false`；测试验收不应这样设置。

需要直接丢弃本地 Profile/Card 状态并重新申请身份、且不向网侧注销旧身份时，设置
`AGENT_FORCE_REGISTRATION=true`。该变量优先于默认的
`AGENT_FRESH_REGISTRATION=true`，因此无需同时修改后者；启动脚本会传入
`--force-registration` 并调用 SDK 的本地 `reset_agent()`。
容器和命令行脚本的默认发现、发布及算力能力均为 `dog-vision`，与 Android 测试
App 一致。

身份、Agent 状态和自动生成的 TLS 私钥保存在 `/var/lib/agent-sdk`，A、B 使用独立
持久卷；日志保存在各自 `/var/log/agent-sdk` 卷中。停止部署使用：

```bash
docker compose --env-file .env -f docker-compose.same-host.yml down
```

## 从 0.17.5 卸载并重装 0.17.6

升级前保留 `agent-a-state` 和 `agent-b-state` 卷，让新版容器第一次启动时可以读取旧
Agent ID 并注销网侧遗留身份。不要使用 `docker compose down -v`。

```bash
docker compose --env-file .env -f docker-compose.same-host.yml \
  down --remove-orphans
docker image rm agent-connect-sdk:0.17.5-arm64

sha256sum -c agent-connect-sdk-0.17.6-linux-arm64.tar.gz.sha256
gzip -dc agent-connect-sdk-0.17.6-linux-arm64.tar.gz | docker load
docker image inspect --format '{{.Os}}/{{.Architecture}}' \
  agent-connect-sdk:0.17.6-arm64
```

将 `.env` 中的 `AGENT_IMAGE` 更新为 `agent-connect-sdk:0.17.6-arm64`，并使用本版本
随附的 `docker-compose.same-host.yml`。展开配置后应看到 A/B 的 fresh 和退出注销开关
都为 `true`：

```bash
docker compose --env-file .env -f docker-compose.same-host.yml config | \
  grep -E 'image:|AGENT_FRESH_REGISTRATION|AGENT_DEREGISTER_ON_EXIT'

docker compose --env-file .env -f docker-compose.same-host.yml up -d agent-b
docker compose --env-file .env -f docker-compose.same-host.yml logs -f agent-b
# B_READY 后，在另一个终端启动 A：
docker compose --env-file .env -f docker-compose.same-host.yml up -d agent-a
```

只有在日志确认旧身份和本轮身份均已成功注销、且明确不再需要故障恢复状态时，才可以
额外删除状态卷。
