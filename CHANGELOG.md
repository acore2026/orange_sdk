# 修改记录

本文件以一次 Git commit 为一个记录单元。每次代码或交付文档修改都必须在同一 commit 中补充对应条目，说明修改原因、实现方式和验证结果；具体提交哈希以 Git 历史为准。

## 2026-08-19 — 恢复 AgentRuntime 控制面纯透传契约

### 修改原因

- AgentRuntime 的职责是按原始 URI 透传原始业务 JSON，不负责把网元字段改造成 SDK 领域模型字段。
- 原实现错误地要求 AgentRuntime 将 `vc0`、`vc1` 和 `result[].agent_card` 分别改成 `identity_vc`、`ability_vc` 和扁平 `agents[]`，同时错误展平了建群请求。

### 修改方式

- Python SDK 直接解析原始身份响应 `vc0`、网侧能力响应 `vc1` 和发现响应 `result[].agent_card`，再在端侧转换为北向领域对象。
- 建群请求恢复为原始 `target_agents` 和嵌套 `group_config` 结构。
- 增加控制面请求认证扩展点，由端侧 SDK 补充原始请求要求的 `timestamp/signature/signature_encoding` 或 `timestamp/proof`；AgentRuntime 不参与补字段或改字段。
- 能力更新继续使用已确认的新 URI `POST /arf/v1/agent-cards-update`，请求体保留原始 `request_id`、`request_type`、`update_items`、`credentials`、`timestamp` 和 `proof`。
- AgentRuntime 返回 HTTP 200 且无响应体时，SDK 将其作为空成功响应处理。
- 本地 HTTP 接口说明同步恢复原始请求/响应字段；该文件按仓库规则保持不跟踪、不推送。

### 验证内容

- 增加身份 `vc0`、能力 `vc1`、发现 `result[].agent_card`、嵌套建群请求及能力更新原始消息体测试。
- 增加 AgentRuntime 空 `200 OK` 响应测试。
- Python 全量测试结果：`27 passed`。
- `python/src` 和 `python/examples` 均通过 `compileall` 语法检查。
- HTTP 接口文档中的 17 个 JSON 示例全部通过解析校验，且不存在要求 AgentRuntime 改名或展平字段的残留说明。

## 2026-08-19 — 移除公开的 `peer_routes` 静态路由配置

### 修改原因

- 群组对端的三层路由应完全由 SDK 根据 `group-moq-info` 自动维护，不应要求 SDK 用户理解或配置具体路由。
- 手工静态路由可能允许尚未出现在有效群组配置中的目标 IP 进入 MASQUE 隧道，破坏“仅使用已提交群组成员地址”的边界。

### 修改方式

- 从 `AgentSdk.init()` 和内部 `SdkConfig` 中移除 `peer_routes`。
- 从 `SdkInitResult` 中移除 `installed_routes`，不再向用户返回底层路由信息。
- 从 Linux example 中移除 `--peer-route` 命令行参数。
- 删除 `GroupRouteManager` 的静态路由安装与保留逻辑，只根据有效群组快照添加、引用计数和删除对端主机路由。

### 验证内容

- 新增公开 API 回归测试，确认 `AgentSdk.init()` 不再包含 `peer_routes`，`SdkInitResult` 不再包含 `installed_routes`。
- Python 全量测试结果：`21 passed`。
- `python/src` 和 `python/examples` 均通过 `compileall` 语法检查。
- 生产代码、README 和 example 中不存在已移除参数及静态路由逻辑的残留引用。

## 2026-08-19 — 群组配置改为 SDK 内部自动提交

### 修改原因

- `group-moq-info` 携带的成员 IP 和端口属于 SDK 内部寻址数据，不应要求 SDK 用户参与缓存或路由决策。
- 原实现要求用户注册 `NetworkMessageListener` 并返回 `ACK` 后才提交配置；用户未注册 listener 或 listener 失败时，SDK 无法使用已经合法下发的对端地址。
- `cache_demo.py` 直接向用户展示内部缓存和路由类，与 SDK 封装边界不一致。

### 修改方式

- 群组配置通过验签、格式校验和本机信息校验后，由 SDK 自动提交成员缓存和动态路由，并将群组状态设为 `ACTIVE`。
- `NetworkMessageListener` 改为提交后的可选通知：未注册、返回 `REJECT` 或抛出异常均不回滚已提交配置，也不改变 SDK 对 AgentRuntime 的 `ACK`。
- 保留 `/A2A/message` 发送和接收路径，不修改消息 URL。
- 删除直接操作 `GroupMemberCache` 和 `GroupRouteManager` 的 `examples/cache_demo.py`，并移除 README 中对应说明。

### 验证内容

- 无 listener 时，合法群组配置仍能写入缓存并安装对端主机路由。
- listener 返回 `REJECT` 时，合法配置仍保持有效。
- listener 抛出异常时，合法配置不回滚。
- `send_message(group_id, target_agent_id, message)` 继续只使用已提交缓存中的目标 IP 和 TCP 端口。
- Python 全量测试结果：`20 passed`。
- `python/src` 和 `python/examples` 均通过 `compileall` 语法检查。
