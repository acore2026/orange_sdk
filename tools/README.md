# 本机更换演示核心网地址

本工具在当前 Windows 主机的 Ubuntu WSL 环境执行。眼镜通过 USB ADB 配置，机器狗通过本机 Windows SSH 访问。填入新的 IPv4 地址或域名，不带协议或端口。

## 两个 PowerShell 命令

将示例中的 `101.245.78.174` 替换为新的核心网地址。

眼镜：保存配置并重启眼镜应用，立即按新地址初始化；会中断当前眼镜会话，不会派遣机器狗。

```powershell
wsl -d Ubuntu --cd /home/huawei/orange python3 sdk/tools/set_core_address.py glasses --host 101.245.78.174
```

机器狗：通过当前手机转发入口保存配置，下一次原服务/Agent 启动生效。

```powershell
wsl -d Ubuntu --cd /home/huawei/orange python3 sdk/tools/set_core_address.py dog --host 101.245.78.174 --ssh-host 192.168.137.14
```

机器狗命令可能依次提示 SSH 密码及 sudo 密码，请在本机终端输入现有凭据。默认 SSH 用户 `unitree`、端口 `40032`。`--ssh-host` 是连接机器狗的入口地址，不是核心网地址；手机转发入口变化时单独修改。

默认不重启机器狗服务。如需立即切换且允许中断机器狗当前任务，在第二条命令末尾加 `--restart`：它会重启原 `dog-init.service` 及其子服务。该选项本轮未在真机执行。

## 地址对应关系

| 目标 | Runtime | MASQUE | 生效方式 |
| --- | --- | --- | --- |
| 眼镜 | 新地址:8088 | 新地址:8443 | 命令默认重启应用 |
| 机器狗 | 新地址:8089 | 新地址:8444 | 默认保存，下一次原服务/Agent 启动 |
| 手机 | 经眼镜使用相同核心网配置 | 经眼镜 | 不持有第二套业务核心网地址 |

`28443` 是远端登录端口，不作为 Runtime HTTP 端口。工具不会更改 sandbox ASR URL、SSH 登录地址、token 或远端服务器本身；新服务器需要预先部署对应服务和端口。

## 其他用法

```powershell
# 查询眼镜地址和 Wi-Fi IP，不写配置
wsl -d Ubuntu --cd /home/huawei/orange python3 sdk/tools/set_core_address.py glasses

# 眼镜只保存，暂不重启
wsl -d Ubuntu --cd /home/huawei/orange python3 sdk/tools/set_core_address.py glasses --host 101.245.78.174 --no-restart
```

可通过 `--serial` / `--adb` 指定眼镜及 ADB；通过 `--ssh-host` / `--ssh-port` / `--ssh-user` / `--ssh` 指定机器狗连接。Linux/WSL 当前目录为 `/home/huawei/orange` 时，可省略 `wsl -d Ubuntu --cd /home/huawei/orange` 前缀。

## 机器狗持久化位置

- `/etc/orange-demo/core.env`：Runtime 主机和 MASQUE URL。
- `/etc/systemd/system/dog-init.service.d/core-endpoint.conf`：原服务的环境配置。
- `/home/aicor/compute_network/sandbox-demo/start_dog_services.sh`：加载同一环境文件，兼容原入口使用 `runuser` 的情况。

修改配置前保留 `.before-core-时间戳` 备份，并检查 shell 语法。机器狗启动脚本必须来自当前源码版本；工具只更新环境配置，不修改启动脚本源码，也不会创建额外后台服务。眼镜设置保存在应用 SharedPreferences，更新安装保留，卸载清数据后恢复编译默认值。

## 离线视频质量基线

见 [VIDEO_BENCHMARK.md](VIDEO_BENCHMARK.md)，工具 `video_benchmark.py` 和候选档 `video-profiles.json`。无需机器狗或眼镜，编码参数与发包预算分开比较。
