# Orange demo source bundle

This release is assembled from **committed source**, never a patch to an installed
`site-packages` tree. `build_demo_bundle.py` rejects uncommitted release changes and
records the four Git commit IDs, source archive hashes, bundled wheel/APK hashes,
and the fixed Unitree SDK commit in `release-manifest.json`.

The two lock files are the complete non-local Python distributions observed in
the running robot's separate CPython 3.12.13/aarch64 environments on 2026-10-11.
The SDK wheel comes from this repository, and Unitree SDK source is vendored at
commit `794fb2b3fd9165fd245a7b568698d9e97d8ac0a0`. The native CycloneDDS
0.10.5 library, ROS Foxy installation, camera driver, JetPack and robot firmware
remain platform prerequisites; no x86 wheelhouse is falsely presented as an
aarch64 offline package. The installer needs package-index access for the locked
Python dependencies unless a matching aarch64 wheelhouse is supplied to pip.

Build after all four repositories are committed and tested:

```bash
cd /home/huawei/orange/sdk
python3 tools/release/build_demo_bundle.py /home/huawei/orange/releases/orange-demo.tar.gz \
  --wheel python/dist/agent_connect_sdk-0.17.10-py3-none-any.whl \
  --rayneo-apk android/example-app/build/outputs/apk/rayneo/debug/example-app-rayneo-debug.apk \
  --phone-apk android/phone-controller/build/outputs/apk/debug/phone-controller-debug.apk
```

If GitHub is intermittently unavailable, download the pinned Unitree source
archive once and pass `--unitree-archive PATH`. Its SHA256 is checked before it
is included, so repeated builds do not depend on the network response.

The archive contains the four committed source trees, pinned Python dependency
lists, configuration templates and source-built SDK wheel/Android APKs. On a
new Go2 Jetson, install the wheel from `artifacts/`, install each service's
locked requirements into separate CPython 3.12.13/aarch64 environments, and
install the bundled Unitree source at the recorded commit. Keep the vendor
`dog-init.service` as the sole startup path and point `dog_init.json` at the
installed `dog/start_dog_services.sh`. Review `core.env.example`,
`dog-init.service.example`, `dog_init.json.example` and
`video-diagnostics.conf` for the new machine. `ros-logging.conf` sets a
writable ROS log directory for nav within the original `dog-init.service`.
The archived `watch.py` is the
current video-capture diagnostic source. Do not copy another robot's
`/root/.local/state/agent-sdk` identity or private keys.

The vendor `dog_init.sh` remains on the robot. The current site uses it as the
single systemd startup path, with `dog_services_run_user` empty. The new
machine's TUN, camera, DDS and log permissions must be checked locally.
`/dev/video4` and `eth0` are site observations, not universal defaults.
