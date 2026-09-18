# N6 / DN Compute Sandbox Mock

这个服务模拟 `pruned_sandbox`（Sandbox Lite），部署在 free6GC 的 N6 数据网 `compose_n6`
中，固定地址为 `172.30.0.10`，UPF 的 N6 地址为 `172.30.0.2`。除“没有模型、接口返回
固定结果”之外，接口路径、请求/响应结构、校验规则、幂等语义和错误码都与
`pruned_sandbox` 一致；代码按 `pruned_sandbox` 的 `services/{asr,intent,sandbox,video}`
目录同构组织，便于对照。

## 端口与进程

与 `pruned_sandbox` 相同的四组监听（Mock 在单进程内启动，正式版由 Supervisor 守护
三个进程）：

| 地址 | 用途 |
| --- | --- |
| `http://172.30.0.10:28501` | CMF 管理面 |
| `http://172.30.0.10:28502` | N6 用户面 |
| `http://172.30.0.10:9004` | 独立 ASR（固定转写结果） |
| `http://127.0.0.1:8011` | Intent 内部服务（规则分类，仅容器内） |

管理路径不会注册到用户面端口，用户路径也不会注册到管理面端口。

## 接口

CMF 管理面（`28501`，`FREE6GC_COMPUTING_SANDBOX_MANAGEMENT_TOKEN` 配置 Bearer Token，
为空则不鉴权）：

- `GET /healthz`
- `POST /management/v1/compute-session-bindings:bind`
- `GET /management/v1/compute-session-bindings/{binding_ref}`
- `GET /management/v1/compute-session-bindings/{binding_ref}/media-state`
- `POST /management/v1/compute-session-bindings/{binding_ref}:unbind`

N6 用户面（`28502`，请求必须携带完整 `computing_context`，服务按 `binding_ref`
校验会话、实例、角色及 Agent；未绑定或已解绑返回 `409 binding-mismatch`）：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/media-connections` | producer/consumer WebRTC Offer/Answer |
| `DELETE` | `/v1/media-connections/{id}` | 幂等关闭媒体连接 |
| `PUT/GET` | `/v1/recognition-targets/{session_id}` | 写入和读取持续识别目标 |
| `POST` | `/v1/control-actions` | 提交文本或结构化控制动作 |
| `GET` | `/v1/control-actions/{action_id}` | 查询控制动作结果 |
| `POST` | `/v1/audio-control-actions` | 上传音频并返回转写和标准化动作 |
| `GET` | `/healthz`、`/debug/v1/sessions` | Orange 兼容健康检查和会话调试 |
| `POST` | `/video/v1/sessions/{session_id}/source` | Orange 兼容源 Offer/Answer |
| `POST` | `/video/v1/sessions/{session_id}/source/stop` | Orange 兼容停止源 |
| `POST` | `/video/v1/sessions/{session_id}/processed` | Orange 兼容处理流 |
| `GET/POST` | `/api/v1/detection/classes` | YOLO 扩展：查询/设置检测目标 |
| `GET` | `/health`、`/api/health` | 检测与帧处理详细状态 |

独立 ASR（`9004`）：

- `GET /health`、`GET /api/health`
- `POST /api/v1/transcribe`（multipart，返回固定转写和公共格式 `intent`）

Intent 内部服务（`8011`）：

- `GET /health`、`GET /api/health`
- `POST /api/v1/intent`、`POST /api/v1/semantic/route`

与 `pruned_sandbox` 一样，Mock 不提供 `/compute/v1/offloading-sessions`、
`.../consumers` 或强制删除会话接口；旧 Orange 路由按 `session_id` 即时创建进程内
媒体状态。

## 固定结果（无模型）

| 能力 | pruned_sandbox | Mock |
| --- | --- | --- |
| ASR 转写 | faster-whisper `whisper-large-v3` | 固定返回 `MOCK_AUDIO_TRANSCRIPTION_TEXT`（默认 `向左转`） |
| 意图分类 | Qwen2.5-0.5B（hybrid，失败回退规则） | 与正式版相同的规则分类，`backend=rules` |
| 目标检测 | `yolov8s-worldv2.pt` | 左上角紫色块 + 移动绿条标记帧；对每个激活类别返回固定检测（confidence `0.99`） |
| `search_object` | 真实 YOLO 检测匹配 | 帧流动时对查询词立即返回固定匹配；无源帧时与正式版一致等待超时返回空 |
| 机器狗动作转发 | `SANDBOX_PRODUCER_CONTROL_URL` | 相同转发逻辑；未配置时 `FAILED`，cause `producer-control-endpoint-unconfigured` |

控制动作生命周期与正式版一致：`202` 返回 `ACCEPTED`，异步执行进入 `RUNNING`，
最终 `COMPLETED`（`search_object`）、`FAILED`（转发失败/未配置）或 `CANCELLED`
（解绑）。语音未命中控制意图时 `control_triggered=false`、`status=COMPLETED`、
cause `no-control-action`。

## 数据流

1. CMF/AgentRuntime 调用 `28501` 的 `compute-session-bindings:bind` 建立绑定
   （`configuration` 含 `connection_parameters` 与 `participants_network_facts`，
   `configuration_digest` 为 canonical JSON SHA-256），随后分别向 A 和 B 下发 C-02；
   测试部署应把 `service_endpoint` 配成 `http://172.30.0.10:28502`，把
   `media_connections_path` 配成 `/v1/media-connections`。
