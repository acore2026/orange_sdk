# RayNeo 巡检应用

正式眼镜应用，负责巡检发现与确认、组网和算力会话编排、语音控制、机器狗 A2A 指令及双目视频展示。

- `src/main/`：既有业务编排、公共资源和测试辅助实现；历史 `AgentTestRunner` 等类名保持原样。
- `src/rayneo/`：眼镜界面、设置、应用入口和 HUD 资源。
- `src/test/`：既有应用测试。
- `libs/`：Mercury SDK AAR。
- `scripts/`：Windows 安装脚本。

从仓库根目录构建：

```bash
./gradlew :example-app:assembleRayneoDebug :example-app:testRayneoDebugUnitTest
```

输出：`apps/rayneo-patrol/build/outputs/apk/rayneo/debug/example-app-rayneo-debug.apk`。

通用界面位于 [`examples/android/`](../../examples/android/README.md)，继续作为同一 Gradle 项目的 `generic` flavor 编译，共享原有编排；不改变已有应用 ID、版本或运行流程。

安装见 [眼镜安装说明](../../docs/deployment/雷鸟眼镜App安装说明.md)，SDK 和流程细节见 [Android 使用说明](../../sdks/android/README.md)，新代码归属见 [目录规范](../../docs/architecture/REPOSITORY_LAYOUT.md)。
