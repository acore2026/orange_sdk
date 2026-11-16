# 通用 Android SDK 示例

`src/main/kotlin/com/rayneo/agent/example/MainActivity.kt` 是既有通用 SDK 联调界面，迁移前位于 `android/example-app/src/main/`。

本目录不是独立 Gradle 项目。为保留现有实现及 Kotlin `internal` 可见性，根 Gradle 工程仍通过 `:example-app` 的 `generic` source set 编译该入口，并复用 `apps/rayneo-patrol/` 中原有业务编排和资源。

从仓库根目录执行：

```bash
./gradlew :example-app:assembleGenericDebug
```

输出位于 `apps/rayneo-patrol/build/outputs/apk/generic/debug/example-app-generic-debug.apk`。

眼镜正式入口位于 `apps/rayneo-patrol/src/rayneo/`，`rayneo` flavor 不编译本目录界面。参考 [Android SDK 使用说明](../../sdks/android/README.md) 和 [代码组织规范](../../docs/architecture/REPOSITORY_LAYOUT.md)。
