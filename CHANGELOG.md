# 修改记录

本文件以一次 Git commit 为一个记录单元。每次代码或交付文档修改都必须在同一 commit 中补充对应条目，说明修改原因、实现方式和验证结果；具体提交哈希以 Git 历史为准。

## 2026-08-20 — Linux 真实示例覆盖全部 SDK 北向函数

### 修改原因

- `examples/linux_agent.py` 原先只恢复一份调用方传入的 `AgentProfile`、执行 `init` 后无限等待，没有展示客户拿到 SDK 后如何完成身份、能力、发现、建群、消息和计算卸载全流程。
- 客户需要在同一份真实网络示例中看到所有北向函数的调用关系，同时仍由 SDK 隐藏群组成员 IP、端口和路由。

### 修改方式

- `linux_agent.py` 依次调用 `init`、`apply_identity`、`set_local_profile_for_restore`、`get_network_ability`、`register_capabilities`、`update_capabilities`、`discover_agents`、`create_group`、`get_group_snapshot`、`send_message`、`create_offloading_session`、`start_video_upload`、`get_processed_video_stream`、`deregister_identity` 和 `close`。
- 同一文件注册网络消息和群组消息 listener；建群后轮询群组快照，必须等到已校验的 `acf_group_config` 包含目标 Agent 才发送消息，应用仍不传 URL、IP、端口或路由。
- 新增完整 CLI 参数，身份申请公钥、发现条件、目标 Agent、群组、消息、卸载、视频和超时均可配置；`--target-agent-id` 为空时使用发现结果第一项。
- 示例默认注销本次申请的身份以真实覆盖注销接口；`--keep-identity` 可显式保留，`--stay-running` 可在全流程后继续接收消息。
- 提供明确标记为 example-only 的媒体适配器，使视频上传句柄、暂停/恢复/停止和处理后视频流函数均能被调用；文档明确它不读取摄像头、不代表真实 WebRTC 实现。
- Python 客户 README 和根 README 同步完整启动命令、函数顺序、身份公钥与 MASQUE TLS 密钥的区别及安全注意事项。SDK 实现和公开 API 未变化，因此 wheel 版本保持 `0.5.0`。

### 验证内容

- 新增 AST 调用覆盖测试，逐项确认 `linux_agent.py` 包含 17 个 SDK 公共入口；新增可执行的 mock 全流程测试，确认全部业务异步函数、视频句柄和视频流实际被调用。
- CLI 参数、JSON 消息解析和非法非对象消息均有测试；`linux_agent.py --help` 可正常输出完整参数。
- Python 全量测试结果：`49 passed`；`src`、`examples` 和 `tests` 全部通过 `compileall`。

## 2026-08-20 — 内置 MASQUE 服务端信任并补齐 Android Native Core

### 修改原因

- 客户不应在 `sdk.init`/`initialize` 中理解或传入 MASQUE CA、服务端证书名称、客户端证书或私钥路径；这些属于 SDK 与部署方的传输安全边界。
- Android 先前只有 `NativeMasqueBridge` 的 JNI 声明和 Fake 测试实现，AAR 没有 `libmasque_core.so`，真实设备无法建立 CONNECT-IP 隧道。
- SDK 需要在首次运行生成自己的公私钥对并稳定复用；服务端公钥信任与端侧私钥必须分离，服务端私钥不得进入客户 Wheel/AAR。

### 修改方式

