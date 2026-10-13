# 新机器狗 Orin Nano 全离线安装

**所有安装命令都在新狗的 Nano Ubuntu 终端执行。Windows/WSL 只用来保存、拷贝安装包。**

## 1. 刷好对应镜像

先按宇树流程把 `go2_nano_Jetpack5.1.1_20250930.img.bz2` 刷到新狗。
本安装包只适配该镜像：Ubuntu 20.04.5、JetPack 5.1.1 / L4T R35.3.1、ARM64。
镜像 SHA256：`4ccc2ea272d4804ea9bd572efe630a352f5e365920d00ccaed8288b337aef092`。

原镜像另存，未嵌入本服务包。原厂 ROS Foxy/Noetic、colcon、编译器、GDB、相机/扩展坞驱动、升级服务、内核/设备树都已在镜像中，安装器不会拿 NX 系统文件覆盖它们。

## 2. 拷包并配置（新狗终端）

将 `orange-nano-offline-20261013-current.tar.gz` 和 `.sha256` 放到 U 盘，拷入新狗用户目录。

```bash
sha256sum -c orange-nano-offline-20261013-current.tar.gz.sha256
tar -xzf orange-nano-offline-20261013-current.tar.gz
cd orange-nano-offline-20261013-current
cp deploy.env.example deploy.env
nano deploy.env
```

配置项：

| 项目 | 填写方式 |
| --- | --- |
| CORE_HOST | 核心网 IP/域名，当前为 192.168.1.13 |
| RUNTIME_PORT / MASQUE_PORT | 角色 B 当前 8089 / 8444 |
| DOG_NAME | 新狗展示名称，如 D-Nano |
| CAMERA_ID | 用 `ls /dev/video*`、`v4l2-ctl --list-devices` 确认彩色相机节点；4 表示 /dev/video4 |
| CONTROL_INTERFACE | 内部控制网卡，通常 eth0；保留原厂 192.168.123.x 网络 |
| WIRED_CONNECTION_NAME | 留用默认 `Orange USB tether`，必须是新的未占用名称。这是脚本可删除重建的手机 USB 共享配置，绝不能填内部 eth0 连接名 |
| CORE_ALLOCATION_CONFIRMED | 确认核心网资源安排后改为 yes |

新狗首次启动自动生成自己的 Agent 身份，不复制现役私钥。若替代旧狗使用同一角色 B，启动新狗之前让旧狗离线；两只同时使用必须先给新狗分配独立核心网角色/Runtime/隧道资源，不能仅修改名称。

安装无需互联网，也无需核心网在线；核心网确认项表示你已安排启动时的角色资源。相机和内部网卡在预检时必须存在；USB profile 由首次联网时创建。

## 3. 完全离线安装

```bash
sudo bash install.sh check
sudo bash install.sh install
```

需要约 8 GiB 空闲空间。脚本检查包内 SHA256、Nano 型号、基础包版本，再备份并安装。全部 Python 包从 wheelhouse 装入两个独立 Python 3.12.13 venv；ROS 用系统 Python 3.8，从完整源码编译 14 个包。耗时取决于 Nano CPU/存储，编译并发限制为 2。

安装范围：dog-ctrl、orange_dog/Agent SDK 0.17.11、nav 与 clients/消息/底盘/手柄等支持包、Unitree SDK 1.0.1、CycloneDDS C 0.10.5 / Python 0.10.2、USB 共享监测及 18892 配置入口、原 dog-init 启动链、ROS 日志配置、视频 GDB 取证。保留原厂系统服务。现役未运行应用容器，不需要 Docker 镜像或远端 sandbox/ASR 模型。

SDK 0.17.11 包含相机帧独立复制与安全释放、可选 group_config 与至少 20 秒协商等待；从提交源码构建 wheel。锁定的两套依赖与 2026-10-12 实机只读清单完全一致。

**安装结束服务保持停止，开机自启动也暂不启用。** 整个安装不发站起、巡逻、移动指令。

## 4. 在新狗启动、验收

确认相机及手机 USB 共享连接正常、核心网可达、原狗未占用相同角色后，在安全场地执行：

```bash
sudo systemctl enable --now dog-init.service
sudo bash check-services.sh
```

唯一启动链：dog-init → dog-ctrl :8080、Agent、nav :10088、配置入口 :18892；GDB 取证也在同一服务下。不要再开独立 Agent/nav/voice 服务。

