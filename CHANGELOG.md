# 修改记录

本文件以一次 Git commit 为一个记录单元。每次代码或交付文档修改都必须在同一 commit 中补充对应条目，说明修改原因、实现方式和验证结果；具体提交哈希以 Git 历史为准。

## 2026-09-01 — 新增参数less Agent Reset 与 App 状态1控制

### 修改原因

- App 缺少一个直接把当前 Agent 的本地状态控制回“未申请数字身份”状态1的入口；网侧状态可能与端侧不同步，因此 Reset 不能依赖去注册请求成功。
- 雷鸟眼镜依赖镜腿焦点和单击操作，Reset 若作为普通按钮直接执行，容易因误触清除当前本地身份状态。
- Reset 与身份申请、能力发布等写接口并发时不能立即清理，否则尚未结束的调用可能在 Reset 后重新写入本地身份。

### 修改方式

- Python/Android SDK 新增 `reset_agent/resetAgent`：状态2/3直接删除本地持久化 Profile、身份申请上下文和 Agent Card 并进入 `NO_IDENTITY`；状态1幂等成功。所有状态下均不发送 HTTP，不修改网侧身份。
- Generic 与 RayNeo App 的运行页增加“重置到状态1”，采用二次点击确认、执行中禁用和成功/失败反馈。成功后停止自动流程并关闭当前链路，不自动重新申请身份；本地持久化删除失败时保留 SDK 身份状态。
- App Runner 用互斥区串行化 SDK 操作。Reset 先等待当前调用明确完成，再阻止后续步骤并清除本地状态，避免进行中的控制面写请求在 Reset 后重新落盘。
- 雷鸟 Reset 纳入 Mercury 镜腿焦点顺序，并使用现有暗色层级与琥珀色警示状态；Python Wheel 升级为 `0.16.0`，示例 App 升级为 `0.2.4`。

### 验证内容

- Python 与 Android SDK 状态机测试覆盖状态1幂等、状态2/3本地迁移，以及 Reset 全程不产生 HTTP 请求。
- Generic/RayNeo App 单元测试覆盖 Reset 接线、二次确认、镜腿焦点和后台执行约束。

## 2026-09-01 — 修复雷鸟 App 首次授权与主线程假死

### 修改原因

- 雷鸟专用 App 在 Launcher 创建后立即拉起 Android 系统 VPN 授权页；该系统页会覆盖眼镜 App 和 Launcher，在用户尚未准备操作时表现为整机被弹窗锁住。
- VPN 授权后，`AgentSdk.initialize` 又从 Activity 主线程进入同步 JNI CONNECT-IP 握手；MASQUE 服务不可达或协商较慢时最多占用主线程 10 秒，使双目 UI 和镜腿事件无响应。
- 旧雷鸟 App 的联调流程会在启动前预授予测试权限，并把业务生命周期与 UI 事件分离；当前专用 App 需要采用相同的前台安全原则，但正式使用仍必须保留 Android 首次 VPN 用户同意。

### 修改方式

- 雷鸟入口不再在 `onCreate` 自动连接；先展示可操作页面，用户单击“启用 Agent 网络”后才调用 `VpnService.prepare`，并使用 Activity Result API 处理同意、拒绝和返回。
- 将 `AgentSdk.create`、密钥初始化、TUN/MASQUE 和 Agent A 全流程放入 `Dispatchers.IO`；SDK 的 Native MASQUE Transport 自身也把 JNI 启动、换 TUN 和关闭放入 IO 调度器，并在连接完成与协程取消竞态下回收刚创建的 Native handle。移除 Activity `onDestroy` 的主线程 `runBlocking`，改由独立清理作用域等待连接任务并释放 SDK。
- 新增 Windows 安装脚本，默认走真实用户首次授权；`-PreAuthorizeVpn` 仅供内部联调或受管设备使用，通过 ADB 设置 `ACTIVATE_VPN` 后再启动。Android 手册说明授权保留与重新授权边界。
- 雷鸟 App 版本升级为 `0.2.3`；增加源码接线测试，防止恢复自动弹窗、主线程 CONNECT-IP 或阻塞式销毁。

### 验证内容

- Android 单元测试验证雷鸟入口必须由用户显式触发、使用 Activity Result 回调、连接和 Native MASQUE 生命周期运行在 IO 调度器、取消竞态会回收 Native handle、销毁路径不含 `runBlocking`，且 ADB VPN 预授权保持为脚本可选项。
- 执行 RayNeo/Generic 单元测试、Lint 与 RayNeo Debug APK 构建，确认四 ABI Native Core、Mercury AAR 和专用 Manifest 正常打包。

## 2026-09-01 — 新增雷鸟 X3 Pro 发起方一键联调构建

### 修改原因

- 雷鸟 X3 Pro 眼镜端输入、滚动和精细点击不便，原通用 A/B App 的角色选择和长配置表单不适合现场佩戴联调。
- 本次部署已确定眼镜固定作为发起方 A，服务器地址为 `101.245.78.174`，AgentRuntime 和 MASQUE 端口分别为 `8088` 与 `8443/UDP`，没有必要每次手工填写。
- 雷鸟官方文档明确 X 系列使用左右两块物理屏和一块横向逻辑屏，普通单份 Android UI 会在左右眼显示不同半屏；仅简化普通 Activity 不能在真机上正确合目，也不能完整支持镜腿焦点交互。

### 修改方式

- Android example-app 新增 `rayneo` 产品构建，与原 `generic` A/B 构建隔离；专用包应用 ID 为 `com.rayneo.agent.example.rayneo`，可与通用包并存。
- 雷鸟构建固定角色 A 和上述服务器端点，保留 CONNECT-IP 标准路径、A2A 4001/28443、DNN `internet` 等既有联调默认值；应用启动即自动连接，首次使用只需确认系统 VPN 授权。
- 按官方 X 系列接入说明打包 Mercury ARDK v0.2.6：新增 `RayNeoApplication` 初始化 `MercurySDK`，Manifest 增加 `com.rayneo.mercury.app=true`，雷鸟入口继承 `BaseMirrorActivity`，通过 ViewBinding 同时生成并更新左右眼布局。
- 雷鸟入口启动后自动执行 Agent A 流程；镜腿前后/上下滑切换两个焦点、单击重试或在群组就绪后发送带序号的预置消息、双击停止退出。页面使用至少 16sp 字号、30dp/20dp 安全边距和高对比焦点框，无需眼镜软键盘。
- 通用 A/B 构建不改变行为；Android 手册新增雷鸟专用包的构建、安装及可选 Token 启动命令，example-app 版本升级为 `0.2.2`。

### 验证内容

- 单元测试校验雷鸟配置固定为角色 A、`101.245.78.174:8088` 和 MASQUE `8443`，并验证完整 URL。
- 执行通用/雷鸟两个构建的单元测试和 Lint，验证 ARDK/ViewBinding 源码编译与双 flavor Manifest 合并；生成同时包含 ARM64、ARMv7、x86 与 x86_64 Native Core 的雷鸟 Debug APK。

## 2026-08-31 — 校正状态3的能力更新与整卡重发语义

### 修改原因

- 上一版错误地把状态3的 `update_capabilities/updateCapabilities` 实现成“注销身份→重新申请→完整注册”，同时拒绝状态3再次调用 `register_capabilities/registerCapabilities`，与最终确认的接口语义相反。
- 能力增删不应改变 Agent 身份；只有用户明确再次发布整张 Agent Card 时，才需要替换身份并重新注册。

### 修改方式

- Python 与 Android 在状态3调用能力更新时直接请求 `POST /arf/v1/agent-cards-update`，成功后保持原 `AgentProfile` 和状态3，仅原子更新本地完整 Card 快照；失败时保留原状态和快照。
- 状态3再次调用能力注册时，先在本地构造并校验待发布内容，再依次执行 `deregister_identity → apply_identity → register_capabilities`。本地校验或注销失败仍在状态3，重新申请失败落到状态1，最终注册失败落到状态2；全部成功后回到状态3并通过 `local_profile/localProfile` 暴露新身份。
- 同步校正 Python/Android README、用户友好版设计文档和独立生命周期图；Python Wheel 版本升级为 `0.15.1`，Android example-app 升级为 `0.2.1`。A/B 自动联调仍按恢复状态补齐缺失步骤，状态3默认复用，只有业务显式再次调用注册接口时才触发整卡替换。

### 验证内容

- Python 单元测试覆盖状态3直接更新只产生一个更新请求，以及状态3再次注册严格产生“注销、申请、注册”三个请求。
- Android 单元测试覆盖相同两条路径；执行完整 Python/Android 测试、静态检查、Wheel 和 APK 构建验证。

## 2026-08-31 — 重画状态与资源生命周期视图

### 修改原因

- 用户友好版设计文档原第 7 章把三种稳定业务状态、`update_capabilities` 内部临时步骤和五套运行资源状态混在一张状态图及一段文本中，箭头含义不统一，无法快速判断合法调用和失败后的真实状态。
- 原说明还把平台并不完全一致的自动重连行为写成统一保证，超出了当前实现。

### 修改方式

- 将第 7 章拆为“Agent 业务三态”和“SDK 运行资源”两部分：第一张图只保留状态 1/2/3 及直接合法迁移；身份替换和能力更新改用表格列出内部顺序及每个失败落点。
- 第二张图只保留 `NEW → INITIALIZING → READY → CLOSING → CLOSED`，并用资源表明确 Agent TUN、本地 A2A 服务、MASQUE、WebSocket/HTTP、群组路由和媒体资源的取得、释放及持久化边界。
- 删除统一自动重连承诺，明确单个业务接口失败不关闭 SDK、初始化失败统一回滚、`close` 不删除 Agent 持久化状态；用户友好版文档升级为 V5.4.1，原始设计文档不改。
- 生成本地独立《SDK状态与资源生命周期》HTML 视图，用于直接审阅图形；该设计材料保持本地，不纳入远端代码仓库。

### 验证内容

- 使用 Mermaid CLI 分别渲染两张 Markdown 图，确认语法通过；使用 Chrome 实际渲染独立 HTML，检查箭头、文字、间距和图例无重叠。
- 校验原始《SDK设计文档》SHA-256 仍为 `d2509f323338d0cdb948ceff36e32c3ae71d59c646916b687168b4c2e862947b`。

## 2026-08-31 — Android A/B 建组后双向手动消息

### 修改原因

- 原 Android 联调 App 在 A 建组成功后自动向 B 发送固定消息，B 只能被动接收，无法在同一条群组链路上反复验证 A→B 和 B→A 双向通信。
- 联调人员需要在不重启 SDK、TUN、MASQUE 的情况下修改消息内容，并能在单次投递失败后继续发送。

### 修改方式

- A/B 收到并缓存群组配置后，从 SDK `GroupConfigSnapshot` 中以本端 Agent ID 排除自身并解析唯一对端；日志页显示“本端 → 对端”群组路由和手动消息输入区。
- 删除 A 建组后的自动首条消息，A/B 均由用户点击“发送消息”调用 `sendMessage`；目标 Agent ID 交给 SDK 继续解析对端 IP、端口和 `/A2A/message` URL，不在 App 暴露路由配置。
- 手动发送使用互斥保护，按钮在发送期间禁用；成功记录消息 ID 和投递回执，失败记录原因并恢复按钮，SDK、TUN、MASQUE 与本地 A2A 服务保持运行。
- 配置页为 A/B 分别提供默认消息草稿，example-app 版本升级为 `0.2.0`，Android 手册补充双向手动发送步骤。

### 验证内容

- example-app 单元测试覆盖 A/B 对端互选、非双成员群组拒绝、双端消息草稿及配置校验。
- 执行 Android SDK/example-app 单元测试、Lint、Debug APK 构建，并检查 APK 同时包含 ARM64、ARMv7、x86_64 与 x86 Native Core。

## 2026-08-31 — 持久化 Agent 身份与 Agent Card 三态生命周期

### 修改原因

- SDK 重启后原先只保留设备签名密钥，不保存网侧分配的 `AgentProfile` 和 Agent Card 发布状态，A/B 自动脚本会重复申请身份并重复调用能力注册接口。
- 同一服务器的不同 Runtime 端口对应不同 UE/PDU 会话，持久化状态必须隔离并绑定 Agent TUN IP，不能跨 UE 误用。

### 修改方式

