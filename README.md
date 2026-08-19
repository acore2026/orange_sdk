# Orange Agent SDK

本仓库包含 Linux/Python SDK 和 Android/RayNeoOS SDK。

当前 Linux/Python 版本可以构建为 `agent-connect-sdk` wheel。客户安装 wheel 后即可导入 `agent_sdk`，端侧通过 TUN + MASQUE CONNECT-IP 接入 UERANSIM/5GC，发送消息时只需要提供 `group_id`、`target_agent_id` 和消息体。

完整的 wheel 构建、客户安装、MASQUE 服务器/端侧配置、全流程调用示例、函数清单和故障排查见：

- [Linux/Python 客户使用指南](python/README.md)
- [离线全流程示例](python/examples/full_flow_demo.py)
- [真实 Linux 端侧示例](python/examples/linux_agent.py)
- [MASQUE Proxy 配置模板](python/examples/masque-proxy.example.json)
- [Android/RayNeoOS 使用说明](android/README.md)

快速验证 Python 实现：

```bash
cd python
python3 -m pip install -e '.[test]'
pytest -q
agent-sdk-self-check
```

设计和示例中的 IP 地址均为部署参数，SDK 源码不硬编码物理地址、Agent TUN 地址、对端地址或端口。对端地址和动态路由只来自通过验证的 `acf_group_config`。
