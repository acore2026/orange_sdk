# 通过 Windows 网络栈连接机器狗 SSH

用于 WSL 网络无法直接到达现场 SSH 转发口、但 Windows 能连接的情况。
普通 Linux SSH 可直达时无需此工具。迁入自现场 `orange/tools/dog-access/`，
已移除写死的 IP、Windows 用户目录和 Python 安装路径。

依赖 WSL Bash、`wslpath`、Linux OpenSSH 和 Windows Python 3。
设置 `WINDOWS_PYTHON` 为 Windows Python 可执行文件的 WSL 路径；若 Windows
`python.exe` 已在 PATH 且不是商店占位入口，可以不设置。

```bash
cp ssh-config.example ssh-config
# 编辑 ssh-config：HostName 为实际狗/手机 SSH 转发 IP，ProxyCommand 为脚本绝对路径
export WINDOWS_PYTHON='/mnt/c/path/to/python.exe'
ssh -F ./ssh-config dog
# 临时覆盖目标，%h/%p 会传给中转程序
ssh -F ./ssh-config -o HostName=192.0.2.31 -p 40033 dog
```

示例 `192.0.2.*` 必须替换。默认使用 Linux OpenSSH 的 known_hosts；首次连接前
核对设备主机密钥并通过受信任方式加入 known_hosts，脚本不关闭密钥检查。
认证由 OpenSSH 负责，中转程序只搬运二进制 TCP 数据，不保存密码。
如果要使用 Windows known_hosts，可自行在本地配置填写对应挂载路径。

旧脚本内部固定 `192.168.137.14:40032`，即使覆盖 HostName 仍会连旧地址。
新脚本强制接收 `--host`、`--port`，缺少或非法端口直接失败。
配置文件 `ssh-config` 被 Git 忽略，仓库只提供示例。

本次没有替换现役中转脚本或连接设备。提交前验证在 Linux 本地回环 TCP 上
执行二进制转发，并通过模拟 Windows Python 验证 WSL wrapper 参数传递；
Windows 真机网络栈与实际 SSH 认证需要现场单独确认。

```bash
python3 -m unittest discover -s tools/dog-access -p 'test_*.py' -v
```
