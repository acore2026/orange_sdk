# N6 / DN Mock Video Server

这个服务模拟算力沙箱分配器和 Video Server，部署在 free6GC 的 N6 数据网 `compose_n6` 中。
固定地址为 `172.30.0.10:28500`，UPF 的 N6 地址为 `172.30.0.2`。

## 数据流

1. B 调用 `createOffloadingSession`，Mock 返回 producer token 和 Video Server 地址。
2. `startVideoUpload` 请求服务端 WebRTC Offer，启动摄像头并返回 Answer。
3. Mock 收到 B 的第一帧后进入 `SOURCE_CONNECTED`。
4. SDK 为每个目标 Agent 申请独立 consumer ticket，并通过群组 P2P 消息发送邀请。
5. A/其他 Agent 调用 `getProcessedVideoStream`，向 Mock 发 WebRTC Offer 并接收处理后视频。

Mock 会在视频左上角添加紫色块和移动绿条，便于确认接收到的是服务端处理后的流。

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
