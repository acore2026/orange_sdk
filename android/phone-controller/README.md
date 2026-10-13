# 巡检演示手机兜底遥控

## 1.1.2：独立 Sandbox 地址（2026-10-12）

配置页新增 Sandbox IP/域名，独立于核心网地址。保存至眼镜后重启眼镜 App 生效，保留数字身份。需要配套眼镜 0.2.48；旧眼镜会返回 UNKNOWN_COMMAND。

该配置用于 ASR `http://<Sandbox>:9004/api/v1/transcribe`。视频 endpoint 仍由核心网 C-02 下发；换 Sandbox 时必须同步更新核心网配置，手机不会覆盖会话绑定。
协议新增 `CONFIG_SANDBOX`（`host` 字段），STATUS 返回 `sandbox_host`（当前）及 `saved_sandbox_host`（已保存）。沿用主机校验、忙状态保护和请求 ID 去重。

## 1.1.1：底部双页导航（2026-10-12）

底部固定“配置 / 导航”两个入口；各页面独立滚动，底栏不随内容滚动。
配置页保存眼镜 IP（默认 TCP 8791）、机器狗 IP（默认 HTTP 8081，可填转发端口）、核心网地址，并提供两个去注册按钮。
导航页保留巡逻、语音、视频控制及原有动作确认。

无需配对码。手机直接发送一行 JSON 至眼镜；狗端身份接口不要求 Authorization。
设备地址可互通即可连接。必须同时更新配套眼镜 APK 和狗端代码，旧版仍要求配对码。
请求 ID 去重、忙状态保护、驱逐二次确认均保留。

去注册成功后保持无身份；失败才清除本地 Profile/Card 并重新注册，失败状态如实显示。
狗端仍在原 Agent / dog-init 内启动，部署 `orange_dog.py` 与同目录 `identity_management.py`，无额外 systemd 服务。
手机中转需单独转发管理 TCP 8081；SSH 40032 转发不能代替它。

手机及眼镜 APK 统一交付到当前 orange 工作区 `apk/`。
本地编译输出仍为 `build/outputs/apk/release/phone-controller-release.apk`。

以下是旧版背景与演示流程，历史真机验证不代表 1.1.1 已在实机安装验收。

参考 `6g-core/RAYNEO-ctrl` 的 `IMT2030-master` 分支（commit `8e43b7a5d6b26617019dd649ff1e33fde1048b33`），沿用手机经局域网 TCP 控制眼镜的方式，为当前 SDK 巡逻流程实现独立 Android 应用。当前协议带请求 ID，需要本次配套眼镜 APK，不能直接使用旧版遥控器。

## 安装与连接

- 手机 APK：`build/outputs/apk/release/phone-controller-release.apk`，包名 `com.rayneo.demo.controller`；Android 8.0 及以上。
- 本机副本：`/home/huawei/orange/apk/phone-controller-1.1.2-sandbox-config.apk`。
- 眼镜 APK：`../example-app/build/outputs/apk/rayneo/release/example-app-rayneo-release.apk`，版本 `0.2.46-rayneo`。
- 当前 APK 使用开发签名，适用于现场演示。
- 手机和眼镜接入可互相访问的局域网，眼镜应用保持前台。手机填写眼镜的 Wi-Fi 地址；此处不是核心网地址。
- “数字身份”页可填写眼镜 IP 和机器狗 IP。去注册成功后设备保持无身份；如果去注册失败，眼镜 SDK 或狗 Agent 会清理本地状态并重新注册。手机不会直接改设备的身份文件。
- 2026-10-10 核验时眼镜地址为 `192.168.137.225:8791`，换网后请重新读取。

在本机 PowerShell 查询眼镜配置和 Wi-Fi 地址（不修改配置、不重启）：

```powershell
wsl -d Ubuntu --cd /home/huawei/orange python3 sdk/tools/set_core_address.py glasses
```

无需配对码，点击“连接 / 刷新状态”。眼镜数字身份恢复成功后，操作按钮按当前阶段启用。

手机通过 USB 连接本机后，用 `adb devices -l` 找到手机序列号，再用 `adb -s 手机序列号 install -r APK路径` 安装。不要把眼镜序列号作为手机目标。

## 演示流程

1. 填写区域，点“发现机器狗”：直接使用明确的巡逻意图，跳过 ASR，仍经过真实核心网发现流程。
2. 核对眼镜候选摘要后点“确认派遣”：经过确认对话框后，进入现有派遣、组网和视频流程。
3. “威吓歹徒”复用 `content → /happy`；“发起驱逐”只进入待确认，30 秒内点“确认驱逐”才发送 `expel`。
4. “开始眼镜录音 / 结束录音并识别”仍使用眼镜麦克风及既有语音接口。
5. “仅重连视频”重建算力视频会话；“隐藏 / 显示视频”只切换眼镜画面。

