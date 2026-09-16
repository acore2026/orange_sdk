# N6 / DN Compute Sandbox Mock

这个服务模拟正式算力会话中的 Sandbox，部署在 free6GC 的 N6 数据网 `compose_n6` 中。
固定地址为 `172.30.0.10:28500`，UPF 的 N6 地址为 `172.30.0.2`。同一镜像同时实现
U-MEDIA、持续视觉识别目标、文本/结构化控制动作和 multipart 语音控制动作。

正式接口包括：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/media-connections` | producer/consumer WebRTC Offer/Answer |
| `DELETE` | `/v1/media-connections/{id}` | 幂等关闭媒体连接 |
| `PUT/GET` | `/v1/recognition-targets/{session_id}` | 写入和读取持续识别目标 |
| `POST` | `/v1/control-actions` | 提交文本或结构化控制动作 |
| `GET` | `/v1/control-actions/{action_id}` | 查询控制动作结果 |
| `POST` | `/v1/audio-control-actions` | 上传音频并返回 mock 转写和标准化动作 |

## 数据流

1. AgentRuntime 接受 A 的 `createComputingSession`，随后分别向 A 和 B 下发 C-02；测试部署应把
   `service_endpoint` 配成 `http://172.30.0.10:28500`，把
   `media_connections_path` 配成 `/v1/media-connections`。
2. A 只通过 `sendMessage` 把 `compute_service_session_id` 发给 B。两端的 Sandbox 地址、
   `binding_ref`、角色和媒体路径都来自各自 SDK 内部缓存的 C-02。
3. A 调用 `getProcessedVideoStream(sessionId)`，以 consumer 身份主动提交非 Trickle Offer；
   B 调用 `startVideoUpload(sessionId)`，以 producer 身份提交 Offer。Mock 分别返回 Answer。
4. consumer 可以先建连并接收占位帧。Mock 收到 B 的第一帧后，在原下行 Track 内切到处理帧。
5. 停止媒体时 SDK 调用 `DELETE /v1/media-connections/{media_connection_id}`；重复删除仍返回 204。

Mock 会在视频左上角添加紫色块和移动绿条，便于确认接收到的是服务端处理后的流。

正式媒体接口为 `POST /v1/media-connections`。请求包含
`request_id + computing_context + offer`，成功返回 HTTP 201，并原样回显请求 ID 和上下文，
同时返回 `media_connection_id + answer`。同一 `binding_ref` 的 producer 和 consumer
各允许一个活动连接；同请求重试复用原连接，不同载荷复用同一请求 ID 返回 409。

旧 `/compute/v1/offloading-sessions` 和 `/video/v1/sessions/...` 路由暂时保留，只用于旧版
Mock 自测；新版 SDK 和 Android App 均不会调用这些路由。

## 部署

在 x86_64 主机构建、执行完整 HTTP/WebRTC 烟测并导出镜像：

```bash
cd /root/lpx/sdk/mock-video-server
./build-image.sh
```

生成：

```text
镜像：agent-compute-sandbox-mock:0.2.0-amd64
归档：dist/compute-mock/agent-compute-sandbox-mock-0.2.0-linux-amd64.tar.gz
校验：dist/compute-mock/agent-compute-sandbox-mock-0.2.0-linux-amd64.tar.gz.sha256
```

N6 主机离线导入后再部署：

```bash
sha256sum -c agent-compute-sandbox-mock-0.2.0-linux-amd64.tar.gz.sha256
gzip -dc agent-compute-sandbox-mock-0.2.0-linux-amd64.tar.gz | docker load
```

```bash
cd /root/lpx/sdk/mock-video-server
docker compose -f docker-compose.n6.yml up -d
docker compose -f docker-compose.n6.yml ps
docker exec agent-sdk-mock-video-server ip route
curl http://172.30.0.10:28500/healthz
python3 smoke_client.py --base-url http://172.30.0.10:28500
```

容器内必须保留以下回程路由，否则 UE 发出的请求能到 DN，但响应无法返回 UE：

```text
10.60.0.0/16 via 172.30.0.2
10.61.0.0/16 via 172.30.0.2
```

调试会话状态：

```bash
curl http://172.30.0.10:28500/debug/v1/sessions
docker logs -f agent-sdk-mock-video-server
```

Sandbox HTTP 不使用业务层 token、ticket、Bearer 或 proof；Agent 和绑定关系由
网侧建立，Mock 根据完整 `computing_context` 关联媒体、识别和控制请求。
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
