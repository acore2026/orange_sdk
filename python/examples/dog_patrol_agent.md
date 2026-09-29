# 机器狗巡检 Agent

`dog_patrol_agent.py` 是故事 case 的机器狗侧程序。它使用当前 Linux Agent SDK 完成身份注册、能力发布、群组邀请接入和 A2A 消息；巡逻动作可以直接调用宇树 SDK，也可以调用已有的 `compute_backend/dog-ctrl` HTTP 服务。

真实机器狗部署：

```bash
python python/examples/dog_patrol_agent.py \
  --action-backend unitree \
  --runtime-ip 192.168.3.10 \
  --runtime-port 8080 \
  --masque-url https://192.168.3.10:4433 \
  --masque-token '<device-token>' \
  --unitree-interface enp2s0 \
  --camera-id 0 \
  --stay-running
```

如果 `dog-ctrl` 已经单独运行：

```bash
python python/examples/dog_patrol_agent.py \
  --action-backend http \
  --dog-ctrl-url http://127.0.0.1:8080 \
  --runtime-ip 192.168.3.10 \
  --masque-url https://192.168.3.10:4433 \
  --stay-running
```

没有网络和机器狗时先运行完整故事自检：

```bash
python python/examples/dog_patrol_agent.py --dry-run
```

眼镜通过安全群组发送的最小业务消息如下。RayNeo 远端流程实际使用
`robot_action`，程序同时保留 `dog_command` 兼容格式：

```json
{"type":"patrol_request","task_id":"task-a","zone":"A","confirmed":true}
{"type":"dog_command","command":"intimidate","confirmed":true,"command_id":"cmd-1"}
{"type":"dog_command","command":"expel","confirmed":true,"command_id":"cmd-2"}
{"type":"robot_action","action":"FrontPounce","command":"驱逐歹徒","risk":"high","task_id":"patrol-g-1"}
```

`intimidate` 调用 `Hello()`，`expel` 调用 `FrontPounce()`。程序要求这两个动作都带用户确认，其中前扑动作额外保持幂等的 `command_id`，避免 A2A 重试重复触发。

视觉算力卸载有两种兼容路径：收到 RayNeo/Android 发来的
`compute_service_session_id` 时作为 producer 调用 `start_video_upload()`；也可以通过巡逻请求中的 `compute_agent_id` 由机器狗主动创建正式 `dog-vision` 会话。视觉服务产生的 `danger_detected`/`vision_alert` 事件会被转成 `danger_alert` 发回眼镜。

宇树官方接口参考：

- [unitree_sdk2_python 的 `SportClient`](https://github.com/unitreerobotics/unitree_sdk2_python/blob/master/unitree_sdk2py/go2/sport/sport_client.py)
- [Unitree C++ Go2 `SportClient` 头文件](https://github.com/unitreerobotics/unitree_sdk2/blob/main/include/unitree/robot/go2/sport/sport_client.hpp)