2. A 只通过 `sendMessage` 把 `compute_service_session_id` 发给 B。两端的 Sandbox 地址、
   `binding_ref`、角色和媒体路径都来自各自 SDK 内部缓存的 C-02。
3. A 调用 `getProcessedVideoStream(sessionId)`，以 consumer 身份主动提交非 Trickle Offer；
   B 调用 `startVideoUpload(sessionId)`，以 producer 身份提交 Offer。Mock 分别返回 Answer。
4. consumer 可以先建连并接收占位帧。Mock 收到 B 的第一帧后，在原下行 Track 内切到处理帧。
5. 停止媒体时 SDK 调用 `DELETE /v1/media-connections/{media_connection_id}`；重复删除仍返回 204。
6. CMF 调用 `28501` 的 `:unbind` 释放绑定；Mock 关闭该绑定的媒体管线、清理识别目标，
   并把执行中的动作置为 `CANCELLED`。解绑后用户面请求返回 `409 binding-mismatch`。

Mock 会在视频左上角添加紫色块和移动绿条，便于确认接收到的是服务端处理后的流。

正式媒体接口为 `POST /v1/media-connections`。请求包含
`request_id + computing_context + offer`，成功返回 HTTP 201，并原样回显请求 ID 和上下文，
同时返回 `media_connection_id + answer`。同一 `binding_ref` 的 producer 和 consumer
各允许一个活动连接；同请求重试复用原连接，不同载荷复用同一请求 ID 返回 409。

## 运行配置

环境变量与 `pruned_sandbox` 同名（含 `MOCK_*` 兼容回退）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SANDBOX_MANAGEMENT_HOST/PORT` | `0.0.0.0` / `28501` | CMF 管理面 |
| `SANDBOX_USER_PORT` | `28502` | N6 用户面（兼容 `VIDEO_PORT`/`MOCK_VIDEO_PORT`） |
| `VIDEO_HOST` | `0.0.0.0`（兼容 `MOCK_LISTEN_HOST`） | 用户面监听地址 |
| `VIDEO_PUBLIC_IP` | `127.0.0.1`（兼容 `MOCK_VIDEO_SERVER_IP`） | 对外通告地址 |
| `ASR_HOST/PORT` | `0.0.0.0` / `9004` | 独立 ASR |
| `INTENT_HOST/PORT` | `127.0.0.1` / `8011` | 内部 Intent |
| `SANDBOX_ASR_URL` | `http://127.0.0.1:9004/api/v1/transcribe` | 运行期语音动作调用的 ASR |
| `SANDBOX_INTENT_URL` | `http://127.0.0.1:8011/api/v1/intent` | 不可用时回退进程内规则 |
| `FREE6GC_COMPUTING_SANDBOX_MANAGEMENT_TOKEN` | 空 | 管理接口 Bearer Token；为空仅适用于本地开发 |
| `SANDBOX_PRODUCER_CONTROL_URL` | 空 | 机器狗业务控制端点模板，支持 `{ue_ipv4_address}` 等占位符 |
| `MOCK_AUDIO_TRANSCRIPTION_TEXT` | `向左转` | Mock 固定转写文本 |
| `VIDEO_FPS`、`VIDEO_WIDTH`、`VIDEO_HEIGHT` | `30`、`640`、`480` | 处理流输出 |
| `VIDEO_H264_RTP_PAYLOAD_BYTES` | `1150`（兼容 `MOCK_VIDEO_H264_RTP_PAYLOAD_BYTES`，范围 1100..1150） | H.264 FU-A RTP payload 上限 |
| `LOG_DIR`、`LOG_MAX_BYTES`、`LOG_BACKUP_COUNT` | 空、10 MiB、5 | 滚动日志 |

## 部署

### 离线整包启动

交付文件 `sandbox.tar.gz` 已包含启动脚本、Compose 配置、ARM64 镜像归档与校验文件、
完整源码及烟测程序。复制到 N6 ARM64 主机后执行：

```bash
sha256sum -c sandbox.tar.gz.sha256
tar -xzf sandbox.tar.gz
cd sandbox
./start_sandbox.sh
```

若原来的 `agent-sdk-mock-video-server` 容器仍在运行或已经停止，脚本会删除旧容器并通过
Compose 强制重建。不存在 `compose_n6` 时，脚本会创建
`172.30.0.0/24` Bridge 网络。需要在启动后执行完整 HTTP/WebRTC 接口烟测时使用：

