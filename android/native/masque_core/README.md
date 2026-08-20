# Android MASQUE Native Core

This Go `c-shared` module builds the ARM64 `libmasque_core.so` packaged by the
Android AAR. It owns the protected UDP socket, HTTP/3 CONNECT-IP session,
RFC 9484 capsules, TUN packet pumps and persistent TLS client identity.

```bash
go test ./...
ANDROID_NDK_ROOT=/opt/android-sdk/ndk/27.0.12077973 ./build-android-arm64.sh
```

`third_party/connect-ip-go` is the MIT-licensed v0.2.0 source with one local,
documented extension: `DialWithHeaders` permits the SDK to include the optional
Authorization header on extended CONNECT. The protocol state machine and wire
format are otherwise unchanged.
