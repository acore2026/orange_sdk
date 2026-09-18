#!/usr/bin/env python3
"""N6/DN Compute Sandbox Mock。

与 pruned_sandbox 相同的四组接口在单进程内监听：

- CMF 管理面：`28501`
- N6 用户面：`28502`
- 独立 ASR：`9004`（固定转写结果）
- 内部 Intent：`127.0.0.1:8011`（规则分类，无模型）
"""

from __future__ import annotations

from services.sandbox.main import main

if __name__ == "__main__":
    main()
