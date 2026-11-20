# Python SDK 示例

本目录仅保留 SDK 用法和联调示例。先从仓库根目录安装 SDK：

```bash
python3 -m pip install -e './sdks/python[test]'
python3 examples/python/full_flow_demo.py
```

| 入口 | 用途 |
| --- | --- |
| `full_flow_demo.py` | 离线 SDK 全流程自检包装 |
| `linux_agent.py` | 真实 Linux 接入和全接口示例 |
| `interactive_linux_agent.py` | 按回车逐接口执行 |
| `agent_a_test.py` | A 端发现、建组、算力申请和视频接收 |
| `agent_b_test.py` | B 端能力发布、接受邀请和视频上传 |
| [`agw/`](agw/) | AGW A/B/C 独立终端用例、共用实现、集成测试和执行手册 |
| `assets/` | 内置测试视频 |
| `docker/arm64/` | A/B 运行镜像、Compose 与启动脚本 |
| `tests/` | 示例和启动脚本自身的测试 |

机器狗正式应用已移至 [`apps/dog-patrol/`](../../apps/dog-patrol/README.md)；真实双实例链路验证已移至 [`integration-tests/scenarios/`](../../integration-tests/README.md)。SDK 接口见 [Python 使用指南](../../sdks/python/README.md)。

AGW 三端的安装、执行顺序和验收结果见 [AGW 终端全流程测试](agw/AGW终端全流程测试.md)。
