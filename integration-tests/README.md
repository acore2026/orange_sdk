# 跨模块验证

- `contracts/test_runtime_voice_contract.py`：验证眼镜/Runtime 动作载荷与机器狗执行接口的契约，使用 dry-run 和 mock，不触发真实运动。
- `scenarios/masque_two_instance_test.py`：既有双实例 MASQUE 真实链路验证程序；需要外部 Runtime、MASQUE、TUN 权限和双实例环境。
- `scenarios/test_masque_two_instance_example.py`：上述程序的本地测试，使用回环地址和测试替身。

从仓库根目录执行本地测试：

```bash
python3 -m pytest -q integration-tests/contracts \
  integration-tests/scenarios/test_masque_two_instance_example.py
```

真实链路参数说明：

```bash
python3 integration-tests/scenarios/masque_two_instance_test.py --help
```

详细部署和双实例操作见 [Python SDK 使用指南](../sdks/python/README.md)。单模块测试继续跟随模块存放；需要公共跨模块测试数据时才建立 `fixtures/`。参见 [目录规范](../docs/architecture/REPOSITORY_LAYOUT.md)。