界面支持中文、英文切换。英文预设指令使用相同结构化命令；语音识别继续沿用原来的中英文接口。

手机不注册第二个核心网身份，所有业务通过眼镜进入当前 SDK 链路。因此更换眼镜核心网地址后，手机后续控制也使用新地址。手机设置页可保存新的核心网 IP/域名至眼镜，眼镜应用下次启动生效。现场需要立即切换时使用下述本机命令。

## 更改核心网地址

见 [本机配置命令](../../tools/README.md)。仅更换主机地址，Runtime/MASQUE 各端端口保持原部署值；不会改变 sandbox 语音接口地址。

## 协议与状态

- TCP `8791`，每次连接一行 UTF-8，最多 8192 字节：`<JSON>`。
- JSON 包括 `command`、`request_id`、`language`；巡逻额外传 `area`，配置传 `host`。
- 数字身份命令为 `DEREGISTER_GLASSES`；机器狗去注册使用 HTTP POST `/identity/deregister`（默认端口 8081），失败时由狗端执行清除本地状态并重注册。
- `OK ACCEPTED` 只表示接收。手机继续查询 `STATUS` 获取真实阶段和执行结果。
- 重复请求 ID 在当前眼镜 Activity 生命周期内去重；同 ID 不同内容拒绝。重启后不保留去重表。
- 任何动作均不自动重试。通信超时可能发生在已执行之后，应刷新状态再判断。
- TCP 控制链路直接按 IP 连接，不使用配对码。

## 构建与验证

```bash
cd /home/huawei/orange/sdk/android
ANDROID_HOME=/home/huawei/android-sdk ./gradlew :demo-remote:test :example-app:testRayneoDebugUnitTest :phone-controller:assembleRelease :example-app:assembleRayneoRelease
```

已完成编译、共享协议单测、眼镜 APK 安装，以及真机配置读写、鉴权、重复请求和冲突请求验证。机器狗核心网配置已接入原 `dog-init.service`，没有新增独立服务。验证未下发巡逻或机械动作。

2026-10-10 最终复核：Windows 本机通过真实局域网 `192.168.137.225:8791` 查询成功，眼镜返回 `phase=LISTENING, ready=true`；眼镜 Runtime 和 downlink WebSocket 初始化成功。机器狗 `dog-init` PID 2469、Agent PID 3486 保持原进程。共享协议 3 项、眼镜应用 37 项、Python 配置工具 3 项测试通过。

本轮只有眼镜通过 USB 连接，手机 APK 尚未在真实手机安装；手机界面、手机到眼镜的局域网链路，以及机器狗 8081 管理端点仍需现场验收。

## 1.1.3（2026-10-13）

Sandbox 首次默认地址改为 192.168.2.150；升级保留用户原有输入，请手动更新后保存至眼镜。配套眼镜 0.2.49 默认相同。APK 在 orange/apk/。该地址当前只验证了 WSL 本地健康接口；眼镜直连 ASR 超时，配置更新不等于网络已打通。

### Sandbox 路由同步
配套眼镜使用 rayneo-0.2.49-dynamic-asr-route.apk。手机保存 Sandbox 地址后重启眼镜 App，ASR URL 和初始 TUN 主机路由同时更新，旧路由随旧 VPN 释放；域名在初始化时解析为 IPv4 路由。与 Runtime/MASQUE 同 IP 的 ASR 保持物理网络访问，避免捕获外层隧道。视频仍由核心网配置。

## 1.2.0 配置与遥控

配置页顶部独立连接眼镜（默认 TCP 8791）和机器狗（默认 HTTP 8081），分别显示连接结果。
下方分别显示眼镜/狗身份去注册处理结果、眼镜/狗核心网地址保存结果；Sandbox 配置保留。
核心网保存不立即切换网络：眼镜需重启 App，机器狗需经原启动链重启以重新读取 /etc/orange-demo/core.env。

遥控页使用 2–3 列按钮，包含前进、后退、左转、右转、停止、威吓，以及巡逻、驱逐、录音、视频控制。
手机不再弹确认对话框；驱逐仍分发起/确认两次操作，眼镜保留危险动作警告和 30 秒有效期。
方向指令需要眼镜 0.2.53 或更新版本；机器狗连接和核心网配置需要 2026-10-13 新版管理接口。