- Python 与 Android SDK 新增 `NO_IDENTITY`、`IDENTITY_READY`、`CARD_PUBLISHED` 三态业务生命周期，与 SDK 连接状态分离；身份申请、能力注册、能力更新和身份注销仅在合法状态执行，成功后才原子迁移。
- 状态2再次申请身份时先以 `replaced` 注销旧身份再申请；状态3重复调用能力注册会在本地拒绝。状态3调用能力更新时按确认后的闭环执行“注销旧身份 → 重新申请身份 → `register_capabilities/registerCapabilities` 发布更新后的完整 Card”，不再调用增量更新端点。
- Linux 使用 `$XDG_STATE_HOME/agent-sdk/agents` 或 `~/.local/state/agent-sdk/agents` 的 `0700/0600` JSON 文件；Android 使用 `noBackupFilesDir/agent-sdk/agents`。记录按 Runtime IP/端口哈希分文件，并校验当前 Agent TUN IP；除 Profile 外同时保存身份申请上下文和上次成功发布的完整 `priority/vc_list`，用于状态3重建。
- Python A/B 自动脚本和 Android A/B App 根据恢复状态补做缺失步骤：状态1执行身份申请，状态2发布 Agent Card，状态3直接复用，避免重复 `register_capabilities/registerCapabilities`。
- Python/Android README 与本地《SDK设计文档-用户友好版》同步状态定义、迁移规则、存储位置和恢复流程；原始《SDK设计文档》保持不变。
- Python Wheel 因新增持久化状态文件格式和能力更新重建语义升级为 `0.15.0`。

### 验证内容

- Python 测试覆盖1→2→3、状态2重复申请替换、状态3按“注销→申请→完整注册”重建并回到3、状态2/3注销回到1、重复能力注册不产生 HTTP 请求、Runtime 端口隔离及 TUN IP 不匹配拒绝。
- Android 单元测试覆盖状态迁移、App 状态恢复分支与应用私有文件存储；执行 SDK/example-app 单元测试和源码编译。

## 2026-08-31 — Android 自动选择 MASQUE 本机出口地址

### 修改原因

- A/B 联调 App 要求用户填写本机 Wi-Fi/VLAN IP，但该地址只是 MASQUE 外层 QUIC 的源地址；只要系统存在到服务器的可达路由，就不应把网卡选择暴露成普通用户配置。
- Runtime 下行已经使用 WebSocket，A2A 地址由 Agent TUN IP 表示，因此 SDK 不再需要监听物理 IP 上的本地 TCP/UDP 服务。

### 修改方式

- Android SDK 在创建 Agent TUN 前对 MASQUE Server 执行 UDP 路由探测，取得系统选择的本机源地址，再用该地址创建、保护并绑定 QUIC Socket；`initialize.localVlanIp` 改为可选高级覆盖参数。
- 本地 A2A TCP/UDP 服务只绑定 `GET /v1/ue/info` 返回的 Agent TUN IP；初始化结果新增 `masqueOuterSourceIp` 用于日志与诊断。
- A/B App 配置页删除本机 Wi-Fi/VLAN IP，日志页显示系统实际选择的 MASQUE 出口；删除对应 SharedPreferences 和 ADB extra。
- 更新 Android/仓库使用手册，明确默认调用不传物理地址以及多网卡测试的可选覆盖方法。

### 验证内容

- Android SDK 单测覆盖自动路由选择、显式出口覆盖以及 A2A 服务只接收 Agent TUN IP；example-app 配置单测确认不再需要物理地址。
- 执行 Android SDK/example-app 单元测试、Lint 和 Debug APK 构建，并检查 APK 的四 ABI Native Core。

## 2026-08-29 — 新增 Android A/B 两页端到端联调 App

### 修改原因

- 原 Android example 只能通过 ADB extras 注入已有 `agent_id`，界面只有一个状态文本，无法像 Linux A/B 脚本一样由初次拿到 SDK 的联调人员完成参数配置、全流程调用和现场日志观察。
- Android 真机与模拟器的 Runtime/MASQUE 端口可能不同，需要在应用页面配置，且单接口失败后必须能保留 SDK 链路并继续联调。

### 修改方式

- 将 example-app 改为两页原生 Android 工具：第 1 页选择角色 A/B，并填写服务器地址、Runtime HTTP 端口、MASQUE QUIC 端口/路径、本地物理 IP、A2A 端口和测试参数；第 2 页提供深色分级日志、当前步骤状态、复制日志、重试当前接口和显式停止。
- 角色 B 自动完成初始化、身份申请、网络能力获取、能力发布、邀请接受和消息接收；角色 A 自动完成初始化、身份申请、能力注册、按能力发现 B、携带 DNN 建组、等待群组配置和定向 A2A 发送。
- 配置页不接收 Agent TUN IP，由 SDK 继续从 `GET /v1/ue/info` 获取；MASQUE Token 不持久化。业务接口异常由 App 捕获并停留在当前步骤，只有用户点击停止才关闭 SDK/TUN/MASQUE。
- 更新 Android 手册和仓库入口，加入 UI 使用方法、APK 构建安装命令以及 A/B 两套可选 ADB 预填参数。

### 验证内容

- example-app 配置单元测试覆盖角色端口默认值、MASQUE URL 拼装、A/B 字段差异和非法端口拦截。
- 执行 Android SDK 单元测试、example-app 单元测试和 Debug APK 构建，并检查 APK 同时包含 ARM64、ARMv7、x86_64 与 x86 Native Core。

## 2026-08-29 — Android MASQUE Native 支持 ARM 与 x86 全 ABI

### 修改原因

- 原 AAR 只打包 `arm64-v8a/libmasque_core.so`，无法在 ARMv7 设备以及 x86/x86_64 Android 模拟器或终端上加载 JNI 核心。

### 修改方式

- 新增统一 native 构建脚本，使用 Android NDK API 26 工具链交叉编译 `arm64-v8a`、`armeabi-v7a`、`x86_64` 和 `x86`；保留原 ARM64 脚本作为兼容入口。
- Android Library 明确声明四个 ABI filter，并将 `verifyMasqueNativeAbis` 接入 `preBuild`，任一 so 缺失时直接阻止 AAR/APK 构建。
- 修正一个遗留单测断言：现行群组配置只通过 `service_endpoints` 提供 TCP URL，不再携带 UDP 端口，缓存中的兼容字段应为 `0` 而不是旧值 `28443`。
- 更新 Android 使用说明、JNI 契约和仓库入口文档，明确真机与模拟器的 ABI 覆盖及全量/按需构建命令。

### 验证内容

- Go 单元测试、四 ABI 交叉编译、ELF 架构检查、Gradle 单元测试以及 AAR/APK 四 ABI 内容检查。

## 2026-08-28 — 隔离单接口失败并保持 SDK 业务链路运行

### 修改原因

- Agent A/B 在真实业务运行中可能遇到单次 Runtime HTTP 拒绝、超时或对端消息投递失败；该次失败必须可由应用捕获，不能导致 SDK 的 TUN、MASQUE、WebSocket 和本地 HTTP 服务退出。
- 应用在 A2A listener 收到消息后立即关闭时，旧关闭顺序会先停止上行转发和 MASQUE，存在本地 HTTP 200 已构造但响应包尚未送回对端就被丢弃的窗口。

### 修改方式

- 所有已装饰的公开业务接口在异常日志中增加 `sdk_state` 和 `sdk_kept_running`；READY 状态下额外记录 `interface_failure_isolated`，异常继续仅返回给当前调用方，SDK 不自动关闭或重试。
- 调整 `AgentSdk.close()` 顺序：先排空本地 A2A HTTP 服务，同时保留 TUN 上行泵、路由和 MASQUE；HTTP 服务结束后再依次释放转发资源。
- README 增加真实业务异常处理示例，明确应用捕获 `AgentSdkError` 后可继续调用接口，以及写请求不应由 SDK 自动重试的原因。

### 验证内容

- 新增回归测试：能力注册接口单次失败后 SDK 仍为 READY，HTTP、TUN、MASQUE 和 Runtime 均保持运行，随后能力发现接口可以成功调用。
- 新增关闭顺序测试：本地 HTTP 服务关闭时上行泵和 MASQUE 仍处于运行状态。

## 2026-08-28 — 建组接口增加必填 DNN

### 修改原因

- 最新联调契约要求 H-GROUP 的 `group_config` 必须携带非空 `dnn`，用于网侧选择对应的 PDU 会话和数据网络；上一提交按当时要求未由 SDK 发送该字段。

### 修改方式

- Python `create_group` 和 Android `createGroup` 增加无默认值的必填 `dnn` 参数，并在发送前拒绝空值或纯空白值。
- SDK 将调用方传入值原样写入 `POST /acf/v1/agents-grouping` 的 `group_config.dnn`；现有控制请求认证器会自动把该字段纳入 proof 覆盖的业务文档。
- Linux 全流程、交互式示例和 Agent A/B 联调示例增加 `--dnn` 配置并传入建组函数；同步更新 README 与两端单元测试。

### 验证内容

- Python/Android 测试覆盖合法 DNN 随建组请求发送以及空 DNN 在本地被拒绝。
- `python3 -m compileall -q src examples tests` 与全量 `pytest` 通过（89 passed）；Android 完整 Gradle 验证仍依赖外部 Android SDK 环境。

## 2026-08-28 — 按 ACN 全流程测试重新对齐发现与 A2A 接口

### 修改原因

- `/root/acn/acn_sdk_runtime_full_flow_test.py` 的 H-DISCOVERY 请求不包含 `task_id`，成功响应的 `agent_card` 使用 `service_endpoints + skills`；原 SDK 仍发送额外 `task_id` 并解析旧 `agent_ip/tcp_port/udp_port`。
- 现行 H-A2A 定义不包含消息级 `proof`，并要求直接调用群组配置中的完整 `service_endpoints`；原 SDK 仍附加 proof，并用 `agent_ip` 改写 URL 主机。
- 用户明确指定 `dnn` 不由端侧 SDK 实现，因此 H-GROUP 参数和请求体保持原设计，不跟随测试脚本增加 `group_config.dnn`。

### 修改方式

- Python/Android 的发现公开函数删除 `task_id`，HTTP 请求及外层 proof 待签业务文档同步删除该字段；`DiscoveredAgent` 改为 `agent_id + service_endpoints + skills + priority`。
- 群组缓存验证 `service_endpoints` 为完整 HTTP/HTTPS URL 后保存原值；`send_message` 直接调用该 URL，A2A 请求仅保留 `message_id/group_id/type/timestamp/payload/src_agent_id/dst_agent_id/task_id`，接收端拒绝旧 `proof` 等契约外字段。
- A/B、Linux 和离线全流程示例发布 Profile 时不再把 VC0 放入 `vc_list`，只传运营商 VC1 与可选能力 VC；同步更新两端测试和用户说明。

### 验证内容

- Python 测试覆盖 H-DISCOVERY 不发送 `task_id`、新 `agent_card` 响应解析、完整 `service_endpoints` 原值投递、A2A 无 proof 请求及旧 proof 拒绝。
- `python3 -m compileall -q src examples tests` 与全量 `pytest` 通过（88 passed）；Android 接口、模型和单元测试同步更新，完整 Gradle 验证仍依赖外部 Android SDK 环境。

## 2026-08-27 — 补齐 AgentCard service_endpoints 并消除 H3 SETTINGS 竞态

### 修改原因

- 真实 `POST /arf/v1/agent-cards` 返回 `invalid_service_endpoints`；现网 H-PROFILE 要求顶层 `service_endpoints`，原 SDK 请求体未携带。
- 测试能力 VC 仍使用旧 `CapabilityCredential + creator/signature_value + claims.capability`，而现网 IDM 校验 `AgentCapabilityCredential`、`claims.skill_name` 和 ACN JsonWebSignature2020。
- 偶发 CONNECT-IP HTTP 200 后立即返回 `peer did not negotiate HTTP/3 Datagram`；客户端直接读取 `received_settings or {}`，在 SETTINGS 尚未发布时会误判为服务端不支持。
- 现网群组配置成员已改为 `service_endpoints + agent_ip + skills`，原 SDK 仍要求 `tcp_port/udp_port/capabilities/did_key`，会在建组成功后的配置缓存阶段拒绝消息。

### 修改方式

- Python/Android 从 SDK 已知的 Agent TUN IP、本地 TCP 端口和固定 `/A2A/message` 自动构造 `service_endpoints`，纳入 `/arf/v1/agent-cards` 请求和外层 proof，不增加用户参数。
- 两端测试能力 VC 改为 `AgentCapabilityCredential`，能力写入 `claims.skill_name`，proof 改为 `assertionMethod` 的 ES256 分离 JWS。
- Python MASQUE 协议层增加 HTTP/3 SETTINGS future；收到 CONNECT-IP 2xx 后等待 SETTINGS，再检查并记录 `H3_DATAGRAM`，连接关闭时同步结束等待。
- Python/Android 群组缓存按最新字段解析 `service_endpoints/skills`；SDK 从完整 URL 提取协议、端口和路径，并强制使用已验证 `agent_ip` 作为实际目标地址，`send_message` 的用户参数保持不变。

### 验证内容

