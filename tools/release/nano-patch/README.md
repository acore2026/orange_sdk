# Nano 累计离线补丁 @RELEASE_DATE@

目标：已安装 **orange-nano-offline-20261013-current** 的 Orin Nano，Ubuntu 20.04 / JetPack 5.1.1 / L4T R35.3.1。
这不是刷机镜像，也不能代替原始基础安装包。对比与累计变更见 `CHANGELOG.md`、`baseline-to-current.json`、`manifest.json`。

## 包含什么

- 狗 Agent 自适应视频、Warning 指令、0.8 米巡检入口、Studio 检查点、自动跟随与 Patrol 身份准备（具体以 manifest 的 dog 提交为准）。
- 控制端直行航向保持、正常动作收尾、单次 `/warning` 并发 Content＋正式大音量合并播报，以及新设备音频未配置保护。
- Python SDK `0.17.11+orange.20261110`，将旧包的固定视频码率改为原生带宽自适应。独立版本号避免与旧包的 `0.17.11` 混淆。
- Python 3.9 独立语音环境全部 wheels、正式音频、上传工具、可重建素材和文档。

保留每只狗的身份/私钥、核心网地址、网卡、相机、名称、运行配置、原厂驱动、ROS/nav 和 CycloneDDS。只有 `core.env` 的 `DOG_WARNING_DIR` / `DOG_WARNING_AUDIO_ID` 会更新。Studio 现在默认跟随 Agent 实际使用的核心网主机（HTTP 7865 端口），不会复制现役狗的 IP。
`DOG_STUDIO_URL=auto` 或未设置表示自动跟随；完整 URL 表示使用现场指定的 Studio；显式空值 `DOG_STUDIO_URL=` 表示关闭。已有设置原样保留，旧的固定 URL 不会自动迁移，需要随核心网切换时请手动改为 `auto`。

本包是狗端更新。眼镜/手机另行更新（当前配套 SDK `Release2.0` 的 `12bacd6`：手机 1.2.7、眼镜 0.2.63）。Sandbox 属于 `pruned_sandbox/orange_sandbox`，应用基线 `d90c9b6`；Studio 属于 `free6gc/RELEASE2.0`，四检查点由 `a12c0ec` 及后续 Warning/Live 提交提供，单独部署到 7865。它们不安装在 Nano，本包未携带 APK 或服务器容器。

## 20261112 发布记录（相对 20261111）

本节及下文“本包”的实测结论针对已归档的 20261112 包。此文件也是构建模板；
更换源码、日期或输入后必须重新验证，不能把历史结果当作新包验收。
该包本机路径为 `/home/huawei/orange/releases/nano-offline/20261112/orange-nano-patch-20261112/`，
不随 Git 分发；隔离测试共 132 项（49+67+10+3+3），目标 Nano 硬件验收仍待完成。

- `POST /identity/prepare`：手机 Patrol 先准备狗与眼镜身份、发布能力、发现候选，Confirm 再派遣。
- 注销后的暂停状态跨重启保留；已有有效身份复用，网络超时或权限错误不会清除身份。
- 准备/注销请求互斥、重复请求去重；命令或视频忙碌时拒绝准备。
- 同步部署文档、启动测试隔离和身份管理测试。

本次实读 dog `orange_dog/a2c35bc` 的 26 个及 dog-ctrl `orange_dog/866bdd2` 的 22 个跟踪文件，均与本机一致；详情见 `live-audit.json`。
狗上仓库外启动入口已改为薄包装，实际调用的仓库内脚本与旧 Nano 包根入口逐字节相同。
本包保留旧 Nano 启动布局及其配置路径，不复制现役 `/etc/orange-demo/dog-media.json` 或 `dog_init.json`，也不执行仓库部署文档中的现场路径迁移。

## 历史 20261111 补丁新增（相对 20261110）

- Studio 自动跟随实际核心网主机，支持显式指定 URL 或关闭。
- 通过 Agent 收到 Warning 时，与设备 Content＋语音调用并行释放 Studio 检查点 3、4；场景播放表示指令已发出，不代表设备动作已成功。
- 等待检查点上限由 60 秒增至 600 秒，临时网络失败重试，并绑定原始 run_id；场景重置后不会释放新一轮检查点。
- 设备调用抛出异常时，取消仍在等待的播放任务。已启动的场景不倒放。

新版本仍从 **20261013-current 基线直接累计安装**。已经装了 20261110 或 20261111 补丁的设备，不要直接叠加：先按旧补丁 README 停服务并用旧包执行 rollback，再安装本包、重新绑定音频。旧包及其备份应保留；本包的两种旧补丁迁移验证结果见 `validation/`。回滚会恢复当时的 core.env，之后的现场配置变更请先记录。

