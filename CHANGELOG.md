# 修改记录

本文件以一次 Git commit 为一个记录单元。每次代码或交付文档修改都必须在同一 commit 中补充对应条目，说明修改原因、实现方式和验证结果；具体提交哈希以 Git 历史为准。

## 2026-08-19 — 增加本地结构化日志和 HTTP 全链路记录

### 修改原因

- 原 SDK 仅在群组 listener 异常时输出一条模块日志，无法从本地文件还原函数调用、AgentRuntime/A2A 请求响应或 CONNECT-IP 协商过程。
- 客户现场需要长期保存日志，同时必须控制文件大小并避免 token、凭证、签名和 VC 等敏感信息明文落盘。

### 修改方式

- `AgentSdk.init()` 增加 `log_file_path`、`log_level`、`log_max_bytes` 和 `log_backup_count`，默认写入 `./logs/agent-sdk.log`，采用 10 MiB × 5 份轮转。
- 所有公开 SDK 函数记录 `function_enter/function_exit/function_error`，包含脱敏参数、脱敏结果、错误码、异常和耗时；初始化前注册 listener 的日志会在 `init()` 配置文件日志后补写。
- AgentRuntime 出站请求、A2A 出站请求、本地 Runtime/A2A 入站请求及响应全部记录关联 ID、方向、方法、URL、状态码和脱敏消息体。
- MASQUE 客户端与 Proxy 两侧记录 HTTP/3 CONNECT-IP 请求、响应状态和协商头；Proxy JSON 配置增加独立的本地日志和轮转参数。
- 新增统一递归脱敏，覆盖 Authorization、Cookie、API key、token、password、secret、签名、proof/JWS、公私钥、DID key、VC/credentials 及 VC 标识。
- 新增 `LOG_SETUP_FAILED`，在日志目录不可创建或文件不可写时阻止 SDK 以无日志状态启动。
- Python 客户指南和真实 Linux example 同步增加日志配置、实时查看、错误检索、轮转和脱敏说明。
- wheel 版本从 `0.1.0` 提升到 `0.2.0`，避免覆盖已经交付的旧版本。

### 验证内容

- Python 全量测试结果：`35 passed`；新增函数成功/异常、Runtime HTTP、入站 HTTP、HTTP/3 CONNECT-IP、敏感字段脱敏、非法日志级别和文件轮转测试。
- `python/src`、`python/examples` 和 `python/tests` 全部通过 `compileall`。
- 在隔离环境中重新安装 wheel 后，`pip check` 无依赖问题，`agent-sdk-self-check` 输出 `FULL FLOW DEMO PASSED`，并实际生成本地日志文件。
- 安装验收日志未出现测试 token、签名、JWS、`vc0` 或 `vc1` 明文值。
- 本地交付物为 `python/dist/agent_connect_sdk-0.2.0-py3-none-any.whl`，SHA-256 为 `e42a1557c92db4fa4dfadd535a10cf13b4e58dc48257f0f35757880025db2cd2`。

## 2026-08-19 — 完成 Python wheel 交付和客户全流程指南

### 修改原因

- 客户需要通过 `pip install` 安装可分发 wheel，而不是依赖源码目录或手工设置 `PYTHONPATH`。
- 原 README 只给出开发态启动命令，没有从首次安装、MASQUE 内外部配置到 SDK 函数调用的完整操作路径。
- 原 example 只初始化并常驻，没有实际跑遍身份、能力、发现、建群、群组配置、消息和卸载流程。

### 修改方式

- 完善 `agent-connect-sdk` 包元数据，增加运行时版本 `agent_sdk.__version__`、`py.typed` 类型标记以及构建/测试开发依赖组。
- wheel 新增 `agent-masque-proxy` 命令，统一读取 JSON 配置并按 token 建立唯一 `agent_ip + uesimtun + allowed_peer_cidrs` 会话策略；原服务器 example 改为该命令的兼容入口。
- wheel 新增 `agent-sdk-self-check` 命令，以真实 `AgentSdk` 核心和内存边界适配器完整调用主要北向 API；仓库 `examples/full_flow_demo.py` 复用同一实现。
- 重写 Python 客户指南，覆盖 wheel 构建、联网/离线安装、安装验收、UERANSIM 和 MASQUE Proxy 配置、端侧 `init` 参数、监听器、全部函数调用、A→5GC→B 数据路径和故障定位。
- 根 README 改为交付入口，链接客户指南、全流程示例、真实端侧示例、Proxy 配置模板和 Android 说明。
- `dist/` 加入忽略列表，构建产物保留为本地交付物，不纳入源码 commit。

### 验证内容

- Python 全量测试结果：`30 passed`；新增全流程 example 和 Proxy 配置/会话映射测试。
- `python/src`、`python/examples` 和 `python/tests` 全部通过 `compileall`。
- 在全新虚拟环境中执行普通 `pip install`，依赖自动安装完成，`pip check` 返回 `No broken requirements found`。
- wheel 内的 `agent-sdk-self-check` 输出 `FULL FLOW DEMO PASSED`，`agent-masque-proxy --help` 和 `agent_sdk.__version__` 均验证通过。
- 本地交付物为 `python/dist/agent_connect_sdk-0.1.0-py3-none-any.whl`，SHA-256 为 `488c0d411a9ec3cb427de69f0a50b20dcda8a785b2af21da3fee245676b3ed59`。

## 2026-08-19 — 同步本地接口文档与当前 SDK 契约

### 修改原因

- HTTP 接口文档和用户友好版设计文档需要与当前 SDK 的封装边界保持一致，避免继续描述 AgentRuntime 字段适配、公开静态路由配置或由用户决定群组配置是否生效。
- 原始《SDK设计文档》按要求保持不变；两份同步后的交付文档继续遵循仓库规则，仅保留在本地，不纳入 Git 推送。

### 修改方式

- HTTP 接口文档明确 AgentRuntime 纯透传原始消息：身份响应使用 `vc0`，网络能力响应使用 `vc1`，发现响应使用 `result[].agent_card`，建群请求使用 `target_agents` 和嵌套 `group_config`。
- 用户友好版设计文档同步移除公开的 `peer_routes`、`peerRoutes` 和 `installed_routes`，明确对端主机路由由 SDK 根据有效 `acf_group_config` 自动维护。
- 用户友好版设计文档同步更新控制面伪代码、Linux/Android 初始化示例、群组配置时序和生命周期：SDK 先提交合法配置，listener 仅接收提交后的可选通知。
- 能力更新接口在两份文档中统一为 `POST /arf/v1/agent-cards-update`，并保留原始请求体字段。

### 验证内容

- HTTP 接口文档的 17 个 JSON 示例和用户友好版设计文档的 1 个 JSON 示例均通过标准 JSON 解析。
- 两份文档均未发现 `peer_routes`、`peerRoutes`、`installed_routes`、旧 `PUT ...agent-cards` 或 AgentRuntime 字段改名说明残留。
- 原始《SDK设计文档》的 SHA-256 保持为 `d2509f323338d0cdb948ceff36e32c3ae71d59c646916b687168b4c2e862947b`，确认未被修改。

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