- Python 测试覆盖自动 `service_endpoints`、最新群组成员格式及隐藏式目标 URL 解析、标准能力 VC 自签自验与篡改拒绝、SETTINGS 晚于 CONNECT 响应时的等待行为。
- Python 源码编译和全量测试通过；Android 完成代码静态检查，Gradle 单测因当前环境未配置 `ANDROID_HOME`、不存在 `android/local.properties` 而未能启动（非源码测试失败）。

## 2026-08-27 — 内置联调三方私钥并解析 VC1 network_abilities

### 修改原因

- Agent B 在 WSL 中发布测试能力时仍默认读取 `~/lpx/cert/third-party/private-key.pem`，展开为不存在的 `/root/lpx/...` 后导致 `register_capabilities` 失败；仅打包公钥无法完成 VC 签发。
- 现网 IDM 将运营商能力放在 `vc1.claims.network_abilities[]`，原 Python 仅解析旧字段 `abilities/agent_attribute`，因此接口成功但日志显示 `abilities=[]`。

### 修改方式

- 按封闭测试要求，将匹配的三方 P-256 测试私钥作为 Wheel/AAR 相对资源 `certs/third-party-capability-private-key.pem` 打包；Python 默认直接读取包资源，Android `AgentSdk.create` 自动导入应用私有目录。
- Python `--third-party-private-key` 和 `--test-third-party-private-key` 改为仅用于可选覆盖，默认不再传入任何宿主机路径；Android example 删除应用侧私钥资源查找和手工导入。
- Python/Android 优先解析 `vc1.claims.network_abilities[]`，同时保留旧 `abilities/agent_attribute` 兼容；Agent B 日志明确输出 `network_abilities` 和 `valid_until`。

### 验证内容

- Python 测试验证包内公私钥匹配、默认无路径签发的 VC 可由包内公钥验证，并覆盖现网 `network_abilities` 响应解析。
- Python 源码编译、全量测试和 Wheel 内容检查通过；Android 按当前联调优先要求只做代码与资源静态检查，不等待完整 Gradle 构建。

## 2026-08-27 — 封闭联调暂时关闭 SDK 入站 proof 验签

### 修改原因

- 当前优先完成 AgentRuntime、核心网和 Agent A/B 用户面联调，群组配置与 A2A proof 的签名口径仍在同步；SDK 本地验签会在业务字段、缓存和链路验证前提前拦截消息。
- Runtime/核心网仍要求控制请求签名，因此不能删除出站 `signature/proof` 或改变请求体。

### 修改方式

- Python 默认使用明确的 no-op 群组配置/A2A verifier；Android 公共 `AgentSdk.create` 及内部默认值同步切换为 no-op verifier。
- 只绕过 `acf_group_config.proof` 与 A2A 入站 `proof` 校验，原有字段检查、目标检查、群组成员解析、缓存、路由和 listener 调用全部保留。
- 身份申请、控制面请求和 A2A 出站签名继续使用设备 P-256 私钥；真实验签实现、核心网内置公钥和单元测试继续保留，便于联调后恢复。
- Python 初始化日志新增 `inbound_signature_verification_disabled` 警告并标注 `internal-test-only`。

### 验证内容

- Python 新增无签名群组配置/A2A verifier 放行测试，并运行源码编译与全量测试。
- Android 按当前联调优先要求不等待全量构建；完成 Python/Android 默认依赖注入和代码静态检查。该配置明确禁止用于生产环境。

## 2026-08-27 — 三方能力认证公钥改为 SDK 包内相对资源

### 修改原因

- 三方能力 VC 的认证公钥不应依赖开发机 `/root/lpx/cert/third-party/public-key.pem`，否则 Wheel/AAR 安装到客户设备后无法按该绝对路径读取。
- 公钥可以随 SDK 分发，但三方签发私钥必须继续留在 SDK 包外。

### 修改方式

- Python 将 P-256 公钥打包为 `agent_sdk/certs/third-party-capability-public-key.pem`，并通过 `importlib.resources` 提供包相对读取。
- Android 将同一公钥打包为 AAR classpath 资源 `certs/third-party-capability-public-key.pem`，通过类资源流读取。
- 两端验证测试改为使用 SDK 内嵌公钥，不再读取宿主机三方公钥绝对路径；外部私钥导入和测试签发流程保持不变。

### 验证内容

- 校验 Python/Android 两份包内 PEM 的 SHA-256 均为 `dbe535abb61b36fba64ea1e90c4436a9af2a762c34afe936ade4b21e4d5bf06c`，且为 P-256 公钥。
- Python 测试验证包内公钥可读取，并在本机测试私钥存在时确认公私钥匹配；Wheel 构建产物检查确认实际包含该资源。Android 按此前联调要求未等待完整构建，仅校验 AAR 主资源目录、相对加载代码和 PEM 内容。

## 2026-08-27 — H-ID 签名对齐现网 IDM 的完整 Metadata JSON 编码

### 修改原因

- 真实 `POST /idm/v1/identity-applications` 已通过 AgentRuntime 本地校验并进入核心网，但 IDM 返回 `SIGNATURE_INVALID`。
- 核心网 `RegisterSigningBytes/hidSigningBytes` 将完整 `request.Metadata` JSON Container 作为一个 LP16 字段；原 SDK 将 `region/os/version` 分成三个 LP16，双方待签字节必然不同。

### 修改方式

- Python 和 Android 统一改为 `domain + owner/name/publicKey/description + timestamp + LP16(compact metadata JSON)`，HTTP 字段名、字段类型和签名算法不变。
- 两端在申请体中固定 `region→os→version` 顺序，额外字符串 metadata 字段按名称排序，签名与 HTTP 发送复用相同对象和紧凑 UTF-8 JSON 编码。
- metadata 校验对齐 IDM：保留三个必填非空字符串，允许额外字符串成员，拒绝非字符串值。
- 用户 README 和 Proof 说明同步更新 H-ID 待签字节与 metadata 顺序约束。

### 验证内容

- Python 和 Android 共享的新黄金向量为 204 字节，SHA-256 为 `97eea3ebc7f7d6018d789b285bf36c16d545698b2931987855881b799d7fea60`。
- 测试覆盖额外字符串 metadata 的确定性排序、非字符串拒绝和 H-ID 自签自验；Python/Android 全量测试、编译及 `git diff --check` 通过，原始《SDK设计文档》保持不变。

## 2026-08-27 — A/B 联调脚本改为默认连续执行

### 修改原因

- A/B 脚本默认在注册第一个监听器前等待回车，导致用户误以为程序没有执行且没有日志。
- SDK 文件日志在 `sdk.init()` 中初始化；阻塞在 `init` 之前时，本地日志文件自然不会出现初始化和 HTTP 记录。

### 修改方式

- `agent_a_test.py` 和 `agent_b_test.py` 改为默认连续执行，启动后立即输出 `TEST_STARTING`，各步骤继续输出 `STEP_AUTO` 及业务结果。
- 移除 `--no-prompt`，改为只有显式传入 `--prompt` 时才启用按回车逐步调用。
- MASQUE 建连增加服务端、物理源地址、当前阶段和超时时间日志；握手或 CONNECT-IP 等待超时时返回包含排查对象的明确错误，并消化 aioquic 在外层超时取消后产生的次生 shielded-future 异常；A/B 终端同步输出 `TEST_FAILED`、错误码及日志文件位置。
- 用户手册同步更新默认行为、可选交互参数和日志初始化时机。

### 验证内容

- 测试确认 A/B 参数默认 `prompt=False`，且原有发现、建组、发送、邀请接受和收包流程保持通过；新增 MASQUE 超时错误的阶段、端点和源地址断言。
- Python 源码编译、全量测试和 `git diff --check` 通过；原始《SDK设计文档》保持不变。

## 2026-08-27 — 增加 Agent A/B 能力发现、建组和消息联调脚本

### 修改原因

- 原有全流程示例只在一个进程内展示调用顺序，缺少两个真实端侧分别承担发起方和能力提供方角色的最小联调入口。
- 联调需要验证 A 确实按 B 发布的能力发现目标，并在群组配置到达后由 SDK 内部解析 B 的 IP/端口发送消息，而不是由用户配置路由信息。

### 修改方式

- 新增 `python/examples/agent_b_test.py`：B 初始化 SDK、申请身份、获取网络能力、发布指定测试能力，自动接受建组邀请并确认群组配置，收到消息后输出 `B_MESSAGE_RECEIVED`。
- 新增 `python/examples/agent_a_test.py`：A 初始化并发布自身身份/网络能力 Profile，按 `target_capability` 发现 B、排除自身、创建二人群组、等待目标成员进入群组缓存，再仅用 `group_id + target_agent_id` 调用 `send_message`。
- 两端主动调用默认采用“每次回车执行下一项”的交互方式；下行邀请和群组配置立即处理以保证事务连续性，同时提供 `--no-prompt` 自动执行选项。
- Python 客户指南补充 B 先启动、A 后启动的双终端命令、成功日志判据、同能力多目标选择和测试能力签名密钥说明。

### 验证内容

- 新增测试覆盖 A 的能力发现过滤、目标建组、群组缓存发送链路，以及 B 的能力发布、邀请接受和收包日志。
- Python 源码编译、脚本命令行解析和全量测试通过；`git diff --check` 通过；原始《SDK设计文档》保持不变。

## 2026-08-26 — 增加按回车逐接口执行的真实 Python Demo

### 修改原因

- 真实 `linux_agent.py` 会连续执行全部 SDK 接口，不便于联调人员在调用前检查网元状态、观察单步请求/响应日志或人工控制下一步时机。
- 若直接在异步流程中调用阻塞式 `input()`，等待回车期间会阻塞 MASQUE QUIC 保活、AgentRuntime 下行 WebSocket 和 A2A 监听，可能引入与待测接口无关的超时。

### 修改方式

- 新增 `python/examples/interactive_linux_agent.py`，复用 `linux_agent.py` 的真实参数、监听器、媒体示例适配器和业务流程；屏幕显示下一个函数及接口说明后，按一次回车只执行一步，输入 `q/quit/exit` 可安全终止。
- 为 `run_full_flow` 增加可选的异步 `before_step` hook，在不改变原有非交互示例行为的前提下覆盖 `init`、身份、能力、发现、建组、消息、卸载、媒体句柄和注销调用。
- 终端输入通过 `asyncio.to_thread` 执行，保证等待人工操作时事件循环和网络保活继续运行；正常流程最后也需按回车调用 `sdk.close`，异常或主动退出时直接清理资源。
- 根 README 和 Python 客户指南增加交互式 demo 入口、完整启动命令、操作方式和异步输入原因说明。

### 验证内容

- 新增测试覆盖“一次回车执行一个步骤”、显式退出和真实参数解析；现有全流程测试新增完整 hook 顺序断言。
- Python 源码编译和全量测试通过；wheel 构建成功，`git diff --check` 通过；原始《SDK设计文档》保持不变。

## 2026-08-25 — 增加 MASQUE QUIC 空闲连接保活

### 修改原因

- A/B 双实例脚本完成 `GET /v1/ue/info`、CONNECT-IP、TUN 和消息监听后，如果用户尚未通过 curl 配置对端或发送报文，aioquic 默认会在约 60 秒无 QUIC 流量后以 `Idle timeout` 关闭连接。
- 连接终止后，transport 的 `connected` 状态未及时变为 `false`，不利于脚本和上层 SDK 准确判断数据面状态。

### 修改方式

- Python `AioquicConnectIpTransport` 内置 15 秒 QUIC PING 保活任务；CONNECT-IP 成功后自动启动，关闭 transport 时自动取消，无需用户新增配置。
- 收到 QUIC 终止事件后，接收循环立即清除 `connected` 状态；保活发送异常记录 `masque_keep_alive_failed` 日志。
- Python MASQUE 双实例说明增加保活日志、默认间隔和进一步排查边界说明。

### 验证内容

- 新增测试覆盖保活间隔校验、连接期间周期发送 PING、PING 发送失败及 QUIC 关闭后的连接状态清除。
- Python 源码编译和全量测试通过（`74 passed`），`agent_connect_sdk-0.14.0-py3-none-any.whl` 构建成功，`git diff --check` 通过；原始《SDK设计文档》保持不变。

## 2026-08-25 — 修复 requirements 源码安装对 wheel 的强制依赖

### 修改原因

- 客户执行 `python -m pip install -r requirements.txt` 时，`-e .` 会创建隔离构建环境；原 `pyproject.toml` 将 `wheel` 声明为强制构建依赖。
- 客户当前 Python 软件源可以解析 `setuptools`，但不提供 `wheel`，导致 pip 在安装 SDK 及运行依赖之前以 `No matching distribution found for wheel` 失败。
- 基于 setuptools 的 PEP 517/PEP 660 构建不需要在 `build-system.requires` 中显式声明 `wheel`。

