# 雷鸟眼镜 App 安装说明

## 1. 没有 `adb.exe` 怎么下载

从 [Android 官方 SDK Platform-Tools 下载页](https://developer.android.com/tools/releases/platform-tools) 下载 **Windows** 版压缩包，解压后将 `platform-tools` 文件夹放到 `C:\Android\platform-tools`。确认 `C:\Android\platform-tools\adb.exe` 存在，并在 PowerShell 中执行：

```powershell
& "C:\Android\platform-tools\adb.exe" version
```

## 2. Zadig 下载及驱动配置

从 [Zadig 官网](https://zadig.akeo.ie/) 下载可执行文件；无需安装，以管理员身份运行。仅当眼镜已开启 USB 调试、使用数据线连接电脑，但 `adb devices` 仍看不到设备时，按以下步骤配置：

1. 在 Zadig 的 `Options` 菜单中勾选 `List All Devices`。
2. 选择眼镜的 `ADB Interface (Interface 1)`，核对 USB ID 为 `18D1:4EE2`、接口号为 `01`。设备标识不符时不要继续。
3. 将目标驱动选为 `WinUSB`，点击 `Install Driver` 或 `Replace Driver`。不要选择 `MTP (Interface 0)` 或其他设备。
4. 重新插拔眼镜，在 PowerShell 中执行以下命令；首次连接时，在眼镜上允许 USB 调试授权。输出中应有一台状态为 `device` 的设备。

```powershell
& "C:\Android\platform-tools\adb.exe" kill-server
& "C:\Android\platform-tools\adb.exe" start-server
& "C:\Android\platform-tools\adb.exe" devices -l
```

## 3. 安装脚本怎么使用

将眼镜连接电脑并开启 USB 调试，确认 `adb devices -l` 中**恰好一台**设备处于 `device` 状态。打开 PowerShell，进入 SDK 仓库的 `android` 目录，运行：

```powershell
cd "<SDK仓库目录>\android"
powershell -ExecutionPolicy Bypass -File .\install-rayneo-windows.ps1
```

脚本默认使用 `C:\Android\platform-tools\adb.exe` 和 `android\example-app\build\outputs\apk\rayneo\release\example-app-rayneo-release.apk`。它会覆盖安装 APK，通过 ADB 执行 `appops set com.rayneo.agent.example.rayneo ACTIVATE_VPN allow`、核对授权结果，再启动 App。App 启动后会自动连接安全网络、恢复数字身份，不需要再点击“启用 Agent 网络”。如果 Android 仍显示一次系统 VPN 授权页，确认后会继续自动连接。如果 ADB 或 APK 位于其他位置，指定实际路径：

```powershell
powershell -ExecutionPolicy Bypass -File .\install-rayneo-windows.ps1 `
  -AdbPath "D:\platform-tools\adb.exe" `
  -ApkPath "D:\packages\example-app-rayneo-release.apk"
```

脚本会在授权失败时停止并报错；如果眼镜仍显示“网络连接请求”，先完成一次系统授权，再检查脚本的 `ACTIVATE_VPN: allow` 输出。后续启动会自动申请网络连接，不再要求用户操作。

App 已安装且只需补授权时，可以直接执行：

```powershell
& "C:\Android\platform-tools\adb.exe" shell appops set com.rayneo.agent.example.rayneo ACTIVATE_VPN allow
& "C:\Android\platform-tools\adb.exe" shell appops get com.rayneo.agent.example.rayneo ACTIVATE_VPN
```

## 4. 怎么卸载 App

连接眼镜、开启 USB 调试并确认 `adb devices -l` 显示 `device` 后，在 PowerShell 中执行：

```powershell
& "C:\Android\platform-tools\adb.exe" uninstall com.rayneo.agent.example.rayneo
```

卸载会删除 App 数据和已有授权。重新安装时，运行第 3 节脚本再次授权。
