# AGW 三终端用例

本目录集中存放通过 `AgentSdk` 公共接口执行的 AGW A/B/C 用例。
部署前提、安装、执行顺序及预期输出见 [终端全流程测试手册](AGW终端全流程测试.md)。

| 文件 | 用途 |
| --- | --- |
| `agw_agent_a.py` | A 端：注册、发现 B/C、建组、多播及双端回执验证 |
| `agw_agent_b.py`、`agw_agent_c.py` | B/C 端：注册、接受邀请、目录同步、订阅、回执及注销 |
| `agw_terminal.py` | 三端共用流程实现 |
| `tests/test_agw_terminals.py` | 用真实 SDK 和本地 QUIC Relay 验证用例，模拟 Runtime/TUN/MASQUE 边界 |

从 SDK 仓库根目录运行本目录的自动化测试：

```sh
python3 -m pytest -q examples/python/agw/tests
```

自动化测试需要已安装配套 SDK 测试依赖及 `moq-py`（安装包名 `moq-transport`，默认包含 ACN 所需依赖）；它不能替代真实核心网和终端验收。