### 修改方式

- 从 `python/pyproject.toml` 的 `build-system.requires` 中移除多余的 `wheel`，仅保留 `setuptools>=68`。
- 保留 `requirements.txt` 的 `-e .`，客户仍可通过一条 `pip install -r requirements.txt` 同时安装运行依赖和当前 SDK 源码。
- Python README 明确源码构建的软件源要求，避免把发行格式名称 wheel 与必须安装的同名构建包混淆。

### 验证内容

- `pyproject.toml` 解析结果确认构建依赖仅包含 `setuptools>=68`，不再请求 `wheel`。
- Python 源码编译和全量测试通过（`70 passed`）；在不额外下载构建依赖的环境中成功生成 `agent_connect_sdk-0.14.0-py3-none-any.whl`；`requirements.txt` 与 `setup.cfg` 的五个运行依赖保持一致，`git diff --check` 通过。
- 原始《SDK设计文档》保持不变。

## 2026-08-24 — 增加 Python 一键依赖安装清单

### 修改原因

- 客户从源码运行双实例 MASQUE 验证脚本时，需要手工查找 `setup.cfg` 中的依赖并安装，同时还要确保源码包 `agent_sdk` 可被 examples 导入。
- 需要提供标准的 `pip install -r requirements.txt` 入口，减少首次安装步骤和漏装 `aioquic/pyroute2` 等运行依赖的风险。

### 修改方式

- 新增 `python/requirements.txt`，逐项列出与包元数据一致的正式运行依赖：`aiohttp`、`aioquic`、`cryptography`、`httpx` 和 `pyroute2`。
- requirements 最后一行使用 `-e .` 安装当前源码目录，使 `python/examples` 可以直接导入 `agent_sdk`，无需额外设置 `PYTHONPATH`。
- Python 客户指南增加创建虚拟环境并执行 `python -m pip install -r requirements.txt` 的完整命令，明确测试依赖仍通过 `-e '.[test]'` 单独安装；根 README 的快速验证入口同步增加 requirements 安装步骤。

### 验证内容

- `requirements.txt` 中五个三方依赖的名称和版本区间与 `python/setup.cfg` 的 `install_requires` 完全一致。
- 已在全新 Python 虚拟环境中实际触发 `python -m pip install -r requirements.txt`；当前构建机访问 `files.pythonhosted.org` 时发生 TLS EOF，未能完成联网下载。依赖文件解析、包元数据一致性和源码导入由本地测试覆盖。
- Python 全量测试通过（`70 passed`），`git diff --check` 通过；原始《SDK设计文档》保持不变。

## 2026-08-24 — 补充 Windows PowerShell 双 WSL 联调命令

### 修改原因

- 第 5.1 章只有进入 Ubuntu 后执行的 Bash 命令，Windows 用户仍需自行判断如何选择 A/B 对应的 WSL 发行版、传入仓库路径和调用各实例的本地控制接口。
- PowerShell 中 `curl` 可能是命令别名，且直接访问 WSL localhost 依赖系统端口转发配置；双实例联调需要明确请求实际进入哪个 Ubuntu 发行版。

### 修改方式

- Python 客户指南增加 Windows PowerShell 前置命令，使用 `wsl.exe --list --verbose` 确认发行版，并约定 Windows 仓库路径到 WSL 路径的映射。
- 为启动 B、启动 A分别增加 `wsl.exe -d Ubuntu-Agent-B/A --cd ...` 命令，保留各自 Runtime HTTP 端口、MASQUE/QUIC 端口和本地控制端口。
- 为 B 配置 A IP、A 配置 B IP以及触发 A 发送分别增加 PowerShell 命令；命令通过 `wsl.exe -d <发行版> -- curl` 在指定 Ubuntu 内执行，不依赖 Windows→WSL localhost 转发，也避免 PowerShell `curl` 别名差异。
- 补充可选说明：确认 localhost 转发可用时可使用原生 `curl.exe` 请求相同 URL，并建议用单引号保护 JSON。

### 验证内容

- Windows 与 Bash 示例中的 Runtime、MASQUE、消息端口、控制端口、URL 和 Agent IP逐项一致。
- README 中 JSON/JSONL 示例均通过标准解析，`git diff --check` 通过；本次仅修改文档，不修改脚本或 SDK 行为。
- 原始《SDK设计文档》保持不变。

## 2026-08-24 — 双实例脚本恢复 UE 查询并增加 curl 交互控制

### 修改原因

- 双实例验证仍应遵循 SDK 初始化时的地址来源：本机 Agent TUN IP必须由对应 AgentRuntime 的 `GET /v1/ue/info` 返回，不能作为脚本启动参数手工传入。
- 对端 Agent IP在本次测试中由用户已知并手工指定，但实例必须先保持运行，便于分别查看 A/B 的本机 IP，再通过 curl 配置对端和控制发送时机。
- 需要一条明确的 curl 请求触发 A向 B 的 `POST http://<B Agent IP>:4001/message`，并在 B 日志中保留可核验的收包正文和实际源地址。

### 修改方式

- 双实例脚本重新接收各自的 `runtime_ip + runtime_port`，复用 `HttpRuntimeTransport.get_ue_agent_ip()` 完整校验注册、NAS 安全状态及活动默认 IPv4 PDU Session；日志新增 `UE_INFO_AGENT_TUN_IP`，回显实际 `GET /v1/ue/info` URL、Agent TUN IP和 `/32` CIDR。
- 删除启动参数 `--local-agent-ip/--peer-agent-ip`；本机地址只来自 UE 查询。A/B 建立 TUN 与 MASQUE 后常驻，并分别使用默认本地控制端口 `18081/18082`。
- 新增本地测试控制接口：`POST /test/peer` 接收 `peer_agent_ip`，原子安装或替换唯一对端主机路由；`POST /test/send` 将 curl 的 JSON 请求体直接发送到已配置对端的 `POST /message`；`GET /test/status` 返回当前本机/对端 IP和 MASQUE 状态。
- 对端未配置前，入站 `/message` 和发送控制请求均拒绝处理；配置完成后，TUN 上下行仍只允许当前本机/对端 Agent IP对，B 收包时校验真实 TCP 源地址并打印 `MESSAGE_RECEIVED`。
- Python 客户指南更新为四步操作：启动 B、启动 A、分别 curl 配置对端 IP、curl A触发发送，并给出可直接复制的三条 curl 命令。
- 单元测试改为覆盖 Runtime UE 查询及日志、对端路由替换、动态 IP包过滤、curl 配置接口、curl 发送的精确目标 URL/消息体以及 B 侧收包日志。

### 验证内容

- `python3 -m compileall -q src examples tests` 通过。
- 双实例验证脚本测试通过（`6 passed`）；Python全量测试通过（`70 passed`）。
- `masque_two_instance_test.py --help` 显示 Runtime、MASQUE、控制接口和日志参数，不再接收本机/对端 Agent IP或 Agent ID 参数。
- README 中 JSON/JSONL 示例均通过标准解析，`git diff --check` 通过；原始《SDK设计文档》保持不变。
- 当前工作区未连接用户的外部 AgentRuntime/MASQUE/UERANSIM 环境，真实链路结果需按三条 curl 在两个隔离 Ubuntu 网络环境中执行确认。

## 2026-08-24 — 双实例验证改为已知 Agent IP 直连 `/message`

### 修改原因

- 双实例脚本只用于验证端侧 TUN、MASQUE CONNECT-IP、服务器用户面和对端 TUN 是否连通，不需要覆盖身份、发现、建群或业务 SDK 寻址流程。
- 上一版要求 B 先申请并输出 `agent_id`，A 再建群、等待 `acf_group_config` 后调用 `send_message()`，引入了与本次网络验证无关的控制面前置条件。
- 已确认验证协议为：A 已知 B 的 Agent IP，直接向 `http://<B Agent IP>:4001/message` 发送 JSON；B 打印日志证明收到。

### 修改方式

- 重写 `python/examples/masque_two_instance_test.py`，删除 AgentRuntime 控制端口、Agent ID、身份申请、邀请、群组配置、群组 ID 和 `AgentSdk.send_message()` 依赖。
- A/B 直接传入 `local_agent_ip + peer_agent_ip + masque_url`；脚本创建 `/32` 或 `/128` Agent TUN、安装唯一对端主机路由、建立各自 CONNECT-IP 会话，并只转发这对 Agent IP之间的报文。
- 两端都在本机 Agent IP 的 TCP 4001 端口注册 `POST /message`；角色 A 向对端固定 URL发送 JSON，角色 B 校验 TCP 源地址等于 A 的 Agent IP，打印并落盘 `MESSAGE_RECEIVED`，返回 `{"status":"OK"}`。
- 保留网络命名空间防短路检查、独立 TUN/密钥目录和本地滚动日志；客户指南中的 B/A 命令、参数、日志名称和验收标志全部改为 IP直连测试语义，并明确该 `/message` 仅是验证接口，不替代正式 SDK 的 `/A2A/message`。
- 重写脚本测试，覆盖无 Agent ID/Runtime 参数、相同网络命名空间拒绝、IP包双向过滤、A 请求的精确 URL/方法/JSON、B `/message` 响应与收包日志。

### 验证内容

- `python3 -m compileall -q src examples tests` 通过。
- 双实例验证脚本测试通过（`6 passed`）；Python 全量测试通过（`70 passed`）。
- `masque_two_instance_test.py --help` 不再包含 Agent ID、群组或 AgentRuntime 控制面参数，固定消息端口默认值为 `4001`。
- 当前工作区未连接用户的外部 AgentRuntime/MASQUE/UERANSIM 环境，真实 A→服务器用户面→B 结果仍需在两个隔离 Ubuntu 网络环境中执行确认。
- `git diff --check` 通过；原始《SDK设计文档》保持不变。

## 2026-08-24 — 新增 Ubuntu A/B 双实例 MASQUE 消息联调脚本

### 修改原因

- 现有 `linux_agent.py` 用于演示全部 SDK 北向接口，包含能力、发现和媒体流程，不适合快速验证两个真实端侧实例的 MASQUE 建连及 A→B 消息交付。
- 在同一个 Linux 网络命名空间启动两个实例时，两个 Agent TUN IP 都属于本机，Linux 可能直接本地交付 A→B 流量；只看到 B 收包不能证明报文经过 MASQUE、UERANSIM 和 5GC。
- 联调需要清楚给出 B 先启动、A 使用 B 的 Agent ID 建群、SDK 自动解析群组端点、B 打印收包证据的完整操作方式。

### 修改方式

- 新增 `python/examples/masque_two_instance_test.py`，同一脚本通过 `--role A/B` 运行两端：两端分别执行真实 `sdk.init()` 和身份申请；B 自动接受邀请并等待消息；A 创建双成员群组、等待已验签 `acf_group_config` 后调用 `send_message()`。
- B 收到消息时向控制台和本地滚动日志写入结构化 `A2A_MESSAGE_RECEIVED`，随后写入 `TEST_PASSED`；A 只有收到 B 的 `status=OK` 才记录 `A2A_MESSAGE_DELIVERED`。
- 脚本分别配置 A/B 的 AgentRuntime 控制端口、MASQUE URL/QUIC 端口、TUN 名称、状态目录和日志文件，并可用 `--expected-agent-ip` 校验 `/v1/ue/info` 返回值。业务消息仍只传 `group_id + target_agent_id`，不向用户暴露对端 IP、端口或路由。
- 增加网络命名空间 ID 输出及 `--peer-netns-id` 同空间拒绝检查，避免把内核本地短路误判为 MASQUE 联通；Python 客户指南补充 Windows/Ubuntu 两实例启动命令、日志位置和验收标志，根 README 增加脚本入口。
- 新增脚本测试，覆盖 A/B 独立默认配置、A 目标 ID 必填、A 建群并发送、B 打印和落盘收包证据以及 SDK 关闭。

### 验证内容

- `python3 -m compileall -q src examples tests` 通过。
- Python 全量测试通过（`69 passed`）。
- `masque_two_instance_test.py --help` 可正常运行并显示 A/B、Runtime/MASQUE、网络命名空间、消息和日志参数。
- 当前工作区未连接用户的外部 AgentRuntime/MASQUE/UERANSIM 环境，因此真实 A→5GC→B 结果需按客户指南在两个隔离 Ubuntu 网络环境中执行确认。
- `git diff --check` 通过；原始《SDK设计文档》保持不变。

## 2026-08-24 — 将 MASQUE 端口分叉点移入服务器内部

### 修改原因

- 上一版虽然删除了共享 Proxy 节点，但把交换机直接连到 AgentRuntime A/B，视觉上等同于在服务器外形成两条物理接入链路。
- 实际网络只有交换机到服务器物理网卡的一条链路；A、B 的 QUIC/UDP 报文进入同一服务器后，才根据不同目的端口交给对应 AgentRuntime。

