# N6 / DN Mock Video Server

这个服务模拟算力沙箱分配器和 Video Server，部署在 free6GC 的 N6 数据网 `compose_n6` 中。
固定地址为 `172.30.0.10:28500`，UPF 的 N6 地址为 `172.30.0.2`。

## 数据流

1. C 使用 `SandboxSpec(vcpus=2, memoryMb=4096)` 调用 `createOffloadingSession`；
   Sandbox 由网络分配，Mock 同时返回 producer 和 processed-stream 地址。
2. B 调用 `startVideoUpload` 启动上传；应用可自行用 `sendMessage` 把 session 信息发给 A，也可不发送。
3. B 自己或收到 session 信息的其他 Agent，可以在 source 尚为 `SOURCE_PENDING` 时完成下行 WebRTC，并接收占位帧。
4. Mock 收到 B 的第一帧后进入 `SOURCE_CONNECTED`，在原下行 Track 内切到处理帧。
5. 切源保持 RTP 时钟单调，不重新协商，并为已连接 consumer 主动请求关键帧。

Mock 会在视频左上角添加紫色块和移动绿条，便于确认接收到的是服务端处理后的流。

创建请求的业务字段为 `workload_type + sandbox_spec{vcpus,memory_mb}`，不接收
`agent_id`、`group_id` 或指定 Sandbox 的 `sandbox_id`。创建响应只返回 session
生命周期字段和视频服务端点。

## 部署

```bash
cd /root/lpx/sdk/mock-video-server
docker compose -f docker-compose.n6.yml up -d --build
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

WebRTC HTTP 信令不使用业务层 token、ticket、Bearer 或 proof；Agent ID 的认证由
核心网会话负责。`/debug/v1/sessions` 的 `consumers.consumer-N` 会返回该连接的
`frames_processed`、`placeholder_frames_sent`、`packets_sent`、`bytes_sent`、
`codec`、`first_frame_sent` 和 `first_source_frame_sent`。`keyframes_requested`
记录 Answer 就绪、consumer 建连以及占位流切换 source 后主动补发的关键帧请求次数。
这些是连接生命周期内保留的单调计数；即使 PeerConnection
随后关闭，也能用来判断 Server 是否真正处理并发出了媒体。Server 日志还会为每个
consumer 连接单独输出首个占位/媒体帧和首个真实 source 帧日志。Server 默认以固定
`640x480@30fps` 输出、只保留最新 source 帧。B→Server 源流只接受
H.264 High/Constrained High（`64001f`/`640c1f`、`packetization-mode=1`），
并在建连后主动请求启动关键帧；`source_codec` 与 `source_keyframes_requested`
可用于确认上行协商和恢复状态。Server→A 下行按 aiortc 的实际编码能力使用
H.264 Baseline，避免 SDP profile 与真实输出不一致。
