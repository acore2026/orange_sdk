# 代码组织与功能归属

本文是新增代码、脚本、测试和文档的放置依据。仓库最外层按应用、SDK、内部库和工程支持划分，各模块内部再按业务职责组织。

本次迁移只调整目录和依赖路径。已有 Python 导入名、Kotlin/Go 包名、应用 ID、协议、版本和业务流程保持原样；历史命名不等于当前产品定位。

## 1. 实际目录

```text
orange_sdk/
├── apps/
│   ├── dog-patrol/             # 正式机器狗应用
│   ├── rayneo-patrol/          # 正式眼镜应用
│   └── phone-controller/       # 正式手机应用
├── sdks/
│   ├── python/                 # Python SDK、wheel 配置和单元测试
│   └── android/                # Android SDK、AAR 配置和单元测试
│       └── native/masque_core/ # Native MASQUE、JNI、第三方依赖
├── libs/
│   └── remote-control/         # 应用内部复用的遥控和管理通信库
├── examples/
│   ├── python/                 # SDK 接入、A/B 示例、媒体素材和 ARM64 镜像
│   └── android/                # 通用 Android SDK 示例界面
├── mock-service/
│   └── mock-sandbox/           # 模拟算力服务及其测试、镜像、启动脚本
├── integration-tests/
│   ├── contracts/              # 跨组件协议验证
│   └── scenarios/              # 跨设备链路与流程验证
├── tools/
│   ├── device/                 # 设备连接和持久化地址配置
│   ├── diagnostics/            # 视频性能和进程故障诊断
│   └── release/                # 交付包、离线安装、累积补丁与回滚
├── docs/
│   ├── architecture/          # 架构与目录规范
│   ├── protocols/             # 跨组件协议说明
│   ├── deployment/            # 设备安装说明
│   └── assets/                # 截图及已有历史日志
├── settings.gradle.kts         # Android/JVM 多模块构建入口
├── build.gradle.kts
├── gradle.properties
├── gradle/、gradlew、gradlew.bat
├── pytest.ini                 # 本地可执行 Python 测试入口
├── README.md                  # 项目导航、开发规范和 SDK 使用入口
└── CHANGELOG.md
```

有实际内容时才建立子目录；例如当前没有单独的 `tools/build/`、Studio 服务或时间同步脚本，因而不创建对应空目录。

## 2. 功能划分与边界

| 模块 | 现有功能 | 后续代码归属原则 |
| --- | --- | --- |
| `apps/dog-patrol/` | 数字身份与能力准备、接受建组、巡逻任务、动作执行、视频上传、事件回执 | 机器狗特有的任务、执行器和设备业务放这里 |
| `apps/rayneo-patrol/` | 巡检编排、发现与派遣确认、语音交互、A2A 控制、视频接收和双目 HUD | 眼镜业务状态、交互和显示放这里 |
| `apps/phone-controller/` | 遥控眼镜、状态查询、设备配置、身份管理、Studio 重置调用 | 手机交互、操作编排及手机专用的外部服务调用放这里 |
| `sdks/python/`、`sdks/android/` | 身份、能力、发现、群组、A2A、TUN/MASQUE、算力会话、媒体与控制接口 | 可供其他应用复用的通用 Agent 接入能力放这里 |
| `libs/remote-control/` | 遥控命令集合、TCP 客户端/服务端、地址校验、机器狗管理 HTTP 客户端 | 仓库内部多个应用共同使用的通信能力放这里 |
| `examples/` | SDK 全流程自检包装、Linux 接入、交互示例、A/B 示例和通用 Android 界面 | 以说明 SDK 用法和联调为目的的代码放这里 |
| `mock-service/` | 当前为 Mock Sandbox：WebRTC、识别目标、文本/音频控制接口 | 后续模拟服务及周边配套按服务建立子目录，源码、启动、镜像和服务自身测试放在一起 |
| `integration-tests/` | A2A 动作协议验证、双实例 MASQUE 链路脚本及其测试 | 验证跨应用、SDK、设备或服务协作的代码放这里 |
| `tools/` | 设备维护、诊断、发布和交付 | 应用运行之外的操作者工具和跨模块交付流程放这里 |

### 眼镜与手机

眼镜是接入 Agent 网络的巡检主端，拥有 Agent 身份，通过 SDK 发现、建组、建立算力会话并下发 A2A 指令。手机提供局域网遥控和管理入口，不注册第二个核心网身份；运动指令仍经眼镜发送。手机也直接访问机器狗管理 HTTP 接口和 Studio。