### 修改方式

- 本地《SDK设计文档-用户友好版》升级到 V5.3.3，在服务器边界内增加 `服务器物理网卡` 节点；交换机只连接该节点一次，再由服务器网卡按目的 UDP 端口 A/B 分叉到两个带独立路由域背景的 AgentRuntime。
- 独立《SDK MASQUE CONNECT-IP 架构图》采用相同结构：Wi-Fi/交换机到服务器网卡使用一条直线，端口 A/B 分叉线全部位于服务器虚线边界内部。
- 仅调整架构表达，不修改 SDK 或服务器代码，不改变 A→`uesimtun0`→5GC→`uesimtun1`→B 的数据路径。

### 验证内容

- 用户友好版全部 10 个 Mermaid 图均通过 Mermaid CLI 11.15.0 语法渲染；物理图 PNG 检查确认交换机到服务器只有一条线，分叉发生在服务器物理网卡之后。
- 独立 HTML 架构图完成 Google Chrome 无头浏览器截图检查，服务器入口、端口 A/B 分叉线和两个 AgentRuntime 路由域均无重叠或截断。
- `git diff --check` 通过；原始《SDK设计文档》保持不变。

## 2026-08-24 — 纠正 MASQUE 物理视图的双 AgentRuntime 端口拓扑

### 修改原因

- 旧物理视图在 Wi-Fi/交换机与 AgentRuntime 之间绘制了一个共享 `MASQUE Proxy` 节点，错误暗示设备 A、B 先汇聚到同一个代理会话，再由代理选择 AgentRuntime。
- 实际组网是设备 A、B 分别连接服务器上 AgentRuntime A、B 的两个独立 MASQUE/QUIC 端口；两个会话各自绑定对应 UE 路由域和 `uesimtun0/1`。

### 修改方式

- 本地《SDK设计文档-用户友好版》升级到 V5.3.2；部署拓扑删除共享 Proxy 方框，从交换机画出两条 CONNECT-IP 分叉线，分别直连带独立背景的 AgentRuntime A/端口 A 和 AgentRuntime B/端口 B。
- 同步修正 A→5GC→B 报文图、逻辑视图、初始化时序、配置责任表和场景时序：A 的 Datagram 直达 AgentRuntime A 的 MASQUE 端口 A，经 `uesimtun0`、UERANSIM/5GC、`uesimtun1` 后，由 AgentRuntime B 的端口 B 回传给 B。
- 独立《SDK MASQUE CONNECT-IP 架构图》删除共享 Proxy 子图，改成交换机后的两条分叉线和两个 AgentRuntime MASQUE 端口，并清理内层路径中的 Proxy 入站/出站节点。
- 服务器实现责任边界保持不变：AgentRuntime、UERANSIM 和 5GC 均为外部系统，本 SDK 不实现或修改服务器代码；原始《SDK设计文档》保持不变。

### 验证内容

- 用户友好版全部 10 个 Mermaid 图均通过 Mermaid CLI 11.15.0 语法渲染；物理部署图另行生成 PNG 并确认两条分叉线、两个独立 UE 路由域及端口 A/B 无重叠或截断。
- 独立 HTML 架构图完成 Google Chrome 无头浏览器截图检查，确认共享 Proxy 节点已移除，设备 A/B 分别进入 AgentRuntime A/B 的 MASQUE 端口。
- `git diff --check` 通过；原始《SDK设计文档》SHA-256 仍为 `d2509f323338d0cdb948ceff36e32c3ae71d59c646916b687168b4c2e862947b`。

## 2026-08-24 — 纠正 H-COMPUTE 签名契约，保留 timestamp 和 proof

### 修改原因

- 上一提交 `fd369ae` 正确将算力卸载业务字段改为 `request_id + agent_id + workload_type`，但错误地将 Android 已有的 `timestamp + proof` 视为应删除字段，并使 Python 也绕过了控制请求签名层。
- 最终契约明确 H-COMPUTE 虽属于用户面/IP 业务且不进入控制面 NAS，仍必须用端侧设备私钥生成 `timestamp + proof` 进行请求防篡改。

### 修改方式

- Python `create_offloading_session` 和 Android `createOffloadingSession` 恢复调用各自的控制请求认证器；线路请求体固定包含 `request_id`、`agent_id`、`workload_type`、`timestamp`、`proof`，并可选包含 `preferred_sandbox_id`。旧 `task_type` 仍被禁止。
- H-COMPUTE 复用已有 ACN JsonWebSignature2020 兼容 Profile：`proof_purpose=authentication`，签名业务文档排除 `request_id` 和整个 `proof`，包含 `timestamp`及其他实际出现的算力请求字段。
- 两端契约测试改为必须出现 `timestamp + proof`，同时继续校验 UUID `request_id`、`workload_type`和旧字段排除。
- 《Agent SDK HTTP 接口文档》升级到 V1.10.1，用户友好版设计文档升级到 V5.3.1，Proof 说明升级到 V1.1；Git 仓库外的 `/root/acn/ACN消息接口定义.md` 升级到 3.29，三处都明确 H-COMPUTE 必须签名。原始《SDK设计文档》保持不变。

### 验证内容

- Python `compileall` 和全量测试通过（`64 passed`）；Android JVM 单元测试 `27 tests / 0 skipped / 0 failures / 0 errors`，Release AAR 和 example Debug APK 构建成功。
- `agent_connect_sdk-0.14.0-py3-none-any.whl` 通过 `twine check`；全新虚拟环境安装显示 `0.14.0`，全流程自检输出 `FULL FLOW DEMO PASSED`，`pip check` 无缺失或冲突依赖。
- Wheel、Release AAR、example Debug APK 的 SHA-256 分别为 `82c974de47843af2e591c10064cfb33d9b6d4ddf3afe2f1d12634c5446b5df69`、`91d25869862d4f03292f65ab74febc916bf46a88d49fbf1c9dce421e0d10d692`、`aef1fb102599e59a6a49c8c6d220f86a207ed1547d3a57b86d60734ec8da84b5`。

## 2026-08-24 — 算力卸载请求对齐 ACN，其余 ACN 契约对齐 SDK

### 修改原因

- 本次契约裁决明确：只有 H-COMPUTE 应由 SDK 跟随 ACN 定义；其他差异均应由 `/root/acn/ACN消息接口定义.md` 跟随已确认的 SDK 契约。
- Python 原 H-COMPUTE 只发送 `agent_id + task_type`，缺少 `request_id`；Android 除使用同样的旧字段外，还通过通用控制面认证层额外添加 `timestamp + proof`，与 ACN 用户面会话请求不兼容。
- ACN 文档其余章节仍残留旧 proof 向量、旧邀请 `group_id`、仅含成员表的 N-12、Runtime WebSocket A2A 和缺少任务字段的发现消息，会导致 Runtime/核心网按过期结构实现。

### 修改方式

- Python Wheel 升级到 `0.14.0`；`create_offloading_session` 的公开参数改为 `workload_type`，请求体固定生成 `request_id + agent_id + workload_type`，按需增加 `preferred_sandbox_id`，不生成 `timestamp/proof`。Linux 全流程示例和命令行参数同步改为 `--offloading-workload-type`。
- Android `createOffloadingSession` 的公开参数改为 `workloadType`，直接构造与 Python 相同的 H-COMPUTE 请求，不再调用会添加控制面 `timestamp/proof` 的 `authenticateControl`。
- Python/Android 新增线路契约断言：校验 UUID `request_id`、`workload_type`、可选沙箱字段，并显式拒绝 `task_type/timestamp/proof`；Linux example 测试同步检查新参数。
- 本地《Agent SDK HTTP 接口文档》升级到 V1.10.0，《SDK 设计文档-用户友好版》升级到 V5.3；两份文档说明 H-COMPUTE 的完整线路请求，并如实说明 A2A 当前不自动重试、不维护 `message_id` 去重缓存。原始《SDK设计文档》保持不变。
- Git 仓库外的 `/root/acn/ACN消息接口定义.md` 升级到 3.28：除 H-COMPUTE 保留 ACN 裁决外，其余对齐 SDK 的 proof 双摘要分离 JWS、WebSocket 初始化、发现任务字段、邀请 `group_name`、完整 `acf_group_config`、N-13 `ACK/REJECT` 以及 SDK 间直接 HTTP A2A；删除会误导实现的过期定长 NAS 向量。

### 验证内容

- Python `compileall` 和全量测试通过（`64 passed`）；Wheel 和 sdist 构建成功，`agent_connect_sdk-0.14.0-py3-none-any.whl` 通过 `twine check`，全新虚拟环境安装显示版本 `0.14.0`，内建全流程自检输出 `FULL FLOW DEMO PASSED`，`pip check` 无缺失或冲突依赖。
- Android JVM 单元测试共 `27 tests / 0 skipped / 0 failures / 0 errors`；Release AAR 和 example Debug APK 构建成功。
- Wheel、Release AAR、example Debug APK 的 SHA-256 分别为 `c3426904b8b62385a56fac41aef9310468c22433535d268bba4da9893be1e870`、`bfcc3a39dcd4858c3acaf2ed6d2327b9fc755027408a79f0474e07302717e2b3`、`a7f37df62c2701f7a9c64da04474b5a69e15e8d2ed9b9fba95dfd4ef735edd78`。
- HTTP 接口文档 21 个 JSON、用户友好版 4 个 JSON、Proof 说明 8 个 JSON 和 ACN 定义 81 个 JSON 全部通过标准解析；ACN 10 个 Mermaid 图全部通过语法渲染。
- `git diff --check` 通过；原始《SDK设计文档》SHA-256仍为 `d2509f323338d0cdb948ceff36e32c3ae71d59c646916b687168b4c2e862947b`。

## 2026-08-24 — SDK 移除服务器实现并将物理视图适配层改为 AgentRuntime

### 修改原因

- MASQUE Proxy、服务器 AgentRuntime、UERANSIM 和 5GC 已明确由外部系统负责，端侧 SDK 不应继续交付服务器程序、配置模板、证书或服务器专用测试。
- 旧 Wheel 仍包含 `MasqueProxyServer`、服务器 CLI 和 Linux UE TUN 适配代码，与“SDK 只实现端侧 Client”的责任边界冲突。
- 物理视图使用 `UE Adapter A/B` 描述服务器路径，与实际由 AgentRuntime 对接 `uesimtun0/1` 的组网表达不一致。

### 修改方式

- Python Wheel 升级到 `0.13.0`；删除服务器 `proxy.py/cli.py`、服务器控制台命令、配置模板、服务器示例、三组服务端专用测试和服务端证书部署材料，只保留 `masque.py` CONNECT-IP Client。
- 删除 Python 和 Android 中未使用的预置 MASQUE Root CA；两端 Client 的 TLS SNI 改为直接使用 `masque_server_url` 的主机名，不再绑定历史服务端名称。Android vendored CONNECT-IP 依赖裁剪为 Client 子集，删除其中的 Proxy/请求处理源码和依赖它的本地服务端测试，再重新编译 ARM64 `libmasque_core.so`。
- 根 README、Python/Android 指南明确仓库只交付端侧 SDK；客户只配置 AgentRuntime 地址、MASQUE URL 和可选鉴权值，不安装或配置任何服务器程序。
- 本地《SDK设计文档-用户友好版》升级到 V5.2：删除服务器配置、转发伪代码、服务器开发树和服务器进程视图；物理视图及 A→5GC→B 路径中的 `Adapter A/B` 改为 `AgentRuntime A/B`。
- 独立《SDK MASQUE CONNECT-IP 架构图》同步显示 `Proxy → AgentRuntime A/B → uesimtun0/1 → UERANSIM/5GC`，并把服务器标为外部系统而非 SDK 交付物。
- 工作区内被忽略的旧服务端私钥移动到系统回收目录 `/root/.local/share/Trash/files/orange-sdk-masque-server-key-20260824.pem`，未永久删除。

### 验证内容

- Python `compileall` 和全量测试通过（`64 passed`）；Android Native Go 测试通过，Android JVM 单元测试 `27 tests / 0 skipped / 0 failures / 0 errors`，Release AAR 构建成功。
- `agent_connect_sdk-0.13.0-py3-none-any.whl` 通过 `twine check`；全新虚拟环境安装版本为 `0.13.0`，`agent-sdk-self-check` 输出 `FULL FLOW DEMO PASSED`，`pip check` 无依赖问题。
- Wheel 内容检查确认不包含 `proxy.py`、`cli.py`、服务端控制台命令或 MASQUE Root CA；Wheel SHA-256 为 `5d180e9ef51a51597ce214546e812362af37fa2307d56a158629f7bc603e76ee`。
- Android ARM64 Native 库和 Release AAR SHA-256 分别为 `1283a9c8f0376cc7069f381fcd699f8753f9b62949bd5549cfe34e0c71fb2ea2`、`229a0587bd7eacbdb1e5f3e5404218534f9247a78db7ad6789d871327f3bf5c5`；Native 库不再包含历史服务端名称。
- 用户友好版 10 个 Mermaid 图全部成功渲染，4 个 JSON 示例通过标准解析；独立 HTML 架构图完成浏览器截图检查，未发现重叠或截断。
- `git diff --check` 通过；原始《SDK设计文档》SHA-256 仍为 `d2509f323338d0cdb948ceff36e32c3ae71d59c646916b687168b4c2e862947b`。

