# Agent SDK implementation

This workspace contains the V4.1 SDK implementation while preserving both
design documents unchanged except for the already-created user-friendly V4.1
document.

- `python/`: Linux Python SDK, real `/dev/net/tun`, pyroute2 routes, aioquic
  CONNECT-IP, local REST endpoints, group cache, tests and runnable examples.
- `android/`: Kotlin Android/RayNeoOS library, `VpnService`, dynamic peer routes,
  group cache, JNI MASQUE boundary, unit tests and an example app.

The address examples are deployment values only. SDK source code obtains all
physical IPs, Agent TUN addresses, ports and MASQUE endpoints from initialization
parameters. A2A target `agent_ip + tcp_port` is resolved exclusively from the
latest accepted `acf_group_config` snapshot.

## Verify

```bash
cd python
python3 -m pip install -e '.[test]'
pytest -q

cd ../android
export ANDROID_HOME=/path/to/android-sdk
./gradlew :agent-sdk:testDebugUnitTest :example-app:assembleDebug
```

For Android device execution, add the ABI-specific `libmasque_core.so` described
in `android/agent-sdk/src/main/cpp/README.md`. Compilation and JVM tests do not
load the native library because transports are injected with test doubles.