### 保留的历史耦合

- `AgentTestRunner.kt`、`TestConfig.kt` 等类名和 `com.rayneo.agent.example` 包名保持原样，现有编排源码位于 `apps/rayneo-patrol/src/main/`。
- 通用 Android 界面入口已移到 `examples/android/src/main/`，继续使用 `:example-app` 的 `generic` flavor，通过 Gradle source set 加入编译；`rayneo` flavor 不编译通用界面入口。两者继续复用原有编排、资源和测试。这是保留已有实现的安排，`examples/android/` 不是独立 Gradle 项目。
- 原 `demo-remote` 对象仍包含 `resetStudio()`，现阶段保留实现和调用位置，不为迁移拆分接口。未来新增手机专用 Studio 调用放在手机应用；未来引入 Studio 服务源码时放在 `mock-service/studio/`。
- 本仓库只包含现有 `dog_patrol_agent.py`。发布工具引用的外部 `compute_backend/dog`、`dog-ctrl`、`nav` 不在本仓库，不能把此目录迁移当作这些外部服务已合入。
- SDK 与 Native 模块的第三方源码、预置证书及二进制保持原内容。部署设备路径、容器内 `/opt/agent-sdk` 和 `/opt/mock-video-server`、镜像名与服务名属于运行契约，不随源码目录改名。

## 3. 新增文件放哪里

| 新增内容 | 放置位置 |
| --- | --- |
| 机器狗巡逻策略、设备动作适配 | `apps/dog-patrol/` |
| 眼镜 HUD、语音和巡检状态 | `apps/rayneo-patrol/` |
| 手机操作页面和设备管理流程 | `apps/phone-controller/` |
| 通用身份、群组、媒体或连接 API | 对应平台的 `sdks/` |
| 手机和眼镜共用的遥控命令或传输组件 | `libs/remote-control/` |
| SDK 最小调用示例 | `examples/<platform>/` |
| 配套服务、模拟接口及服务部署脚本 | `mock-service/<service>/` |
| 模块单元测试、服务接口测试 | 被测模块内部的 `tests/` 或 `src/test/` |
| 多组件协议兼容测试 | `integration-tests/contracts/` |
| 跨设备、完整巡检或视频流程验证 | `integration-tests/scenarios/` |
| 设备连接、校时或地址配置工具 | `tools/device/`；当前未实现校时脚本 |
| 日志采集、进程诊断、编码基准 | `tools/diagnostics/` |
| 整包交付、升级、离线部署和回滚 | `tools/release/` |
| 模块用法与构建说明 | 模块自身 `README.md` |
| 跨模块架构、协议、设备部署文档 | `docs/` 对应子目录 |

`libs/` 面向内部复用，`sdks/` 面向外部应用接入。不要把设备业务放入 SDK，也不要把正式应用放回 `examples/`。集成测试需要公共数据时，再建立 `integration-tests/fixtures/`。

## 4. 构建、运行与测试入口

以下命令均从仓库根目录执行。真实联网、设备部署和运动操作仍需各模块文档所述环境。

### Python

```bash
python3 -m pip install -e './sdks/python[test]'
python3 -m pip install -r mock-service/mock-sandbox/requirements.txt
python3 -m pip install 'build>=1.2,<2'
python3 -m build --wheel sdks/python
agent-sdk-self-check
python3 examples/python/full_flow_demo.py
python3 apps/dog-patrol/dog_patrol_agent.py --dry-run
python3 -m pytest -q
```

SDK 本身支持 Python 3.10+；现有完整测试中有两项使用 Python 3.11+ 的 `asyncio.timeout`，运行完整测试建议使用 Python 3.11+。未修改这些测试的业务断言。

根 `pytest.ini` 收集 SDK、示例、狗应用、集成、Mock 和工具的本地测试。`test_studio_overlay.py` 需要外部 dog 源码和 `DOG_SOURCE`，独立运行：

```bash
DOG_SOURCE=/path/to/dog python3 -m unittest discover \
  -s tools/release/nano-patch/tests -p test_studio_overlay.py -v
```

### Android / JVM

为保留现有 Gradle 任务和 APK/AAR 名称，项目 ID 与目录映射如下：

| Gradle 项目 ID | 实际目录 | 主要产物 |
| --- | --- | --- |
| `:agent-sdk` | `sdks/android/` | `build/outputs/aar/agent-sdk-release.aar` |
| `:example-app` | `apps/rayneo-patrol/` | `rayneo` 正式眼镜 APK，`generic` SDK 示例 APK |
| `:demo-remote` | `libs/remote-control/` | 内部 JVM 库 |
| `:phone-controller` | `apps/phone-controller/` | 手机 APK |