## 2026-08-24 — 用户友好版补齐 MASQUE 物理视图与 Proof 声明

### 修改原因

- 用户友好版虽然已经描述 CONNECT-IP 数据泵，但原物理视图只用一张简图概括端到端路径，未清楚区分端侧配置、服务器 Proxy/UE 适配配置和必须经过的 5GC 用户面路径。
- 原文只有 proof 双摘要公式摘要，缺少线上声明字段、proofOptions 与业务文档的精确边界、RFC 7797 分离 JWS 编码、可信公钥选择和当前防重放能力边界，无法直接支持跨端联调。
- 原物理图同时表达部署和报文方向，节点与连线容易拥挤；需要按架构图的层级和单一阅读方向重新组织。

### 修改方式

- 将本地《SDK设计文档-用户友好版》升级到 V5.1；原始《SDK设计文档》保持不变。
- 物理视图拆为“部署拓扑”和“A 经 5GC 用户面发送到 B”两张 Mermaid 图：明确设备 A/B 的 Agent TUN 与物理 IP、双 Wi-Fi/交换机、独立 CONNECT-IP 会话、每 UE Adapter、`uesimtun0/1`、UERANSIM gNB、N3/GTP-U 和 5GC UPF。
- 明确内层包在端侧已经使用 Agent IP，服务器不做隐式 SNAT/DNAT；A 的包必须从 `uesimtun0` 进入 UERANSIM/5GC，再从 `uesimtun1` 出来并由会话 B 回传，禁止宿主本地路由短路。
- 增加 SDK 内部自动配置与 SDK 外部部署配置责任表，说明 `masque_server_url` 已包含 QUIC 服务端口，`local_udp_port` 是 Agent 业务端口，并明确服务端无需安装端侧 SDK。
- 增加完整 proof 章节：保留 `verification_method/proof_purpose` 字段名，声明当前为 ACN JsonWebSignature2020 兼容 Profile；说明 proof 五个字段、场景用途、排序紧凑 JSON、`proofHash || documentHash`、ES256 原始 `r || s`、可信公钥来源、旧 DER 兼容分支及基础验签不包含时间窗/重放缓存。
- 同步纠正文档中已超出当前实现的防重放表述：群组配置只拒绝不比已提交快照更新的时间戳，A2A 当前未维护 `message_id` 去重缓存。

### 验证内容

- 用户友好版全部 11 个 Mermaid 图通过 Mermaid CLI 语法解析并生成 SVG；新增两张物理图另行生成 PNG 完成布局检查。
- 文档中的 4 个 JSON 代码块全部通过标准 JSON 解析；`git diff --check` 通过。
- 《SDK设计文档-用户友好版》继续命中 `.gitignore`，不会进入远端；原始《SDK设计文档》SHA-256 仍为 `d2509f323338d0cdb948ceff36e32c3ae71d59c646916b687168b4c2e862947b`。

## 2026-08-21 — 新增 Proof 生成与校验独立说明

### 修改原因

- 双摘要签名输入已经在 Python 和 Android SDK 中落地，但 README 只给出了公式，无法独立支持 AgentRuntime、核心网和其他 Agent 开发者完成逐字节联调。
- 需要明确区分 proof 生成规则、可信公钥选择和上层时间/重放策略，避免接入方从未验签消息的 `verification_method` 自选公钥，或误以为基础验签函数已经完成全部业务授权和防重放。
- 现有消息字段固定为 snake_case，规范化采用项目排序紧凑 JSON；文档需要明确它是 ACN 兼容 Profile，而不是把当前实现误写成采用 URDNA2015/camelCase 的完整 W3C JsonWebSignature2020。

### 修改方式

- 新增《Agent SDK Proof 生成与校验说明》，分别说明适用范围、proof 五个字段、设备密钥生命周期、三类消息的信任锚、待签业务文档、proofOptions、JSON 规范化、`proofHash || documentHash`、RFC 7797 分离 JWS、ES256 `r || s` 编码和验签步骤。
- 文档提供跨平台黄金向量、控制面请求的 `request_id` 排除规则、群组配置与 A2A 的验签公钥来源、生成/校验伪代码、常见对接错误和 Python/Android 实现位置。
- 如实记录当前兼容和安全边界：无句点 `jws` 的历史 DER Base64 验签分支仍存在；基础 proof 验签只要求 `created` 非空，不负责绝对时间窗、重放缓存或业务授权。
- 根 README 增加该文档入口；SDK 代码、线上字段、签名字节和版本号均未修改。

### 验证内容

- 文档中的 8 个 JSON 代码块全部通过标准 JSON 解析。
- 按文档重新计算的 `proofHash` 为 `1a96f0c94b92eaa51b8fb1de55b1842584e66a24be9af373507bd956581ab0b3`，`documentHash` 为 `31126a50a843b70e3b740f33884f6d0dc38054a942753600f9546c10a67122c1`，拼接结果为 64 字节，与 Python/Android 自动测试黄金向量一致。
- `git diff --check` 通过；本次仅修改 Markdown，无需重新构建 Wheel、AAR 或 APK。

## 2026-08-21 — proof 签名改为 proof 与业务文档双摘要拼接

### 修改原因

- 旧实现先把不含 `jws` 的 proof 元数据放回业务文档，再对整个 JSON 直接生成分离 JWS；这不符合已确认的 `Hash(Canonicalize(proofOptions)) || Hash(Canonicalize(业务文档，不含 proof))` 签名数据结构。
- `proofOptions` 不应嵌入待签业务文档，但 `type/created/verification_method/proof_purpose` 仍必须受签名保护，避免攻击者在不修改业务字段的情况下替换验签方法、用途或创建时间。
- 当前 AgentRuntime/NAS 消息契约已经固定 snake_case 字段名，因此本次只调整签名字节，不把 `verification_method/proof_purpose` 改为 camelCase。

### 修改方式

- Python 与 Android 新增一致的 proof 签名数据生成逻辑：移除 `proof.jws` 得到 `proofOptions`，从业务文档完整移除 `proof`，分别按既有递归字段排序、紧凑 UTF-8 JSON 规则规范化并计算 SHA-256，最后按 `proofHash || documentHash` 拼接为固定 64 字节 `verifyData`。
- ES256 分离 JWS 强制使用 `b64=false` 和 `crit=["b64"]`，验签不再接受编码载荷变体；JWS 实际签名输入改为 `BASE64URL(protectedHeader) + "." + verifyData`。签名和验签路径同步修改，控制请求的 HTTP `request_id` 继续只做幂等关联，不进入业务文档摘要。
- 线上 proof 仍输出和校验 `type/verification_method/proof_purpose/created/jws`；测试显式断言没有改成 `verificationMethod/proofPurpose`。
- 两端增加同一个跨平台黄金向量：proof 摘要 `1a96f0c94b92eaa51b8fb1de55b1842584e66a24be9af373507bd956581ab0b3` 在前，文档摘要 `31126a50a843b70e3b740f33884f6d0dc38054a942753600f9546c10a67122c1` 在后；同时覆盖业务字段篡改和 `verification_method` 篡改拒绝。
- 该签名输入与旧版本线协议不兼容，Python Wheel 升级为 `0.12.0`。根 README、Python/Android 指南同步；被 Git 忽略的本地《Agent SDK HTTP 接口文档》和《SDK 设计文档-用户友好版》分别更新为 V1.9.0 和 V5.0，原始《SDK设计文档》保持不动。

### 验证内容

- Python `compileall` 和全量测试通过（`70 passed`）；`agent_connect_sdk-0.12.0-py3-none-any.whl` 通过 `twine check`，独立虚拟环境安装显示版本 `0.12.0`，`agent-sdk-self-check` 输出 `FULL FLOW DEMO PASSED`。
- Android JVM 单元测试共 `27 tests / 0 skipped / 0 failures / 0 errors`；Release AAR 和 example Debug APK 构建成功。
- Wheel、AAR、APK SHA-256 分别为 `29c695c8a835a9b0add53eb046becf35f48b5d9684108715c9ece698014350b0`、`58e31f483f7236f8e8df71f2ee3e7fe59aecc95df13fcc55c6a1c2d3750b88b3`、`d83e7cc9d307957192249e459ca87ef9ac163215a499f18a8e604e055eb9621d`。
- HTTP 接口文档 21 个 JSON 示例、用户友好版设计文档 2 个 JSON 示例均通过标准解析；原始《SDK设计文档》SHA-256 仍为 `d2509f323338d0cdb948ceff36e32c3ae71d59c646916b687168b4c2e862947b`。

## 2026-08-21 — SDK 对齐 ACN HTTP、身份签名与 A2A 新契约

### 修改原因

- 新版端到端流程明确以详细接口定义为准：初始化查询使用 `GET /v1/ue/info`，控制面写请求由 SDK 生成 `request_id`，身份申请采用固定字段二进制编码签名，其他控制请求使用 `proof`。
- 旧实现只有能力更新携带带 `urn:uuid:` 前缀的请求 ID，身份申请仍签规范化 JSON，注销和能力注册仍使用旧 `signature` 字段，无法与 N-01 及新版 Runtime 契约逐字段对应。
- 群组配置下行消息类型和 A2A 字段/确认格式发生变化；旧实现仍识别 `ACN_AGENT_GROUP_CONFIG`/`ACF_GROUP_CONFIG`，并发送 `sender_agent_id/target_agent_id`、等待 `ack=true`。
- 输入文档的汇总表与详细章节存在冲突；路径大小写敏感，因此实现遵循详细 §3.4 的小写 `/v1/ue/info`，下行遵循步骤 7 的 WebSocket，不恢复旧端点注册或回调 POST。

### 修改方式

- Python 与 Android 的身份、注销、网络能力、AgentCard 注册/更新、发现和建群七个控制请求统一生成普通 UUID `request_id`；删除能力更新的 `request_type` 和 `urn:uuid:` 前缀。一次公开方法调用只生成一次请求 ID，认证层将其从签名输入中排除。
- 身份申请的 `description` 及 `metadata.region/os/version` 改为必填。两端新增完全一致的 `ACN-H-ID-v1\0` 域分隔、LP16、U64BE 编码器；公钥先按标准 Base64 解码为 P-256 SPKI DER，时间戳严格限制为 UTC `Z` 且不超过毫秒，签名为 ECDSA P-256/SHA-256 ASN.1 DER 标准 Base64。
- 注销和 AgentCard 注册改为 `proof`；网络能力默认 intent 改为 `Issue Network Ability Credential`，Android 同步支持从 `vc1.claims.agent_attribute` 构造能力说明。注销原因严格限定为新版七个枚举值。
- WebSocket 群组配置只接受 `ACN_AGENT_GROUPING_NOTIFICATION`，不再用 payload 内容或旧消息类型做兼容推导。邀请继续使用 `ACN_AGENT_GROUPING_INVITATION`。
- A2A 继续由 SDK 按 `group_id + target_agent_id` 从已验签缓存解析目标 IP/端口，但线上业务字段改为 `type/timestamp/payload/src_agent_id/dst_agent_id/task_id`；保留 `group_id/message_id/proof` 用于安全快照定位、回执和防篡改。接收成功统一返回 `{"status":"OK"}`，不再返回 `ack=true`。
- Python Wheel 版本升级为 `0.11.0`；Linux 全流程示例增加 `--region` 和 `--message-type`，身份元数据、注销原因和 A2A 调用均更新。根 README、Python/Android 指南同步。被 Git 忽略的本地《Agent SDK HTTP 接口文档》和《SDK 设计文档-用户友好版》同步到 V1.8.0/V4.9；原始《SDK设计文档》未修改。

### 验证内容

