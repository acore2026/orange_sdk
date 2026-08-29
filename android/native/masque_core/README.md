# Android MASQUE Native Core

This Go `c-shared` module builds the `libmasque_core.so` libraries packaged by
the Android AAR. The build covers all four Android ABIs: `arm64-v8a`,
`armeabi-v7a`, `x86_64`, and `x86`. The core owns the protected UDP socket,
HTTP/3 CONNECT-IP session, RFC 9484 capsules, TUN packet pumps and persistent
TLS client identity.

```bash
go test ./...
ANDROID_NDK_ROOT=/opt/android-sdk/ndk/27.0.12077973 ./build-android.sh
```

To rebuild only selected ABIs, pass them explicitly. The old ARM64 script is
retained as a compatibility wrapper:

```bash
./build-android.sh arm64-v8a x86_64
./build-android-arm64.sh
```

The default API level is 26, matching the AAR's `minSdk`. Override it with
`ANDROID_API_LEVEL` only when producing a separately qualified build.

`third_party/connect-ip-go` is the MIT-licensed v0.2.0 source with one local,
documented extension: `DialWithHeaders` permits the SDK to include the optional
Authorization header on extended CONNECT. The protocol state machine and wire
format are otherwise unchanged.