- Python Wheel 和 Android Native Core 内置相同的 ACore MASQUE 根 CA，TLS 名称固定为 `masque.agent.internal`；Python/Android 初始化接口删除运行时 CA 和 Server Name 参数。
- Python 首次连接时在 `$XDG_STATE_HOME/agent-sdk/tls` 或 `~/.local/state/agent-sdk/tls` 生成 Ed25519 客户端证书/私钥；Android 在应用 `noBackupFilesDir/agent-sdk/tls` 生成。目录和文件权限分别固定为 `0700/0600`，不完整或不匹配的已有身份会 fail closed。
- 在 SDK 仓库的 `deployment/masque-tls/` 提供封闭实验网 POC 服务端证书和部署说明；对应私钥仅保留在当前安全工作区并由 `.gitignore` 排除，通过受控渠道交给服务器运维。现有 Server 只需使用已有 `tlsCertFile/tlsKeyFile` 配置加载，无需修改服务端代码。远端源码、Wheel 和 AAR 均不包含服务端私钥。
- 新增 Go/NDK ARM64 Native Core，实际完成受 `VpnService.protect()` 保护并绑定 `localVlanIp` 的 QUIC socket、HTTP/3 CONNECT-IP、`ADDRESS_ASSIGN` 地址校验、`ROUTE_ADVERTISEMENT` 等待、TUN 双向包泵、MTU 限制、关闭清理和 TUN fd 热替换。
- AAR 直接打包 `jni/arm64-v8a/libmasque_core.so`；保留可复现构建脚本。固定的 `connect-ip-go v0.2.0` MIT 源码仅增加 `DialWithHeaders`，保证 Android 的可选 `masqueAuthorization` 真正进入扩展 CONNECT 请求而非被静默忽略。
- Python wheel 公开初始化签名和安全默认值发生变化，版本从 `0.4.0` 提升到 `0.5.0`；Linux/Android example、README、Native ABI 说明和本地《SDK 设计文档-用户友好版》同步更新。原始《SDK设计文档》和 HTTP 消息契约不变。
- `/root/proj/go/free6gc/free6gc-ueransim-go` 未产生任何代码或配置改动。

### 验证内容

- Python 全量测试 `45 passed`，覆盖内置 CA、密钥首次生成/稳定复用、权限、残缺身份拒绝和真实 HTTP/3 CONNECT-IP；Wheel 独立安装后版本为 `0.5.0`、`pip check` 无错误，`agent-sdk-self-check` 输出 `FULL FLOW DEMO PASSED`。
- Native Core 的 3 个 Go 测试通过；真实 HTTP/3 测试验证 `ADDRESS_ASSIGN`、路由 Capsule、双向 IP 包、客户端证书出示和 Authorization 请求头。
- Android 11 个 JVM 单元测试零失败；Release AAR 和 example Debug APK 构建成功。ARM64 ELF 中存在 `nativeStart/nativeReplaceTunFd/nativeStop` JNI 导出符号，AAR/APK 均包含该 `.so`。
- 已按已知服务端私钥内容扫描 Wheel 和 AAR，均未发现服务端私钥；Wheel 仅包含 `certs/masque-root-ca.pem`。
- 本地交付物：`agent_connect_sdk-0.5.0-py3-none-any.whl` SHA-256 `ab44eabaffbbc5491be64ff4144961bc7b0acc6e32893bf502480cde81b3ae15`；`libmasque_core.so` SHA-256 `544bad09b0233888271424e2ea89f4aeafbf7322d33675a02b1bcbff742261e3`；Release AAR SHA-256 `afe43e3994d6c6255d0308d0ee78faf082f781c1d1bf4814920e16d06147c4d8`。
- 原始《SDK设计文档》的 SHA-256 仍为 `d2509f323338d0cdb948ceff36e32c3ae71d59c646916b687168b4c2e862947b`，确认未被修改。

## 2026-08-20 — 精简 SDK 端点注册请求和响应

### 修改原因

- AgentRuntime 的 SDK 端点注册接口不再需要客户端声明固定回调路径，因此请求体不应继续发送 `callback_paths`。
- 服务器不再分配或返回 `registration_id`，SDK 不应要求该字段或向用户暴露无来源的注册标识。

### 修改方式

- Python 和 Android 的 `POST /sdk/v1/endpoints` 请求体统一为 `local_vlan_ip`、`tcp_port`、`udp_port` 三个字段。
- 端点注册响应模型仅保留必填的 `ue_ip` 和 `ue_prefix_length`；两端删除 `registration_id/registrationId` 解析和缺失校验。
- Python 和 Android `SdkInitResult` 删除 `registration_id/registrationId`，example 和测试伪实现同步新模型。
- HTTP 精确报文测试改为断言请求不包含 `callback_paths`、响应不包含 `registration_id`，但 UE IP 仍能创建正确的 Agent TUN。
- Python/Android README 和本地《Agent SDK HTTP 接口文档》、《SDK 设计文档-用户友好版》同步精简后契约；两份交付文档按仓库规则继续仅保留在本地。
- Python wheel 因公开返回模型变更从 `0.3.0` 提升到 `0.4.0`。