- Python `compileall` 和全量测试通过（`68 passed`）；Android JVM 单元测试共 `26 tests / 0 skipped / 0 failures / 0 errors`。两端共享的身份编码黄金向量长度为 174 字节，SHA-256 为 `483881296c5966469dcc901c15e7ff1c970644d7cb81446493f6178837e47a03`。
- `agent_connect_sdk-0.11.0-py3-none-any.whl` 通过 `twine check`；独立虚拟环境安装显示版本 `0.11.0`，`agent-sdk-self-check` 完整执行初始化、身份、能力、发现、建群、群组配置、A2A、算力和注销并输出 `FULL FLOW DEMO PASSED`。
- Android Release AAR 和 example Debug APK 构建成功。Wheel、AAR、APK SHA-256 分别为 `c2007e3a6d671645d7d0e9f80883fa5cec5b5af257b46877fc7aeba61007517c`、`c58486d12fd89522df49fd0755e4b979c1ff15bf1f0587baaaaaeafab0341eb6`、`693eee4302dfda34918bcdae752eeae76178aa12d0259dbca02654255567fa26`；Wheel/AAR 不含私钥，APK 仅含私钥解析器所需的 PEM 边界字面量，不含私钥材料或测试私钥资源。
- HTTP 接口文档 20 个 JSON 示例、用户友好版设计文档 2 个 JSON 示例全部通过标准解析。

## 2026-08-21 — SDK 初始化改为查询 UERANSIM UE 信息

### 修改原因

- 上一提交将用户误粘贴的下行 WebSocket 协议当作了 `sdk.init` 的 UE IP 获取接口，导致公开 API 需要额外传入 `agent_tun_cidr`。
- 正确的初始化查询是 `GET /v1/ue/info`；响应中活动 IPv4 PDU Session 的 `ipv4` 就是本机 Agent TUN IP。
- 该接口是无请求体的状态查询，仍不需要向 AgentRuntime 同步端侧物理 IP、端口或自行分配的 TUN 地址。

### 修改方式

- Python `AgentSdk.init()` 和 Android `AgentSdk.initialize()` 删除公开的 `agent_tun_cidr`/`agentTunCidr` 参数，初始化时先对同一 AgentRuntime IP/端口发起无 body 的 `GET /v1/ue/info`。
- 两端统一要求 `nas.registered=true`、`nas.state=session_ready`、`nas.security_context=true`；只选择活动 IPv4 PDU Session，优先且要求唯一的 `default_route=true` Session。
- 对所选 `ipv4` 做 IPv4 字面量校验和规范化，按点到点 TUN 构造 `<ipv4>/32`；无可用 Session、多个默认 Session 或非法 IP 均使初始化明确失败。
- 原有 `/v1/acn/downlink-websocket` 仍作为独立的核心网主动下行通道保留；初始化顺序为 UE 信息查询、TUN/MASQUE 建立、WebSocket 握手。
- Linux/Android example 删除 TUN IP 入参；客户 README 同步新流程。本地 HTTP 接口文档和用户友好版设计文档已增加完整请求/响应和选择规则，但按交付排除规则继续被 Git 忽略、不进入提交或远端；原始《SDK设计文档》保持不动。
- Python Wheel 版本从 `0.9.0` 升级为 `0.10.0`。

### 验证内容

- Python `compileall` 和全量测试通过（`65 passed`）；新增精确 GET URL/空 body、正常 PDU IPv4 提取和 NAS/PDU 非就绪拒绝覆盖。
- Android JVM 单元测试共 `23 tests / 0 skipped / 0 failures / 0 errors`；新增精确 GET 报文、PDU IPv4 提取和非活动 Session 拒绝覆盖。
- `agent_connect_sdk-0.10.0-py3-none-any.whl` 通过 `twine check`，在独立虚拟环境安装后显示版本 `0.10.0`，`agent-sdk-self-check` 输出 `FULL FLOW DEMO PASSED`，并确认初始化结果为 `agent_tun_cidr=8.8.8.7/32`。Wheel SHA-256 为 `ddd0d20676758bcf10cd841a034b5c9abaa77a707d27be0b6e7594ccb4f392c1`。
- Android Release AAR 和 example Debug APK 构建成功，SHA-256 分别为 `85127cc82c1d61d04e1d1ae63ea750b0c51a4d9dd2591bbc28810c33009b96b3` 和 `e645ed2e6eea207446f04dc2601df3d5949dff93003be75f62405b09f14b8f8c`；Wheel、AAR 和 APK 均无私钥资源。
- HTTP 接口文档的 17 个 JSON 示例和用户友好版设计文档的 2 个 JSON 示例均通过标准解析；原始《SDK设计文档》SHA-256 仍为 `d2509f323338d0cdb948ceff36e32c3ae71d59c646916b687168b4c2e862947b`。

## 2026-08-21 — SDK 初始化收敛为 WebSocket 下行注册

### 修改原因

- Agent Application 与基带 SDK 的下行注册已统一为 `GET /v1/acn/downlink-websocket` WebSocket Upgrade，SDK 初始化不应再额外调用 `/health` 或 `/sdk/v1/endpoints`。
- AgentRuntime 仅负责原样透传网内消息；SDK 不应在初始化时向它同步本机物理 IP、监听端口或 Agent TUN 地址。
- WebSocket 握手没有 UE IP 响应体，因此本机 TUN 地址必须由部署配置显式传入，并与 MASQUE Proxy/UERANSIM 的设备映射一致。

### 修改方式

- Python `AgentSdk.init()` 新增必填关键字参数 `agent_tun_cidr`，Android `AgentSdk.initialize()` 新增必填参数 `agentTunCidr`；两端均在创建 TUN 前校验 IP 字面量、地址簇和前缀范围，该值只在本地使用。
- Python/Android 删除 Runtime Transport 的健康检查、端点注册方法及 `EndpointRegistration` 模型；初始化完成 TUN、A2A Server 和 MASQUE 后，只向同一 AgentRuntime IP/端口发起固定 WebSocket Upgrade，握手成功才返回。
- 保留现有 WebSocket 报文：下行请求使用 `kind + request_id + message_type + transaction_id + payload`，响应仅使用 `kind=response + request_id + payload.result`，并支持多请求并发和乱序返回。
- Linux 真实示例新增 `--agent-tun-cidr`，Android example 新增 `agent_tun_cidr` Intent extra；离线全流程示例、客户 README 同步新的边界。本地 HTTP 接口文档和用户友好版设计文档也已更新，但按交付排除规则继续被 Git 忽略、不进入提交或远端；原始《SDK设计文档》保持不动。
- Python Wheel 版本从 `0.8.0` 升级为 `0.9.0`。

### 验证内容

- Python `compileall` 和全量测试通过（`59 passed`）；覆盖本地 TUN CIDR 实际传入、非法前缀拒绝、初始化期间无 Runtime REST 请求以及下行 handler 已注册。
- Android JVM 单元测试共 `22 tests / 0 skipped / 0 failures / 0 errors`；覆盖本地 TUN CIDR、非法 CIDR 拒绝、无初始化 REST 请求，以及 WebSocket 精确路径/请求响应格式/并发乱序响应。
- `agent_connect_sdk-0.9.0-py3-none-any.whl` 通过 `twine check`，在独立虚拟环境安装后显示版本 `0.9.0`，`agent-sdk-self-check` 输出 `FULL FLOW DEMO PASSED`。Wheel SHA-256 为 `468985d46d5e12a1e34ce1a3c7f2d1044b7e7623fd0ec8ae55a43682ead8dcaa`。
- Android Release AAR 和 example Debug APK 构建成功，SHA-256 分别为 `56744363b968dd0d2899626d46d544eda9a060c1108e3001ed1b736ba51378ea` 和 `9e68e6cecf301109907c1777b99cde01130e90e8335ab2ec8d73c608069ea61a`。Wheel、AAR 和 APK 均无私钥资源；APK `classes2.dex` 仅包含 SDK 解析器的 PEM 私钥边界字面量，不包含私钥材料。
- HTTP 接口文档的 16 个 JSON 示例和用户友好版设计文档的 1 个 JSON 示例均通过标准解析；原始《SDK设计文档》SHA-256 仍为 `d2509f323338d0cdb948ceff36e32c3ae71d59c646916b687168b4c2e862947b`。

## 2026-08-20 — Android AgentCard 支持内测能力 VC 即时签发

### 修改原因

- 上一提交只在 Linux/Python `register_capabilities()` 增加了能力字符串即时签发，Android `registerCapabilities()` 仍强制要求调用方提供预签发 VC，两端北向能力不一致。
- Android 设备无法直接读取构建机 `~/lpx/cert/third-party`，同时三方测试私钥不能进入远端 Git 或 Agent SDK AAR，因此需要明确的测试密钥导入和应用私有存储机制。

### 修改方式

- Android `registerCapabilities()` 新增默认空的 `credentials`、`capabilities` 和可选 `agentName`，支持已有 VC、能力字符串或混合输入；本地身份与 `agentId` 匹配时自动使用缓存的 Agent 名称。
- 新增 Android 测试能力 VC 签发器，每个能力生成一张与 Python 相同的 `CapabilityCredential`；签发者、claims、有效期、七字段排序紧凑 ASCII JSON、P-256 ECDSA/SHA-256 和 DER Base64 规则保持一致。
- `AgentSdk.create()` 将测试签发器绑定到应用 `noBackupFilesDir/agent-sdk/test-capability-vc`；`importTestCapabilityIssuerPrivateKey()` 负责校验并保存 PKCS#8 P-256 PEM。私钥不进入 AAR，未导入时请求明确返回 `SIGNATURE_ERROR`。
- Android example 可从本地且被 Git 忽略的 `raw/test_third_party_private_key.pem` 导入测试密钥，并通过 `test_capabilities` Intent extra 发布能力；不传该参数时不读取密钥，也不改变原流程。
- Android 指南、根 README、Python 交叉说明、本地 HTTP 接口文档和用户友好版设计文档同步两端行为；原始《SDK设计文档》保持不动。

### 验证内容

- Android JVM 单元测试共 `23 tests / 0 skipped / 0 failures / 0 errors`；新增 5 个签发器测试和 1 个 AgentCard 测试，覆盖多能力逐项签发、混合已有 VC、自动 Agent 名称、导入缺失、重复能力、非 ASCII 规范化、签名验签和能力篡改拒绝。
- JVM 测试直接读取真实 `/root/lpx/cert/third-party/private-key.pem` 签发 `robot-control` VC，并使用对应 `public-key.pem` 验签；本地执行未跳过且通过。
- Android Release AAR 和 example Debug APK 构建成功；SHA-256 分别为 `d40f1c67488be6bb7d47ed3e55f39d8a96754bf595cc8251dbbd2ee608d65502` 和 `d53378a80e72dff17d0dd7fbc95a1cdbd8b0943dc188078d53d9858a64c6d43e`。
- 对 AAR、APK 及其嵌套归档逐项扫描，均未包含真实三方私钥 PEM 或其 Base64 密钥体；测试资源路径已加入 `.gitignore`。
- 本地 HTTP 接口文档的 19 个 JSON 示例和用户友好版设计文档的 3 个 JSON 示例均通过标准解析；原始《SDK设计文档》SHA-256 仍为 `d2509f323338d0cdb948ceff36e32c3ae71d59c646916b687168b4c2e862947b`。

## 2026-08-20 — Linux AgentCard 支持内测能力 VC 即时签发

### 修改原因

- 原 `register_capabilities()` 只能接收已经签发好的 VC 并原样写入 `vc_list`，封闭测试用例无法直接用能力字符串构造 AgentCard。
- `/root/lpx/cert/third-party` 已提供三方能力认证组织的 P-256 测试私钥和公钥，需要复用现有 IDM 签名规则生成可验签的能力 VC，同时不能要求 AgentRuntime 或服务端增加新的 HTTP 字段。

### 修改方式

- Python `register_capabilities()` 保留 `credentials` 正式入口，并新增可选的 `capabilities`、`agent_name` 和 `test_vc_private_key_path`；两类输入可以单独使用或混合使用。
- 每个能力字符串生成一张 `VerifiableCredential + CapabilityCredential`，包含 `agent_id`、`agent_name`、`capability` 和 `authorization_mode`；签发者固定为测试三方 DID。
- 测试 VC 按 IDM 现有规则只签 `context/id/type/issuer/valid_from/valid_until/claims` 七个字段，使用排序紧凑 ASCII JSON、P-256 ECDSA/SHA-256、ASN.1 DER 标准 Base64。默认私钥路径为 `~/lpx/cert/third-party/private-key.pem`，可显式覆盖。
- 生成后的 VC 追加到现有 `vc_list`，HTTP URL 和请求体外层字段不变，AgentRuntime 仍纯透传；测试私钥只从 Wheel 外部读取，不复制到源码或交付物。Android 和正式生产入口继续接收预签发 VC。
- `linux_agent.py` 新增可重复的 `--test-capability` 和外部测试私钥路径参数；Python 客户指南、根 README、本地 HTTP 接口文档和用户友好版设计文档同步说明。原始《SDK设计文档》保持不动。
- Python Wheel 版本从 `0.7.0` 升级为 `0.8.0`。

### 验证内容

