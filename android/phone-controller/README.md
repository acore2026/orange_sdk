# 巡检演示手机兜底遥控

参考 `6g-core/RAYNEO-ctrl` 的 `IMT2030-master` 分支（commit `8e43b7a5d6b26617019dd649ff1e33fde1048b33`），沿用手机经局域网 TCP 控制眼镜的方式，为当前 SDK 巡逻流程实现独立 Android 应用。当前协议带配对码和请求 ID，需要本次配套眼镜 APK，不能直接使用旧版遥控器。

## 安装与连接

- 手机 APK：`build/outputs/apk/release/phone-controller-release.apk`，包名 `com.rayneo.demo.controller`；Android 8.0 及以上。
- 本机可直接取用的副本：`C:\Users\huawei\Downloads\RAYNEO-patrol-controller-1.0.0.apk`。
- 眼镜 APK：`../example-app/build/outputs/apk/rayneo/release/example-app-rayneo-release.apk`，版本 `0.2.46-rayneo`。
- 当前 APK 使用开发签名，适用于现场演示。
- 手机和眼镜接入可互相访问的局域网，眼镜应用保持前台。手机填写眼镜的 Wi-Fi 地址和配对码；此处不是核心网地址。
- 2026-10-10 核验时眼镜地址为 `192.168.137.225:8791`，换网后请重新读取。

在本机 PowerShell 查询眼镜配置、配对码和 Wi-Fi 地址（不修改配置、不重启）：

```powershell
wsl -d Ubuntu --cd /home/huawei/orange python3 sdk/tools/set_core_address.py glasses
```

将查询结果中的 `pairing_pin` 填入手机，点击“连接 / 刷新状态”。眼镜数字身份恢复成功后，操作按钮按当前阶段启用。

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

- TCP `8791`，每次连接一行 UTF-8，最多 8192 字节：`AUTH <8位配对码> <JSON>`。
- JSON 包括 `command`、`request_id`、`language`；巡逻额外传 `area`，配置传 `host`。
- `OK ACCEPTED` 只表示接收。手机继续查询 `STATUS` 获取真实阶段和执行结果。
- 重复请求 ID 在当前眼镜 Activity 生命周期内去重；同 ID 不同内容拒绝。重启后不保留去重表。
- 任何动作均不自动重试。通信超时可能发生在已执行之后，应刷新状态再判断。
- PIN 用于局域网配对，TCP 链路本身不加密；本应用用于受控演示网络。

## 构建与验证

```bash
cd /home/huawei/orange/sdk/android
ANDROID_HOME=/home/huawei/android-sdk ./gradlew :demo-remote:test :example-app:testRayneoDebugUnitTest :phone-controller:assembleRelease :example-app:assembleRayneoRelease
```

已完成编译、共享协议单测、眼镜 APK 安装，以及真机配置读写、鉴权、重复请求和冲突请求验证。机器狗核心网配置已接入原 `dog-init.service`，没有新增独立服务。验证未下发巡逻或机械动作。

2026-10-10 最终复核：Windows 本机通过真实局域网 `192.168.137.225:8791` 查询成功，眼镜返回 `phase=LISTENING, ready=true`；眼镜 Runtime 和 downlink WebSocket 初始化成功。机器狗 `dog-init` PID 2469、Agent PID 3486 保持原进程。共享协议 3 项、眼镜应用 37 项、Python 配置工具 3 项测试通过。

本轮只有眼镜通过 USB 连接，手机 APK 尚未在真实手机安装；手机界面及手机到眼镜的局域网链路仍需接入手机后验收。
