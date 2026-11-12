#!/usr/bin/env python3
"""Attach completed isolated validation evidence and archive a patch bundle."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import tarfile


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def validation_inputs(bundle, harness):
    """Capture inputs before testing, including tests, manifest and installer."""
    files = {}
    for path in sorted(bundle.rglob('*')):
        if '__pycache__' in path.parts:
            continue
        if path.is_symlink():
            raise ValueError('Symlink in bundle: ' + str(path))
        if path.is_file():
            files[str(path.relative_to(bundle))] = sha(path)
    return {'files': files, 'harness': {name: sha(harness/name) for name in
            ('validate-overlay.sh', 'validate-in-chroot.py')}}


def check_validation_inputs(bundle, harness, snapshot, log):
    expected = json.loads(snapshot.read_text())
    if validation_inputs(bundle, harness) != expected:
        raise ValueError('Bundle or harness changed since validation snapshot; rerun validation')
    marker = 'VALIDATION_INPUTS_SHA256 ' + sha(snapshot)
    if marker not in log.splitlines():
        raise ValueError('Validation log is not bound to this input snapshot')
    return expected


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--validation-log',type=Path)
    parser.add_argument('--harness-dir',type=Path,required=True)
    parser.add_argument('--validation-inputs',type=Path,required=True,help='External input snapshot captured before validation')
    parser.add_argument('--snapshot-validation',action='store_true',help='Capture inputs only; print marker to include in the validation log')
    a=parser.parse_args()
    bundle=a.bundle.resolve()
    if a.validation_inputs.resolve().is_relative_to(bundle):
        parser.error('validation-inputs must be outside the bundle')
    if a.snapshot_validation:
        with a.validation_inputs.open('x') as f:
            json.dump(validation_inputs(bundle, a.harness_dir), f, indent=2)
            f.write('\n')
        print('VALIDATION_INPUTS_SHA256 ' + sha(a.validation_inputs))
        return
    if a.validation_log is None:
        parser.error('--validation-log is required when finalizing')
    archive=bundle.with_suffix('.tar.gz')
    if archive.exists():raise SystemExit('Archive already exists: '+str(archive))
    log=a.validation_log.read_text()
    inputs = check_validation_inputs(bundle, a.harness_dir, a.validation_inputs, log)
    counts=[int(n) for n in re.findall(r'^Ran (\d+) tests? in [^\n]+\n\s*\nOK\s*$',log,re.M)]
    if len(counts)!=5 or any(n<minimum for n,minimum in zip(counts,[49,51,10,3,3])) or 'ARM64_OFFLINE_PATCH_VALIDATION_PASSED' not in log:
        raise SystemExit('Expected final validation pass and all five test suites')
    if 'AUTOMATIC_FAILURE_ROLLBACK_PASS' not in log:raise SystemExit('Automatic rollback evidence missing')
    if (bundle/'live-audit.json').exists() and 'PREVIOUS_PATCH_ROLLBACK_TO_NEW_BASE_PASS' not in log:
        raise SystemExit('Previous patch migration evidence missing')
    validation=bundle/'validation'
    shutil.copy2(a.validation_inputs,validation/'inputs.json')
    shutil.copy2(a.validation_log,validation/'arm64-offline.log')
    harness=validation/'harness';harness.mkdir(exist_ok=True)
    for name in ('validate-overlay.sh','validate-in-chroot.py'):
        shutil.copy2(a.harness_dir/name,harness/name)
    evidence={'environment':'QEMU ARM64, vendor Nano rootfs, private mount/network/PID namespaces; loopback only',
              'simulated':['Jetson /proc/device-tree/model','systemctl inactive status (no systemd in chroot)'],
              'suites':dict(zip(('controller','dog_agent','installer','live_studio_overlay','installed_sdk'),counts)),
              'checks':['CLI preflight/install/verify/rollback','offline wheel installation and pip check',
                        'baseline drift rejection','duplicate install rejection','identity and network preservation',
                        'post-install failure automatic rollback','Agent dry run without device commands'],
              'hardware_acceptance':'Pending on the destination Nano; no robot audio upload, playback, motion, camera or core-network integration executed',
              'previous_patch_rollback_then_new_install':'passed' if 'PREVIOUS_PATCH_ROLLBACK_TO_NEW_BASE_PASS' in log else 'not tested',
              'tested_files':inputs['files'],
              'input_snapshot_sha256':sha(a.validation_inputs),
              'validation_log_sha256':sha(a.validation_log)}
    (validation/'results.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
    (validation/'README.md').write_text('''# 离线验证记录

最终运行记录见 `arm64-offline.log`，机器可读结果见 `results.json`。

在旧 Nano 原厂镜像的 ARM64 根文件系统上，以 QEMU 和独立 mount/network/PID namespace 验证；只有 loopback 网络可用。
原根文件系统作为只读 overlay 下层，恢复旧发布包应用和 SDK 后执行真实更新器 CLI：check → install → verify → rollback。
Jetson 设备树型号和 systemctl 的停止状态由测试夹具模拟，其他系统版本、安装标记、文件、依赖检查使用隔离环境的真实数据。

- @TEST_SUMMARY@
- 完全离线安装语音 wheels 与新版 SDK，依赖检查和模块导入通过；Agent dry-run 通过。
- 本地文件被修改、包校验损坏、重复安装、未确认停止服务等拒绝路径通过。
- 正常回滚通过；安装后验证阶段注入失败，自动回滚通过；文件、SDK、原配置和独立身份标记恢复/保留。
@MIGRATION_RESULT@

测试中的预期异常会打印日志，最终结果以各 unittest 的 OK 和末尾 ARM64_OFFLINE_PATCH_VALIDATION_PASSED 为准。
测试没有上传/播放狗体音频，也没有执行真实运动。目标 Nano 的扬声器、相机、核心网、Studio 服务器和运动效果仍需现场验收。

`harness/` 是构建机验证过程存档，路径指向构建机的原始镜像和工作目录；不要在机器狗上执行这些脚本。
新狗只需按包根目录 README.md 操作。测试代码分别位于 installer-tests、overlay-tests、sdk-tests；应用测试随 payload 安装。
'''.replace('@TEST_SUMMARY@','控制端 %s 项、Agent %s 项、安装器 %s 项、现场 Studio 增量 %s 项、已安装 SDK 自适应策略 %s 项，共 %s 项通过。' % (*counts,sum(counts)))
        .replace('@MIGRATION_RESULT@','- 20261110 旧补丁安装 → 拒绝直接叠加新版 → 用旧包回滚 → 新版安装的迁移顺序通过。' if 'PREVIOUS_PATCH_ROLLBACK_TO_NEW_BASE_PASS' in log else ''))
    source=bundle/'sources/release-tools';source.mkdir(exist_ok=True)
    for name in ('build_patch.py','finalize_patch.py'):
        shutil.copy2(Path(__file__).parent/name,source/name)
    manifest=json.loads((bundle/'manifest.json').read_text())
    manifest['validation']={'report':'validation/results.json','automated_tests':sum(counts),'offline_install_and_rollback':'passed in isolated ARM64 vendor rootfs','target_hardware_acceptance':'pending'}
    (bundle/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    checks={str(p.relative_to(bundle)):sha(p) for p in sorted(bundle.rglob('*')) if p.is_file() and p.name!='checksums.json' and '__pycache__' not in p.parts}
    (bundle/'checksums.json').write_text(json.dumps(checks,indent=2)+'\n')
    with tarfile.open(archive,'w:gz') as t:
        def include(info):
            if '__pycache__' in Path(info.name).parts:return None
            info.uid=info.gid=0;info.uname=info.gname='root'
            return info
        t.add(bundle,arcname=bundle.name,filter=include)
    archive.with_name(archive.name+'.sha256').write_text(sha(archive)+'  '+archive.name+'\n')
    print(archive)
    print('bytes',archive.stat().st_size)


if __name__=='__main__':main()
