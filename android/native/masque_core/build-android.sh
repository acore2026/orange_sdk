#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ndk_root="${ANDROID_NDK_ROOT:-/opt/android-sdk/ndk/27.0.12077973}"
host_tag="${ANDROID_NDK_HOST_TAG:-linux-x86_64}"
toolchain="$ndk_root/toolchains/llvm/prebuilt/$host_tag/bin"
output_root="$script_dir/../../agent-sdk/src/main/jniLibs"
android_api="${ANDROID_API_LEVEL:-26}"
build_dir="$(mktemp -d)"
trap 'rm -r -- "$build_dir"' EXIT

all_abis=(arm64-v8a armeabi-v7a x86_64 x86)
if (( $# > 0 )); then
    requested_abis=("$@")
else
    requested_abis=("${all_abis[@]}")
fi

build_abi() {
    local abi="$1"
    local goarch cc goarm="" go386=""
    case "$abi" in
        arm64-v8a)
            goarch="arm64"
            cc="$toolchain/aarch64-linux-android${android_api}-clang"
            ;;
        armeabi-v7a)
            goarch="arm"
            goarm="7"
            cc="$toolchain/armv7a-linux-androideabi${android_api}-clang"
            ;;
        x86_64)
            goarch="amd64"
            cc="$toolchain/x86_64-linux-android${android_api}-clang"
            ;;
        x86)
            goarch="386"
            go386="sse2"
            cc="$toolchain/i686-linux-android${android_api}-clang"
            ;;
        *)
            echo "unsupported Android ABI: $abi" >&2
            echo "supported ABIs: ${all_abis[*]}" >&2
            return 2
            ;;
    esac
    if [[ ! -x "$cc" ]]; then
        echo "Android NDK compiler not found: $cc" >&2
        return 1
    fi

    local abi_build_dir="$build_dir/$abi"
    local output_dir="$output_root/$abi"
    mkdir -p "$abi_build_dir" "$output_dir"
    (
        cd "$script_dir"
        CGO_ENABLED=1 \
        GOOS=android \
        GOARCH="$goarch" \
        GOARM="$goarm" \
        GO386="$go386" \
        CC="$cc" \
        go build -trimpath -buildmode=c-shared \
            -ldflags='-s -w -buildid=' \
            -o "$abi_build_dir/libmasque_core.so" .
    )
    cp "$abi_build_dir/libmasque_core.so" "$output_dir/libmasque_core.so"
    echo "built $abi: $output_dir/libmasque_core.so"
}

for abi in "${requested_abis[@]}"; do
    build_abi "$abi"
done