`check-services.sh` 只读状态，不发动作。检查 HTTP /health、/status 的状态新鲜度，ROS 四节点、Agent ready/连接日志，以及实际视频流。手柄监听和 nav 可发运动命令，功能动作验收需在安全场地进行；巡逻派遣会等待站稳后按时间控制向前走约 1 米（不是里程计闭环），并同步上传视频。

如果启动后等待手机网络，查看 `journalctl -u dog-init.service -n 80 --no-pager`。
日志路径：`/home/aicor/compute_network/sandbox-demo/.dog-services/logs/`、`/var/log/orange-demo/ros/`、`/var/lib/orange-video-capture/`。应用日志做轮转；取证最多两份 core 且剩余空间超过 8 GiB 才生成。长期运行应监控 ROS/取证日志空间。

## 5. 失败与回退

预检失败不修改业务目录。依赖或编译失败会停止安装并保留日志；不要使用 apt/pip 联网临时补装来掩盖缺包。把错误日志和 `manifest.json` 保留下来。

安装前备份位置会打印为 `/var/backups/orange-offline/时间戳`。可执行：

```bash
sudo bash rollback.sh /var/backups/orange-offline/时间戳
```

回退移走本次应用文件并恢复备份，保持服务停止；不会删除首次运行新生成的身份。安装器只适合原镜像新机，不覆盖既有业务环境；重复安装会拒绝执行。

## 6. 已验证与待验收

已在该 Nano 镜像的 ARM64 隔离环境、断网条件下验证两套 Python 安装、pip check、视频/控制/SDK 导入、CycloneDDS 编译、全部 ROS 源码编译及原生消息库导入。`validation/` 留存构建日志和清单。
没有新 Nano 实机，无法提前确认相机 USB3、控制状态/动作固件、核心网组网、视频性能和长时间稳定性；这些需要到新狗上验收。制作期间没有修改现役狗环境、重启服务或发机械动作。


## 7. 出问题时导出日志（无需远程登录）

**请使用本次更新后的包。** 每次 `install.sh check/install` 自动保存完整终端输出（含 pip/colcon 错误）、命令时间戳、脚本行号、环境快照、退出码与包清单：
`/var/log/orange-offline/UTC时间-PID-check或install/`。若该目录不可写则使用 `/tmp/orange-offline/`。
`console.log` 是完整安装记录，`status` 是结束状态；断电/强制终止可能仅留 `running`，尚未落盘的最后输出无法恢复。预检失败也记录。非 sudo、参数错误或检测到现役狗时直接拒绝，不创建日志。

遇到安装失败、服务启动失败或运行异常，在**新狗终端、解压后的安装包目录**执行：

```bash
sudo bash collect-diagnostics.sh
# 也可直接输出到已挂载 U 盘的可写目录：
sudo bash collect-diagnostics.sh /media/unitree/你的U盘目录
```

拷走打印的 `orange-diagnostics-*.tar.gz` 和 `.sha256` 给我，同时告诉我：做了哪一步、故障大约时间、看到什么现象。无需复制整个系统，也不需要我登录。

诊断包包含：安装日志及退出码、包版本/已装代码校验和、配置、磁盘/内存/时间/温度、网卡/路由/端口、USB/相机枚举、systemd 状态和当前/上次启动日志、内核日志、控制 GET 健康状态、ROS 节点清单/构建日志、Agent/控制/ROS/GDB 文本日志。缺少工具、无服务或超时也会记入结果，不要求安装成功才能打包。

收集不安装软件、不重启服务、不发动作、不打开视频流、不探测远端核心网。避免无限收集，每个文件/命令最多保留末尾 4 MiB，每个日志目录取最新 150 个文件，总文本上限 160 MiB；截断/遗漏/超时见 `collection-index.json`。完整安装原始日志仍保留在新狗上述目录，必要时可再单独拷贝。脚本不会设置日志自动删除。

不打包 SDK 身份目录、私钥文件和 native `.core` 内存转储，常见 password/token 字段做脱敏；设备/IP/任务元数据可能保留。若之后确需 native core，可根据这次文本日志再指定导出，不要先打包整个身份目录。

## 2026-10-13 更新

狗端包含身份管理接口 :8081、巡逻站稳检查与约 1 米前进、Content 动作映射。视频采集上限 20 fps，编码 250 kbps，分包限速 1 Mbps；不扩大接收缓冲。首次运行生成新身份。原始 Nano 镜像未修改。各源码提交与文件哈希见 manifest.json；本次和基线验证范围分别记录，硬件验收仍需在新 Nano 上完成。
