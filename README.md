# Orange Agent SDK

本仓库包含 Linux/Python SDK 和 Android/RayNeoOS SDK。

当前 Linux/Python 版本可以构建为 `agent-connect-sdk` wheel。客户安装 wheel 后即可导入 `agent_sdk`，端侧通过 TUN + MASQUE CONNECT-IP 接入 UERANSIM/5GC，发送消息时只需要提供 `group_id`、`target_agent_id` 和消息体。

完整的 wheel 构建、客户安装、MASQUE 服务器/端侧配置、全流程调用示例、函数清单和故障排查见：

- [Linux/Python 客户使用指南](python/README.md)
- [离线全流程示例](python/examples/full_flow_demo.py)
- [真实 Linux 全接口调用示例](python/examples/linux_agent.py)
- [MASQUE Proxy 配置模板](python/examples/masque-proxy.example.json)
- [Android/RayNeoOS 使用说明](android/README.md)
- [MASQUE 服务端证书部署材料](deployment/masque-tls/README.md)

当前封闭内测构建保留 MASQUE TLS 1.3 加密，但关闭服务端证书链和名称校验；
连接时会记录明确的安全警告。SDK 首次启动仍会在端侧私有目录生成并复用
自己的 Ed25519 MASQUE 客户端证书/私钥；同时生成独立的 P-256 消息签名
密钥。控制面和 A2A 消息由 SDK 自动签名，核心网群组配置使用 AAR/Wheel
内预置的核心网 P-256 公钥验签，应用不配置密钥或安全回调。除 MASQUE 的
HTTPS/HTTP/3 外，SDK 与 AgentRuntime、对端 Agent 的接口统一使用 HTTP。
Android AAR 已包含真实的 ARM64 `libmasque_core.so`，不再要求客户另行提供
Native Core。

Linux/Python 的 `register_capabilities()` 默认接收已经签发的 VC。封闭联调时
也可传 `capabilities=[...]`，SDK 会从外部
`~/lpx/cert/third-party/private-key.pem` 读取测试机构 P-256 私钥，为每个能力
生成一张 IDM 兼容的 `CapabilityCredential` 后放入既有 `vc_list`。测试机构
私钥不会打包进 Wheel；正式环境仍应传入外部认证机构签发好的 VC。

核心网主动下行不再反向 POST 到端侧。Linux/Android SDK 初始化时主动连接
`/v1/acn/downlink-websocket`，WebSocket 握手与主动上行 REST 接口使用同一
AgentRuntime IP 和端口；本地 HTTP Server 只保留 Agent 间 `/A2A/message`。

快速验证 Python 实现：

```bash
cd python
python3 -m pip install -e '.[test]'
pytest -q
agent-sdk-self-check
```

设计和示例中的 IP 地址均为部署参数，SDK 源码不硬编码物理地址、Agent TUN 地址、对端地址或端口。对端地址和动态路由只来自通过验证的 `acf_group_config`。