## 1. 拷贝和停止服务

先用旧包完成基础安装，再把本补丁 `.tar.gz` 和 `.sha256` 拷入新狗。以下命令均在 **新 Nano** 上执行，不在 WSL/现役 NX 上运行：

```bash
sha256sum -c orange-nano-patch-@RELEASE_DATE@.tar.gz.sha256
tar -xzf orange-nano-patch-@RELEASE_DATE@.tar.gz
cd orange-nano-patch-@RELEASE_DATE@
sudo systemctl stop dog-init.service
```

更新器不自动启停服务。若提示应用仍在运行，先检查 PID/启动方式并停止残留应用，再继续，避免重复启动导航。安装不修改开机启用状态；更新期间不要重启设备。

## 2. 预检和离线更新

```bash
sudo python3 update.py check
sudo python3 update.py install
```

检查基础安装标记、Nano/系统型号、目标文件旧哈希、SDK 内容和软件包校验，至少需 2 GiB 空间。
若旧文件被现场修改，预检会列出差异并拒绝覆盖；不要使用强制参数绕过。

安装不访问互联网。依赖来自 `voice-wheelhouse/`、`wheelhouse/`，保留旧 Python 3.12 和系统 ROS，使用镜像自带的 Python 3.9.5 建立独立语音环境。服务始终保持停止。

运行日志可额外记录：

```bash
set -o pipefail
sudo python3 update.py install 2>&1 | tee ~/orange-patch-install.log
```

上面是安装命令的替代写法，不是第二次安装。成功后重复 install 会拒绝，应使用 verify。
自动备份位于 `/var/backups/orange-patch/<时间-PID>`，补丁状态位于 `/var/lib/orange-patch/orange-nano-patch-@RELEASE_DATE@.json`。异常会尝试自动回滚，若回滚失败请保留输出与备份。

## 3. 上传并绑定本机音频

安装后状态为 `installed_audio_unbound`。必须先完成此步再启动组合功能。
狗体内部网络应可达 `192.168.123.161`，这是内网通信，**无需互联网**。

```bash
sudo python3 setup_audio.py
```

脚本先查询音频库，以带文件哈希的名称查找已有记录；不存在时上传正式 WAV，核对回执后把这只狗的 ID 写入 `core.env`。
它不会试听、执行 Content 或启动服务。新设备不能沿用现役狗的 UUID。

如果上传连接中断，再执行时会先查询，尽量避免重复上传。若同名记录不止一条，会要求操作员检查后显式指定：

```bash
sudo python3 setup_audio.py --audio-id 设备返回的完整ID
```

音频日志在 `/var/log/orange-patch/`。未绑定或语音环境不存在时，`/warning` 返回 503 并且不触发 Content。

## 4. 验证、启动、验收

```bash
sudo python3 update.py verify
sudo systemctl start dog-init.service
curl --noproxy '*' -fsS http://127.0.0.1:8080/health
```

确认旧狗没有占用新狗的核心网角色/隧道资源。`verify` 只校验文件、依赖、导入和 CLI，不发动作；输出 `ready` 表示已绑定音频。

在狗站立、场地允许动作时，以下会执行 **Content 动作＋Warning 语音和双音警报**：

```bash
curl --noproxy '*' --max-time 25 -sS -w '\nHTTP %{http_code}\n' \
  -X POST http://127.0.0.1:8080/warning
```

无需额外并发调用 `/content`。成功返回 `ok=true`，`motion.sdk_code=0`、`audio.audiohub_code=0`。单项失败分别报告，不能盲目重试整个组合。语音目前仍受外层 15 秒期限影响，偶发连接延迟可能超时。

直接调用上述 HTTP `/warning` 只验证狗的组合动作；Studio 检查点联动由 Agent 接收手机/眼镜的 warn/warning 指令触发。验收 Studio 时应从手机/眼镜发出 Warning，并确认当前服务有检查点 4。

新增身份流程可通过手机 1.2.7 / 眼镜 0.2.63 的 Patrol 验收：先确认身份准备与候选发现，再由现场操作员点 Confirm 派遣。不要把 Agent `/health` 的 `ok=true` 当作 `ready=true`；注销暂停后需要显式准备，重启不会自动恢复身份。

随后按现场计划验证相机、组网、视频、手机/眼镜 Warning、0.8 米巡检、转向与停止；安装验证不等于真机运动验收。

## 5. 回滚

先停止服务，确保没有残留应用，再执行：

```bash
sudo systemctl stop dog-init.service
sudo python3 update.py rollback
```

