# Orange Agent SDK

本仓库包含 Linux/Python SDK 和 Android/RayNeoOS SDK。

当前 Linux/Python 版本可以构建为 `agent-connect-sdk` wheel。客户安装 wheel 后即可导入 `agent_sdk`，端侧通过 TUN + MASQUE CONNECT-IP 接入外部 AgentRuntime/MASQUE 服务。发送消息时应用提供 `group_id`、`target_agent_id`、消息类型、任务 ID 和消息体；IP、端口和路由始终由 SDK 缓存解析。仓库只交付端侧 Client，不包含 MASQUE Server、UERANSIM 适配器或服务器启动命令。

完整的 wheel 构建、客户安装、端侧 MASQUE 参数、全流程调用示例、函数清单和故障排查见：

- [Linux/Python 客户使用指南](python/README.md)
- [离线全流程示例](python/examples/full_flow_demo.py)
- [真实 Linux 全接口调用示例](python/examples/linux_agent.py)
- [按回车逐接口调用的 Linux 交互示例](python/examples/interactive_linux_agent.py)
- [Agent A：按能力发现、建组并发送消息](python/examples/agent_a_test.py)
- [Agent B：发布能力、接受建组并接收消息](python/examples/agent_b_test.py)
- [A/B 双实例 MASQUE 消息联调脚本](python/examples/masque_two_instance_test.py)
- [Android/RayNeoOS 使用说明](android/README.md)
- [Proof 生成与校验说明](Agent-SDK-Proof生成与校验说明.md)

当前封闭内测构建保留 MASQUE TLS 1.3 加密，但关闭服务端证书链和名称校验；
连接时会记录明确的安全警告。SDK 首次启动仍会在端侧私有目录生成并复用
自己的 Ed25519 MASQUE 客户端证书/私钥；同时生成独立的 P-256 消息签名
密钥。控制面和 A2A 消息由 SDK 自动签名，核心网群组配置使用 AAR/Wheel
内预置的核心网 P-256 公钥验签，应用不配置密钥或安全回调。除 MASQUE 的
HTTPS/HTTP/3 外，SDK 与 AgentRuntime、对端 Agent 的接口统一使用 HTTP。
Android AAR 已包含真实的 ARM64 `libmasque_core.so`，不再要求客户另行提供
Native Core。

Linux/Python 和 Android 的 AgentCard 发布接口默认接收已经签发的 VC。封闭
联调时也可传 `capabilities=[...]`，SDK 为每个能力生成一张 IDM 兼容的
`CapabilityCredential` 后放入既有 `vc_list`。Python 从外部
`~/lpx/cert/third-party/private-key.pem` 读取测试机构 P-256 私钥；Android 需先
把同一测试私钥导入应用私有目录。对应的三方认证公钥已经作为 SDK 相对资源
打包进 Wheel/AAR，不依赖宿主机绝对路径；测试私钥不会打包进 Wheel、AAR 或
Git。正式环境仍应传入外部认证机构签发好的 VC。

核心网主动下行不再反向 POST 到端侧。Linux/Android SDK 初始化时主动连接
`/v1/acn/downlink-websocket`，WebSocket 握手与后续主动上行 REST 接口使用同一
AgentRuntime IP 和端口。初始化不再调用 `/health` 或 `/sdk/v1/endpoints`，
而是调用 `GET /v1/ue/info` 查询 UERANSIM UE 状态，从活动 IPv4 PDU
Session 的 `ipv4` 字段得到本机 Agent TUN IP；该 GET 无请求体，不向
AgentRuntime 同步本机信息。
本地 HTTP Server 只保留 Agent 间 `/A2A/message`。

控制面写请求由 SDK 自动生成普通 UUID 格式的 `request_id`。身份申请严格按
`ACN-H-ID-v1` 编码 owner/name/publicKey/description/timestamp，并将完整紧凑
`metadata` JSON Container 作为一个 LP16 字段签名；其他控制请求使用 `proof`。
`proof` 保留现有 `verification_method/proof_purpose` 字段名。其分离 JWS 的
未编码载荷固定为
`SHA-256(canonical(proof 去掉 jws)) || SHA-256(canonical(业务文档去掉 proof))`；
两个摘要均为 32 字节且 proof 摘要在前。控制请求的 `request_id` 不进入业务
文档摘要。
核心网群组配置下行消息类型固定为
`ACN_AGENT_GROUPING_NOTIFICATION`。A2A 消息使用
`src_agent_id/dst_agent_id/type/task_id/payload`，成功响应为
`{"status":"OK"}`。

快速验证 Python 实现：

```bash
cd python
python3 -m pip install -r requirements.txt
python3 -m pip install -e '.[test]'
pytest -q
agent-sdk-self-check
```

设计和示例中的 IP 地址均为部署参数，SDK 源码不硬编码物理地址、Agent TUN 地址、对端地址或端口。对端地址和动态路由只来自通过验证的 `acf_group_config`。
