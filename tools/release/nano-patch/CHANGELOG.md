# 20261013 基线 → @RELEASE_DATE@ 累计差异

## 2026-11-12 源码整理与后续重建

当前 dog `orange_dog/a2c35bc`、dog-ctrl `orange_dog/866bdd2` 已包含此前 Studio/Warning 增量，
狗实机与本地分别核对 26/22 个跟踪文件一致。新增 dog Patrol 身份准备配套手机 1.2.7、眼镜 0.2.63。
构建器默认只收集干净 HEAD 的普通文件；未跟踪文件不入包，现场覆盖必须显式注明来源。
SDK wheel 模块须与所选 SDK HEAD 一致，最终归档须绑定验收前输入快照。

以下表格与现场差异均是**历史 20261111 包**的来源记录，不能用来声称当前源码仍未提交，
也不能当作最新 a2c35bc/866bdd2 重建包的验证结果。重建包以自身 manifest、overlay diff 和新 validation 为准。
历史 live-audit.json、previous-patch-to-live.patch 由当时人工取证产生，构建脚本不自动生成这些额外证据。
Studio 属于 Free6GC 主仓库，Sandbox 不提供 Studio 检查点。

## 历史发布差异

基线外层包 SHA256：`44de185939b608f5bde7fe40c52cd4cc91bc6e724430841335ad40f93221b0e4`。
对照的是原包内部 `payload.tar.gz`、SDK wheel 和 manifest，不仅依据 Git 日期。

| 组件 | 旧包 | 本次目标 | 累计内容 |
| --- | --- | --- | --- |
| dog | `0d6cb813` | `93ed1e7` + 当前工作区/现役 Studio 增量 | 视频原生带宽自适应、独立 warn、0.8 米巡检、Studio 检查点 |
| dog-ctrl | `38bf6dda` | `b0bc3cb` + 当前工作区 | 航向保持、StopMove/BalanceStand 收尾、Warning 播音、0.8 米巡检、Content＋Warning 并发与互斥、新设备配置保护 |
| Python SDK | `bed37bce` / 0.17.11 | `cd53f65` 的 python 源码（Python 与 f0c261d 相同） / 0.17.11+orange.20261110 | 关闭固定编码码率和传输限速，允许 bitrate_kbps=0 自适应 |
| nav | `dc35f62` | 同版本 | 无变更，不替换 ROS/nav |
| 启动脚本 | 原包版本 | 保留 | 与现役启动脚本哈希一致 |
| 语音 | 无独立环境/素材 | Python 3.9.5 + aiortc 1.9.0 + aioice 0.10.2 | 全离线依赖、正式音频与按设备上传绑定 |

## 旧包之后的已提交历史

Dog：`4406bfc` 视频带宽自适应和巡逻轮次验证、`e89a80e` warn 路由、`93ed1e7` Studio 检查点与 0.8 米入口。

Dog-ctrl：`f687bbe` 直行航向与动作收尾、`d235ad8` Warning 音频接口、`b0bc3cb` 0.8 米入口。

SDK Python：`230a1d7` 的三个运行模块 `rtc_policy.py`、`sdk.py`、`webrtc.py` 改动。后续 Android/文档提交不代表 Nano 运行模块也改变。

## 现役文件与仓库不同的处理

- 现役 `dog/orange_dog.py` 额外含 Studio 自动跟随（`_studio_control`、`_studio_state`、`_follow_studio_patrol`、`_studio_task`）。本包明确作为现场增量包含，来源哈希写入 manifest，而非假称已提交到 Git。本次导出的运行文件与本地工作区逐字节一致，但仍未提交；与仓库 HEAD 的差异存于 `sources/live-dog-overlay.patch`。Studio 默认跟随核心网主机，显式空 DOG_STUDIO_URL 关闭。
- 现役 Python SDK 元数据是 0.17.10，但运行模块与当前源码一致，只有 `__init__.py` 版本标记不同。本包从当前源码构建带本地版本后缀的新 wheel；不用版本号推断运行内容。
- 现役 dog_init.sh 与旧包的差异是现场默认密码，本包不复制该差异，不携带设备密码。
- 现役 root 配置、身份、相机、MAC、核心网地址、调试日志、venv 和备份不作为覆盖素材。

## 包外配套差异

旧发布包还包含 Android APK 源码快照。现在眼镜/手机已有 Warning、巡逻重置和 Studio 联动更新；服务器 Sandbox 也有独立语音和视频 viewer 更新。这些运行在眼镜/手机/服务器，不装进 Nano 补丁。参照 README 的兼容源码版本另行交付。

## 行为变化

`/warning` 现在是实际运动接口（Content＋大音量播报），不再只是播放。
声源为 Warning. Warning. + 0.2 秒间隔 + 2 秒 900/1400 Hz 报警，总长约 5.81 秒。新狗上传后取得独立 ID。

保留的已知限制：语音 15 秒外层超时、8 秒播放等待、前置 WebRTC/查询可能偶发变慢；不自动重试物理动作。

## 20261111 现役核对与新增内容

已读取现役进程命令行、运行文件和限定配置项，Agent 运行文件 SHA256 为
`e9ee884a2d5a48417884b54edf5046e8f38aa79aab0d5607ccf05ee82b9900bc`。
它在 20261110 下午更新，早于本次现场检查，但晚于上一补丁快照。

1. `_studio_base_url()` 默认使用实际 runtime_ip 的 7865 端口，支持 `auto`、显式 URL、空值关闭及 IPv6。
2. Agent Warning 与狗端调用并行触发检查点 3、4，绑定原始 run_id；场景重置后丢弃旧信号。
3. 检查点等待从 60 秒增为 600 秒，临时连接错误重试；设备抛出异常则取消待处理的播放任务。
4. 同步更新 Agent 文档及 Studio 回归测试。语音音频、动作控制与 Python SDK 运行内容沿用上一补丁；SDK wheel 继续使用 `0.17.11+orange.20261110`，避免只改日期就重复构建相同运行代码。

现役控制端没有上一补丁专为新机增加的音频绑定保护，本包继续保留保护，不能直接用现役 main.py/warning_audio.py 覆盖。
现役 `DOG_STUDIO_URL=auto` 已在进程中生效；本包不复制其核心网 IP、音频 UUID 或整份 core.env。
手机控制器新增跟随配置的核心网地址重置 Studio（`cd53f65`），属于 APK 配套更新，不进入狗端 Python wheel。

具体逐文件复核见 `live-audit.json`，相对上一补丁的 Agent 差异见 `sources/previous-patch-to-live.patch`。
本包没有修正其他既有 Agent 行为；600 秒等待是每个检查点的上限，两个 Warning 检查点仍可延长 Agent 指令处理等待时间，不改变狗端 HTTP 播音超时。
