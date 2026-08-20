#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ndk_root="${ANDROID_NDK_ROOT:-/opt/android-sdk/ndk/27.0.12077973}"
toolchain="$ndk_root/toolchains/llvm/prebuilt/linux-x86_64/bin"
output_dir="$script_dir/../../agent-sdk/src/main/jniLibs/arm64-v8a"
build_dir="$(mktemp -d)"
trap 'rm -r -- "$build_dir"' EXIT

cd "$script_dir"
CGO_ENABLED=1 GOOS=android GOARCH=arm64 \
    CC="$toolchain/aarch64-linux-android26-clang" \
    go build -trimpath -buildmode=c-shared \
    -ldflags='-s -w' -o "$build_dir/libmasque_core.so" .
mkdir -p "$output_dir"
cp "$build_dir/libmasque_core.so" "$output_dir/libmasque_core.so"
echo "built $output_dir/libmasque_core.so"