恢复备份的应用文件、旧 SDK 和 `core.env`，移除新语音运行目录。服务仍停止，身份与私钥不触碰。回滚后新上传的狗体音频库条目仍保留；它不会自行播放。
如安装后又修改过网络配置，回滚会恢复安装前的 `core.env`，请先记录后续现场修改。

## 验证范围

包内 `validation/` 记录本包的 ARM64 隔离安装、测试、两种旧补丁迁移和回滚结果；测试前保存全部输入哈希，测试日志与最终归档绑定，未复用旧包测试结果。
没有另一只 Nano 实机，因此该目标上的扬声器固件、相机、运动、核心网和断电后音频保留仍需验收。现役狗没有安装这个整包。

## 维护者：构建与验证来源

此目录保存固定 20261013 基线的补丁工具，`@RELEASE_DATE@` 由构建脚本替换。
完整历史包及原厂 rootfs 验证夹具位于构建机 `/home/huawei/orange/releases/nano-offline/`，不随 Git 分发。
需要事先准备原基线归档（摘要见 CHANGELOG）、两个狗端仓库、Python SDK wheel 和 ARM64 Python 3.9 语音依赖。
当前源码基线为 dog `orange_dog/a2c35bc`、dog-ctrl `orange_dog/866bdd2`；后续实际选择以 manifest 中完整 HEAD 为准。

```bash
python3 tools/release/nano-patch/build_patch.py \
  --orange-root /path/to/orange \
  --base /path/to/orange-nano-offline-20261013-current.tar.gz \
  --sdk-wheel /path/to/agent_connect_sdk-0.17.11+orange.20261110-py3-none-any.whl \
  --voice-wheels /path/to/arm64-python39-wheelhouse \
  --release-date YYYYMMDD --output /path/to/orange-nano-patch-YYYYMMDD
```

构建器只导出 dog/dog-ctrl HEAD 中的普通文件，拒绝已跟踪文件的暂存/未暂存改动和符号链接，排除全部未跟踪文件。
默认不使用现场增量。确需包含导出的 `orange_dog.py` 时，显式传 `--live-dog /path/to/export.py`；
若不同于 HEAD，还必须传 `--overlay-reason '导出来源、时间及差异原因'`，哈希与 diff 会入包。
Python SDK wheel 的全部 `agent_sdk/` 文件必须与 SDK HEAD 一致，版本固定为 `0.17.11+orange.20261110`。
构建脚本不构建 wheel、不下载依赖、不生成语音、不替换 nav，也不会自动部署任何服务。

最终归档前必须在隔离 ARM64 原厂 rootfs 中重新执行完整验收。先审核并调整外部 harness 中的 rootfs、挂载和包路径；
`validate-overlay.sh`、`validate-in-chroot.py` 不是通用测试器，不能把历史硬编码路径直接用于新包。
输入快照及日志放在包目录外，避免把生成中的证据当作待测输入：

```bash
# BUNDLE、HARNESS、INPUTS、VALIDATION_LOG 指向本次新的包/夹具/外部证据文件。
set -euo pipefail
python3 tools/release/nano-patch/finalize_patch.py \
  --bundle "$BUNDLE" --harness-dir "$HARNESS" \
  --validation-inputs "$INPUTS" --snapshot-validation > "$VALIDATION_LOG"
# 只有在已审核的构建机隔离环境执行；记录须包含五组 unittest 和验收标记。
sudo bash "$HARNESS/validate-overlay.sh" >> "$VALIDATION_LOG" 2>&1
python3 tools/release/nano-patch/finalize_patch.py \
  --bundle "$BUNDLE" --harness-dir "$HARNESS" \
  --validation-inputs "$INPUTS" --validation-log "$VALIDATION_LOG"
```

快照绑定全部包文件、测试及两个 harness 的哈希；最终归档会拒绝输入已变、日志没有对应快照标记、缺少测试/回滚证据的情况。
输入快照是操作审计依据，不是硬件验收或签名证明。不要在测试后补生成快照或复用旧日志。

本机回归（无真实安装/网络设备操作）：

```bash
python3 -m unittest discover -s tools/release/nano-patch/tests -p test_build_inputs.py -v
python3 -m unittest discover -s tools/release/nano-patch/tests -p test_update.py -v
DOG_SOURCE=/path/to/dog python3 -m unittest discover -s tools/release/nano-patch/tests -p test_studio_overlay.py -v
# 以下必须使用安装了本次 SDK wheel 及依赖的独立 Python 环境：
python3 -m unittest discover -s tools/release/nano-patch/tests -p test_sdk_adaptive.py -v
```