```bash
ANDROID_HOME=/path/to/android-sdk ./gradlew \
  :demo-remote:test :agent-sdk:testDebugUnitTest \
  :example-app:testRayneoDebugUnitTest \
  :example-app:assembleRayneoDebug :example-app:assembleGenericDebug \
  :phone-controller:assembleDebug
```

APK 输出位于各项目实际目录的 `build/outputs/apk/`。本机 SDK 设置使用仓库根目录的 `local.properties`，或设置 `ANDROID_HOME`。

### 脚本位置

| 用途 | 入口 |
| --- | --- |
| Windows 安装眼镜 APK | `apps/rayneo-patrol/scripts/install-rayneo-windows.ps1` |
| 构建 Native MASQUE | `sdks/android/native/masque_core/build-android.sh` |
| ARM64 A/B 镜像与启动 | `examples/python/docker/arm64/` |
| 真实双实例 MASQUE 验证 | `integration-tests/scenarios/masque_two_instance_test.py` |
| Mock 服务、镜像和烟测 | `mock-service/mock-sandbox/` |
| 眼镜/机器狗核心网地址设置 | `tools/device/set_core_address.py` |
| WSL/Windows TCP 中转 | `tools/device/dog-access/` |
| 视频基准、进程诊断 | `tools/diagnostics/` |
| 源码交付、Nano 离线部署和补丁 | `tools/release/` |

各模块脚本随模块存放。ARM64 镜像构建上下文改为仓库根目录，脚本负责定位 SDK 与示例，镜像内既有路径保持不变。Nano 离线包中的 `config/watch.py` 继续作为自包含交付副本，通用源文件位于 `tools/diagnostics/watch.py`。

## 5. 旧路径迁移表

| 旧位置 | 新位置 |
| --- | --- |
| `python/src/`、构建配置、SDK 测试 | `sdks/python/` |
| `python/examples/dog_patrol_agent.py` 及狗应用测试 | `apps/dog-patrol/` |
| 其他 `python/examples/` | `examples/python/`，双实例链路脚本除外 |
| `python/docker/` | `examples/python/docker/` |
| `python/examples/masque_two_instance_test.py` | `integration-tests/scenarios/` |
| `python/tests/test_runtime_voice_contract.py` | `integration-tests/contracts/` |
| `android/agent-sdk/`、`android/native/` | `sdks/android/`、`sdks/android/native/` |
| `android/example-app/` | `apps/rayneo-patrol/`，通用 `MainActivity.kt` 除外 |
| 通用 `MainActivity.kt` | `examples/android/src/main/` |
| `android/phone-controller/` | `apps/phone-controller/` |
| `android/demo-remote/` | `libs/remote-control/` |
| `android/` 中 Gradle 根配置及 Wrapper | 仓库根目录 |
| `mock-video-server/` | `mock-service/mock-sandbox/` |
| 核心网配置脚本、`tools/dog-access/` | `tools/device/` |
| 视频基准及通用 `watch.py` | `tools/diagnostics/` |
| 根目录协议、架构、安装资料和截图 | `docs/` 对应子目录 |

历史 CHANGELOG、历史安装证据中的旧路径代表当时状态，继续保留。新命令与新增代码以本文和模块 README 为准。

## 6. 本次目录迁移的验证

2026-11-16 在本地执行：

- 原有 279 个受版本控制文件全部保留；业务源码、资源、预置二进制和 Python wheel 内的 SDK 模块保持原字节内容。
- Android/JVM 191 项单元测试通过：通信库 9 项、Android SDK 96 项、RayNeo 与 generic 变体各 43 项（两变体复用同一套编排/接线测试）。
- 眼镜、通用 Android 示例、手机 Debug APK 和 Android SDK Release AAR 构建通过。
- Python 3.11 在相同依赖环境下，重构前后均为 279 项全部通过；Python 3.10 下两项既有测试因缺少 `asyncio.timeout` 失败，未修改既有实现或测试断言。
- Python wheel 构建、离线全流程自检、机器狗 dry-run、脚本语法及文档本地链接检查通过。

本次未执行真实设备运动、离线安装、补丁升级或完整 ARM64 Docker 镜像重建。需要外部 dog 源码的 Studio overlay 测试按上文单独运行。
