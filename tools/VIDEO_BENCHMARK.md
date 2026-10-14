# 离线视频基线与码率对比

本工具只读本地文件或生成确定性素材，不打开相机、不连接机器狗、不使用网络。
运行需要 Python 3.12、aiortc 1.14、PyAV 16、numpy、OpenCV；可使用已有开发 venv。
默认用当前 SDK 编码策略模拟两段 VP8；加 --sandbox-root 后第二段使用当前 Sandbox 策略。

```bash
python tools/video_benchmark.py --output /tmp/video-baseline --frames 120 \
  --sandbox-root /path/to/pruned_sandbox
python tools/video_benchmark.py --input /path/to/recording.mp4 \
  --output /tmp/video-recording --frames 120 --sandbox-root /path/to/pruned_sandbox
```

请使用新的输出目录。默认合成固定随机纹理的静止/平移/快速横移三段，不代表真实机器狗行走。
可以输入已有的行走、转头、复杂纹理录像，对同一文件重复比较。输入归一为 640×480、采样上限 20 fps。
低于目标 fps 的输入不插帧，报告中的名义 fps 仅为编码时间轴；请用源帧率不低于目标的素材作严格对照。

默认比较 250/500/750 kbps：分别只提高狗端、只提高 Sandbox、两端同时提高。
保持 VP8 不变，1/2/3 Mbps pacing 只用于发包预算计算，不执行网络传输。
report.json 保存素材帧哈希、依赖版本、每段编码/解码/处理时间均值和 P95、实际负载码率、PSNR、帧数、预算。
CSV 保存逐帧耗时、包数和字节数；PNG 保存开始/中间/结束的最终解码图。
默认处理只画矩形，因此 processing_ms **不是 YOLO 耗时**。此模式的 PSNR 是相对同样画框的原始图。

可选 --model /已有本地模型.pt --sandbox-root /源码目录 启用真正 detector；
模型及其依赖资源必须事先准备，不使用该选项自动下载模型。模型有外部资源需求时不应使用此模式进行严格断网验收。
真实检测会受第一次压缩影响而改变框的位置，此模式不报告不可直接比较的 PSNR。
默认本轮只运行画框模式，未加载模型或设备。

限制：这是串行离线编解码基线，不是实时 20 fps 播放测试。没有 Android 解码/EGL、MASQUE、抖动缓冲、丢包重传或完整流水线调度。
编码器/CPU 已预热程度会影响短样本耗时，不以单次均值挑选最终档位。PSNR 只能辅助比较，不能代替观感。
不对结果承诺真实端到端时延，不将计算出的发送预算当作实测。

## 配置

候选及回退档见 video-profiles.json。默认 native：不施加应用层编码上限或自定义发包限速，无需演示时配置。baseline 仅为历史对照档。

| 进程 | 编码设置 | 发包设置 |
| --- | --- | --- |
| 狗 Agent | DOG_AGENT_VIDEO_BITRATE_KBPS=0 | AGENT_VIDEO_WIRE_BITRATE_BPS=0 |
| Sandbox | VIDEO_BITRATE_BPS=0 | VIDEO_WIRE_BITRATE_BPS=0 |

狗端两个上传入口均读取同一配置。Sandbox 的配置由 sandbox.env 传入容器。
设置应用到新建流/新进程，不会在线重新配置已建立的 sender。正式默认用 native；本次不修改任何运行环境。
编码/发包设置为 0 表示不施加应用限制；显式正数编码范围 250～1500 kbps，发包为正整数。H264 编码器自身最低码率可能高于 250 kbps，实际以协商和编码器行为为准。
不要把码率和 pacing 设成相等而耗尽包头、关键帧和重传余量。

## Sandbox 可观察性

每 5 秒输出 pipeline_metrics：received/processed/dropped 累计计数，以及最近最多 200 帧的 queue_p95_ms、processing_p95_ms。
processing 包含 detector.process 和输出适配，非纯模型 GPU 核函数时间。
DEBUG 级别 video_output 记录结果 revision 与输出间隔。间隔统计在生成输出帧时，不等于眼镜渲染间隔。
未增加队列容量、重复帧或更改 20 fps 上限。SDK 现有编码日志保留。

当前正式默认：Python 上传 bitrate_kbps=0，不安装应用编码限幅、不写 SDP b=AS；两段 pacing 默认 0，不安装自定义发包限速。保留 WebRTC 拥塞控制和编码器固有边界。无需演示时调参；部署时若已有正数环境覆盖，须清理或设为 0。