- Python `compileall` 和全量测试通过（`62 passed`）；新增测试覆盖多能力逐项签发、混合已有 VC 与即时 VC、自动读取本地 Agent 名称、参数校验、签名验签及能力篡改拒绝。
- 使用真实 `/root/lpx/cert/third-party/private-key.pem` 生成 `robot-control` VC，并用对应 `public-key.pem` 成功验签；`certificate_signing.py demo` 的三方能力 IDM VC 验签同时通过。
- `agent_connect_sdk-0.8.0-py3-none-any.whl` 通过 `twine check`，独立虚拟环境安装显示版本 `0.8.0`，`agent-sdk-self-check` 输出 `FULL FLOW DEMO PASSED`。
- Wheel 逐文件扫描确认不包含三方私钥或任何 PEM 私钥边界；交付 Wheel SHA-256 为 `49025fab35b4ac18507b311c050adb58790bb3a6c58718e31ea7b4620c62e32d`。
- 本地 HTTP 接口文档的 19 个 JSON 示例和用户友好版设计文档的 3 个 JSON 示例均通过标准解析；原始《SDK设计文档》SHA-256 仍为 `d2509f323338d0cdb948ceff36e32c3ae71d59c646916b687168b4c2e862947b`。

## 2026-08-20 — 将设备签名和核心网验签收敛为 SDK 内建能力

### 修改原因

- Python 真实示例要求用户传入 `proof_verifier`、`control_request_authenticator`、`message_signer` 和 `message_signature_verifier`，其中 Demo 实现只检查字段存在或返回假签名，既不安全也不符合“SDK 封装消息防篡改”的北向目标。
- 身份申请还要求用户传公钥，但设备公私钥应由 SDK 第一次启动时生成并稳定复用；核心网下发群组配置应直接使用固定核心网公钥验签，不应要求客户自行接安全插件。
- Android 原实现同样依赖 Demo 安全对象，而且控制面请求尚未统一补真实时间戳和签名。

### 修改方式

- Python 首次 `init()` 在 `$XDG_STATE_HOME/agent-sdk/security` 或 `~/.local/state/agent-sdk/security` 生成 P-256 设备公私钥，目录和密钥文件权限固定为 `0700/0600`；后续启动复用并校验公私钥匹配。Android 在不可导出的 Android Keystore 条目 `agent-sdk-device-signing-v1` 中生成并复用 P-256 私钥。
- Python Wheel 和 Android AAR 预置 `/root/lpx/cert/core-network/public-key.pem` 的同一 P-256 公钥；没有复制核心网私钥。`acf_group_config.proof` 先用固定核心网公钥验签，通过后才允许缓存成员和提交动态路由。
- `apply_identity/applyIdentity` 删除北向公钥参数，SDK 自动将本机设备公钥编码为 Base64 DER SubjectPublicKeyInfo 并填入原始 HTTP `public_key` 字段。Python 真实示例删除 `--identity-public-key` 和四个 Demo 安全对象；Android 用户入口收敛为 `AgentSdk.create(service)`。
- 控制面旧式 `signature` 使用 P-256 ECDSA + SHA-256、ASN.1 DER + 标准 Base64；`proof.jws` 和 A2A 消息使用 ES256、RFC 7797 `b64=false` 分离 JWS。签名原文采用字段名排序、无多余空白的 UTF-8 JSON，并覆盖业务字段、时间戳和 proof 元数据。
- A2A 入站继续只使用已验签群组快照中的发送方 P-256 `did:key` 验签；出站自动使用本机设备私钥签名。安全 SPI 从 Android 公共入口移为内部实现，Python 顶层包不再导出 `ControlRequestAuthenticator`，测试覆盖使用下划线内部注入点。
- Android 控制面同步接入内建认证，并在端侧直接解析 AgentRuntime 原样透传的 `vc0`、`vc1`、`result[].agent_card` 和嵌套 `target_agents + group_config`，不要求 Runtime 做字段改名或展平。
- Python Wheel 版本从 `0.6.0` 升级为 `0.7.0`。根 README、Python/Android 客户指南及本地 HTTP 接口文档、用户友好版设计文档同步新的密钥生命周期和调用方式；原始《SDK设计文档》保持不动。

### 验证内容

- Python 新增设备密钥首次生成/稳定复用/权限、控制面真实签名、A2A `did:key` 验签、核心网固定公钥验签和篡改拒绝测试；`compileall` 与全量测试通过（`58 passed`）。
- Android 新增软件 P-256 后端单元测试，覆盖控制面签名、A2A 分离 JWS、核心网固定公钥验签和篡改拒绝；JVM 单元测试 `17 tests / 0 failures / 0 errors`，Release AAR 和 example Debug APK 构建成功。
- `agent_connect_sdk-0.7.0-py3-none-any.whl` 通过 `twine check`，独立安装显示版本 `0.7.0`，`agent-sdk-self-check` 输出 `FULL FLOW DEMO PASSED`，首次初始化实际生成两个权限为 `0600` 的设备签名密钥文件。
- Wheel 和 AAR 私钥材料扫描均为 clean；Wheel/AAR 包含相同的 `core-network-public-key.pem`，其源文件 SHA-256 均为 `c1f6cff8bd6d29225ffe7b7c7c3877471355b5d13db7882132a6f33456312d38`。
- 交付物 SHA-256：Wheel `6a20473d8b9132b85a0ec51543718777e46fb696f22fd350ede067b6a11d880b`，Release AAR `9fc517c7e786e3d426e9eeed65e234102f86c670c1978bfe67034c7f67d93b90`，example APK `92b947a6ea755b97c2e794cae61bc629da562d8dc17a5d2023d15fab507ecb45`。
- HTTP 接口文档的 19 个 JSON 示例和用户友好版设计文档的 3 个 JSON 示例全部通过标准解析；原始《SDK设计文档》SHA-256 仍为 `d2509f323338d0cdb948ceff36e32c3ae71d59c646916b687168b4c2e862947b`。

## 2026-08-20 — 核心网主动下行改为同端口 WebSocket

### 修改原因

- 核心网主动下行不再由 AgentRuntime 反向 POST 端侧 `/agent/group-invitation` 和 `/agent/group-moq-info`，而是要求 Agent Application 主动连接基带 SDK 的固定 WebSocket。
- WebSocket 必须复用主动上行 REST 服务的 AgentRuntime IP 和端口；`request_id` 允许多个 NAS 请求并发处理和乱序响应，`transaction_id` 必须随请求保留给基带侧构造原始 NAS 响应。

### 修改方式

- Python 与 Android Runtime Transport 新增内部 `start_downlink/startDownlink`：固定 GET `/v1/acn/downlink-websocket` 并完成 WebSocket Upgrade，不增加北向初始化参数；`init/initialize` 只有在握手成功后才返回。
- 下行帧严格解析 `kind=request`、非空 `request_id/message_type`、非负整数 `transaction_id` 和对象 `payload`。每个请求使用独立 task/coroutine 处理，响应固定为 `kind=response + request_id + payload.result`，允许乱序返回。
- `ACN_AGENT_GROUPING_INVITATION` 映射为北向 `GROUP_INVITATION`；`acf_group_config` payload 继续走既有验签、成员缓存和动态路由提交逻辑。未知消息映射为 `UNKNOWN`，无 listener 或处理异常时返回 `REJECT`。
- Android 群组配置语义与 Python 对齐：有效快照提交成功即返回 `ACK`，用户 listener 只作提交后通知，其 `REJECT` 或异常不回滚缓存与路由；群组邀请仍由 listener 返回 `ACCEPT/REJECT`。
- 真实本地 HTTP Server 删除两个 Runtime 下行 POST 路由，仅保留 Agent TUN 上的 `/A2A/message`；WebSocket 和所有主动上行 REST 请求共享 `agent_runtime_ip:agent_runtime_port`。
- Python 支持 WebSocket 断开后的有界指数退避重连；两端关闭 SDK 时主动关闭连接并清理并发处理任务。Python wheel 因线协议非兼容变更从 `0.5.0` 升级为 `0.6.0`。
- 根 README、Python/Android 客户说明以及本地《Agent SDK HTTP 接口文档》《SDK 设计文档-用户友好版》同步新通道；两份本地文档继续按规则忽略、不推送，原始《SDK设计文档》保持不动。

### 验证内容

- Python 真实 WebSocket 测试验证 Upgrade 路径、并发请求、乱序响应、字段透传、非法关联请求返回 `REJECT`、旧 POST 路由不可用和邀请消息北向映射。
- Android 使用真实 OkHttp WebSocket/MockWebServer 验证相同 Runtime 端口、固定路径以及并发乱序响应；Fake Runtime 验证邀请和群组配置均通过 WebSocket handler 进入 SDK。
- Python `compileall` 与全量测试通过（`53 passed`）；Android JVM 单元测试 `14 tests / 0 failures / 0 errors`，Release AAR 和 example Debug APK 构建成功。
- `agent_connect_sdk-0.6.0-py3-none-any.whl` 通过 `twine check`，独立安装后版本为 `0.6.0`，`agent-sdk-self-check` 输出 `FULL FLOW DEMO PASSED`。
- 本地接口文档 19 个 JSON 示例、用户友好版设计文档 3 个 JSON 示例全部通过标准解析。交付物 SHA-256：Wheel `19c3f6aba2ff354e0d2c1cf255bd284e69dfc9e517f008e0989d2b49ca02eabc`，Release AAR `b7c52d2034a127ea70b591e6545de78a811c489fcf0907306f34c856e9d78a9b`，example APK `dd246ffcbb6265e9b55daa9218c3af12f2add1ceed2422326be823bbed67d76a`。
- 原始《SDK设计文档》SHA-256 仍为 `d2509f323338d0cdb948ceff36e32c3ae71d59c646916b687168b4c2e862947b`，确认未修改。

## 2026-08-20 — 内测网络关闭 MASQUE 服务端证书校验并统一 Runtime HTTP

### 修改原因

- 当前部署仅用于封闭内部联调，需要允许 Linux 和 Android SDK 连接自签名、名称不匹配或未纳入 SDK 信任库的 MASQUE 服务端证书。
- 除 MASQUE CONNECT-IP 必须使用 HTTPS/HTTP/3 外，SDK 与 AgentRuntime 的健康检查、端点注册和全部控制接口均要求改用普通 HTTP。

### 修改方式

- Python MASQUE 客户端改为 `ssl.CERT_NONE`，Android Native Core 改为 `InsecureSkipVerify=true`；两端仍限定 TLS 1.3、继续携带 Authorization，并继续生成和出示端侧客户端证书。
- Linux 每次连接记录结构化 `masque_server_certificate_verification_disabled` 警告，Android Native Core 输出同义警告，明确标记 `internal-test-only` 安全配置。
- Python `HttpRuntimeTransport` 与 Android `OkHttpRuntimeTransport` 的基础 URL 从 `https://` 改为 `http://`；Android AAR Manifest 启用 cleartext traffic。AgentRuntime 主动回调和 Agent 间消息原本已经使用 HTTP，不改变消息体和 URL 路径。
- 根 README、Linux 客户指南、Android 说明和 MASQUE TLS 部署说明同步当前内测安全边界；本地《Agent SDK HTTP 接口文档》和用户友好版设计文档同步 HTTP 协议与证书策略，但按仓库规则继续忽略、不推送。服务端程序及配置未修改，原始《SDK设计文档》保持不动。

### 验证内容

- Python 真实 HTTP/3 CONNECT-IP 测试不再向客户端提供 CA 或匹配的 Server Name，以覆盖不受信任且名称不匹配的证书仍能完成双向数据报转发，并断言安全警告已写入本地日志。
- Android Native Core 真实 CONNECT-IP 测试使用同样不受信任且名称不匹配的服务端证书，同时继续验证客户端证书、Authorization、ADDRESS_ASSIGN、路由 Capsule 和双向 IP 包。
- Python `compileall` 和全量测试通过（`50 passed`），其中新增断言确认 Runtime 基础地址固定为 `http://`；Go Native Core 全量测试通过；Android JVM 单元测试 `11 tests / 0 failures / 0 errors`，Release AAR 与 example Debug APK 构建成功。
- 重建 Wheel 后 `twine check` 通过，独立安装运行 `agent-sdk-self-check` 输出 `FULL FLOW DEMO PASSED`。交付物 SHA-256：Wheel `2d40e3a39a3abf5b243df94f973adb2b6b674bbdf2c9c70f8c3bab15ea089528`，ARM64 Native Core `b9696c524fb14d9ca2899129ab348359e5534bd88f475a30bcfd90dd303942c1`，Release AAR `732fab5454e8354347da3581fc65be389db9072a4acfb185cf1d280769b8dccc`。
- 原始《SDK设计文档》SHA-256 仍为 `d2509f323338d0cdb948ceff36e32c3ae71d59c646916b687168b4c2e862947b`，确认未修改。

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