```bash
./start_sandbox.sh --smoke
```

### 宿主机防火墙放行

容器健康后脚本会自动添加幂等的放行规则：Docker 29 的 nftables 后端在
`raw PREROUTING` 为每个容器 IP 生成防伪 DROP（非本网桥入口即丢弃），跨网桥
FORWARD 也默认隔离；CMF 等核心网组件位于其他网桥时将无法访问 Sandbox 的
`28501/28502/9004`。脚本按 Sandbox 容器 IP 精确限定，在 `raw PREROUTING`
顶部与 `DOCKER-USER` 链首插入双向 ACCEPT（重复执行不会叠加）。主机重启或
Docker 重建网络后重新执行 `./start_sandbox.sh` 即可恢复；
`SANDBOX_SKIP_FIREWALL=1` 可跳过该步骤。

烟测覆盖：管理面 bind/查询/media-state/unbind、Bearer Token、producer/consumer 真实
WebRTC 建连、占位帧到处理帧切换、幂等重放与 409 冲突、识别目标、文本搜索动作、
独立 ASR 与运行期语音动作、解绑后 409。

在仓库中重新生成完整交付包：

```bash
cd /root/lpx/sdk/mock-video-server
./package-sandbox.sh
```

输出为 `dist/sandbox.tar.gz` 和 `dist/sandbox.tar.gz.sha256`。

### 构建镜像

构建 ARM64 镜像、执行完整 HTTP/WebRTC 烟测并导出镜像。在 x86_64 构建机上需要先启用
Docker/QEMU 的 ARM64 binfmt 支持：

```bash
cd /root/lpx/sdk/mock-video-server
./build-image.sh
```

生成：

```text
镜像：agent-compute-sandbox-mock:0.3.0-arm64
平台：linux/arm64
归档：dist/compute-mock/agent-compute-sandbox-mock-0.3.0-linux-arm64.tar.gz
校验：dist/compute-mock/agent-compute-sandbox-mock-0.3.0-linux-arm64.tar.gz.sha256
```

N6 主机离线导入后再部署：

```bash
sha256sum -c agent-compute-sandbox-mock-0.3.0-linux-arm64.tar.gz.sha256
gzip -dc agent-compute-sandbox-mock-0.3.0-linux-arm64.tar.gz | docker load
```

```bash
cd /root/lpx/sdk/mock-video-server
docker compose -f docker-compose.n6.yml up -d
docker compose -f docker-compose.n6.yml ps
docker exec agent-sdk-mock-video-server ip route
curl http://172.30.0.10:28501/healthz
curl http://172.30.0.10:28502/healthz
python3 smoke_client.py \
  --management-url http://172.30.0.10:28501 \
  --base-url http://172.30.0.10:28502 \
  --asr-url http://172.30.0.10:9004
```

容器内必须保留以下回程路由，否则 UE 发出的请求能到 DN，但响应无法返回 UE：

```text
10.60.0.0/16 via 172.30.0.2
10.61.0.0/16 via 172.30.0.2
```

调试会话状态：

```bash
curl http://172.30.0.10:28502/debug/v1/sessions
docker logs -f agent-sdk-mock-video-server
```

Sandbox HTTP 用户面不使用业务层 token、ticket 或 proof；Agent 和绑定关系由
网侧通过管理面建立，Mock 根据完整 `computing_context` 关联媒体、识别和控制请求。
`/debug/v1/sessions` 的 `consumers.consumer-N` 会返回该连接的
`frames_processed`、`placeholder_frames_sent`、`packets_sent`、`bytes_sent`、
`codec`、`first_frame_sent` 和 `first_source_frame_sent`。`keyframes_requested`
记录 Answer 就绪、consumer 建连以及占位流切换 source 后主动补发的关键帧请求次数。
这些是连接生命周期内保留的单调计数；即使 PeerConnection
随后关闭，也能用来判断 Server 是否真正处理并发出了媒体。Server 日志还会为每个
consumer 连接单独输出首个占位/媒体帧和首个真实 source 帧日志。Server 默认以固定
`640x480@30fps` 输出、只保留最新 source 帧。正式 U-MEDIA 路径按双方 Offer 协商
可用视频编码，并在建连后主动请求启动关键帧；旧 source 路由仍保留原 H.264 High
Profile 限制。`source_codec` 与 `source_keyframes_requested` 可用于确认上行协商和恢复状态。

## 测试

```bash
cd /root/lpx/sdk/mock-video-server
python3 -m pytest tests -q
```

测试覆盖：管理/用户面路由隔离与鉴权、bind/unbind 幂等与冲突、上下文校验、
真实 aiortc WebRTC 端到端（consumer 先建链、占位帧、H.264 High 源 Answer、
处理帧无重协商切换、RTP payload 限制、media-state 观测）、识别目标与搜索固定
结果、语音动作全链路（ASR→意图→控制状态机）以及日志脱敏和滚动。