### 验证内容

- Python 全量测试结果：`40 passed`；`python/src`、`python/examples` 和 `python/tests` 通过 `compileall`。
- Android 单元测试结果：`11 tests, 0 failures`；`example-app` Debug APK 构建成功。
- HTTP 文档 17 个 JSON 示例和用户友好版文档 3 个 JSON 示例全部通过标准解析。
- 全新虚拟环境安装 wheel 后依赖检查通过，`agent_sdk.__version__` 为 `0.4.0`，`agent-sdk-self-check` 输出 `FULL FLOW DEMO PASSED`。
- 本地交付物为 `python/dist/agent_connect_sdk-0.4.0-py3-none-any.whl`，SHA-256 为 `4b01b31764f31deef4baa1661cf0ea430351788f68e408cf2f3cb481cdb1dc70`。
- 原始《SDK设计文档》的 SHA-256 保持为 `d2509f323338d0cdb948ceff36e32c3ae71d59c646916b687168b4c2e862947b`，确认未被修改。

## 2026-08-20 — SDK 初始化改为使用 AgentRuntime 分配的 UE IP

### 修改原因

- 端侧设备对应的 UE IP 由服务器组网和 UERANSIM 映射决定，不应由 SDK 用户在 `init` 中手工填写。
- 手工填写可能使端侧 Agent TUN IP 与 MASQUE Proxy 中的 `agent_ip -> uesimtun*` 映射不一致，导致 CONNECT-IP 内层包被拒绝或注入错误 UE。

### 修改方式

- `POST /sdk/v1/endpoints` 成功响应新增必填的 `ue_ip` 和 `ue_prefix_length`；Python 和 Android 都会校验 IP 字面量、地址簇和前缀范围。
- Python `AgentSdk.init()` 和 Android `AgentSdk.initialize()` 移除公开的 Agent TUN CIDR 参数，固定先连接 AgentRuntime 并注册端点，再用返回的 `ue_ip/prefix` 创建 Linux TUN 或 Android VPN TUN。
- `SdkInitResult.agent_tun_cidr/agentTunCidr`、Agent IP 侧监听地址和 MASQUE 配置均来自同一份服务器分配结果，不存在硬编码默认 UE IP。
- Android 同步清理此前遗留的公开 `peerRoutes` 和 `installedRoutes`，与 Python 及“对端路由由 `acf_group_config` 管理”的已确认契约一致。
- Linux/Android example、Python/Android README 和本地《Agent SDK HTTP 接口文档》、《SDK 设计文档-用户友好版》同步新时序。两份交付文档继续按仓库规则仅保留在本地，不进入 Git 提交或远端。
- Python wheel 因公开初始化签名变更从 `0.2.0` 提升到 `0.3.0`。

### 验证内容

- Python 全量测试结果：`40 passed`；覆盖精确 URL/请求体、UE IP 响应解析、非法 IP/前缀拒绝以及分配地址实际传入 TUN。
- Android 单元测试结果：`11 tests, 0 failures`；`example-app` Debug APK 构建成功。
- `python/src`、`python/examples` 和 `python/tests` 全部通过 `compileall`；HTTP 文档 17 个 JSON 示例和用户友好版文档 3 个 JSON 示例全部通过标准解析。
- 全新虚拟环境安装 wheel 后依赖检查通过，`agent_sdk.__version__` 为 `0.3.0`，`agent-sdk-self-check` 输出 `FULL FLOW DEMO PASSED`。
- 本地交付物为 `python/dist/agent_connect_sdk-0.3.0-py3-none-any.whl`，SHA-256 为 `4f275641f870ebbf51815ae6c44c91aaf0d5bd123c465cdc35f46773cb54c31d`。
- 原始《SDK设计文档》的 SHA-256 保持为 `d2509f323338d0cdb948ceff36e32c3ae71d59c646916b687168b4c2e862947b`，确认未被修改。

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
