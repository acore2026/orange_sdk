# 模拟服务与周边配套

本目录统一容纳模拟服务及后续周边配套，按服务建立子目录。每个服务自带源码、依赖、接口测试、Docker/Compose、启动和打包脚本及使用说明。

当前服务：[`mock-sandbox/`](mock-sandbox/README.md)，提供算力会话媒体、识别目标、文本和音频控制接口的模拟实现。

项目级部署工具放在 `tools/release/`；跨组件验证放在 `integration-tests/`；嵌入应用的 HTTP 客户端留在对应应用或内部库中。当前未包含独立 Studio 或时间同步服务。参考 [目录规范](../docs/architecture/REPOSITORY_LAYOUT.md)。
