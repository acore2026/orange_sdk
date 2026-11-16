# Remote Control 内部通信库

手机与眼镜复用的纯 Kotlin/JVM 库，包含遥控命令、单行 UTF-8 TCP 请求/响应、眼镜 TCP 服务端、主机与端口校验、机器狗管理 HTTP 客户端。当前保留既有 Studio 重置客户端。

迁移前位于 `android/demo-remote/`。为保持接口兼容，包名 `com.rayneo.demo.remote`、类名 `DemoRemote` / `DemoRemoteServer` 和 Gradle 项目 ID `:demo-remote` 保持原样。

请求 JSON 的业务解析、去重、确认、忙状态判断与执行流程由应用负责。从仓库根目录执行：

```bash
./gradlew :demo-remote:test
```

后续新增手机专用的 Studio 客户端放在手机应用中；多个应用共同依赖的遥控协议与传输能力才放在本库。参见 [目录规范](../../docs/architecture/REPOSITORY_LAYOUT.md)。
