#!/usr/bin/env python3
"""Offline cumulative updater. Never starts services or sends robot commands."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time

APP = Path('/home/aicor/compute_network/sandbox-demo')
STATE = Path('/var/lib/orange-patch')
BACKUPS = Path('/var/backups/orange-patch')
CONFIG = Path('/etc/orange-demo/core.env')
DOG_PY = APP/'dog/.venv/bin/python'
CTRL_PY = APP/'dog-ctrl/.venv/bin/python'


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for data in iter(lambda:f.read(1024*1024),b''): h.update(data)
    return h.hexdigest()


def run(args, **kwargs):
    print('+', ' '.join(map(str,args)),flush=True)
    return subprocess.run(list(map(str,args)),check=True,**kwargs)


def safe_relative(name):
    p=Path(name)
    if p.is_absolute() or '..' in p.parts or not p.parts: raise ValueError('Unsafe path: '+name)
    return p


def destination(root, name):
    target=root/safe_relative(name)
    for p in (target,*target.parents):
        if p==root: break
        if p.is_symlink(): raise RuntimeError('Refuse symlink destination: '+str(p))
    return target


def verify_bundle(bundle):
    checks=json.loads((bundle/'checksums.json').read_text())
    for name, expected in checks.items():
        p=destination(bundle,name)
        if not p.is_file() or digest(p)!=expected: raise RuntimeError('Package checksum mismatch: '+name)
    actual={str(p.relative_to(bundle)) for p in bundle.rglob('*') if p.is_file() and p.name!='checksums.json' and '__pycache__' not in p.parts}
    if actual != set(checks): raise RuntimeError('Package contains missing or unlisted files')
    m=json.loads((bundle/'manifest.json').read_text())
    for entry in m['files']:
        if digest(bundle/'payload'/safe_relative(entry['path'])) != entry['after']: raise RuntimeError('Payload manifest mismatch')
    return m


def verify_base_files(root, manifest):
    errors=[]
    for entry in manifest['files']:
        target=destination(root,entry['path'])
        if entry['before'] is None:
            if target.exists(): errors.append('New destination already exists: '+entry['path'])
        elif not target.is_file() or digest(target)!=entry['before']:
            errors.append('Baseline differs: '+entry['path'])
    if errors: raise RuntimeError('\n'.join(errors))


def merged_env(text, values):
    keys='|'.join(re.escape(k) for k in values)
    # Only edit our keys. Never execute/source the existing file as root.
    text=re.sub(r'^[ \t]*(?:export[ \t]+)?(?:'+keys+r')[ \t]*=.*(?:\n|$)','',text,flags=re.M)
    return text.rstrip('\n')+'\n'+''.join(k+'='+v+'\n' for k,v in values.items())


def snapshot(root, backup, names):
    entries=[]
    (backup/'files').mkdir(parents=True)
    for name in names:
        target=destination(root,name)
        entry={'path':name,'exists':target.exists()}
        if target.exists():
            if not target.is_file(): raise RuntimeError('Not a regular file: '+name)
            st=target.stat()
            entry.update(mode=st.st_mode & 0o777,uid=st.st_uid,gid=st.st_gid,sha256=digest(target))
            copy=backup/'files'/name;copy.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,copy)
        entries.append(entry)
    (backup/'files.json').write_text(json.dumps(entries,indent=2)+'\n')
    return entries


def atomic_write(path, data, mode=0o644, uid=None, gid=None):
    tmp=path.with_name(path.name+'.orange-patch-'+str(os.getpid()))
    # Exclusive creation prevents following an existing temporary symlink.
    with tmp.open('xb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
    try:
        tmp.chmod(mode)
        if uid is not None: os.chown(tmp,uid,gid)
        os.replace(tmp,path)
    finally:
        if tmp.exists(): tmp.unlink()


def restore_files(root, backup):
    entries=json.loads((backup/'files.json').read_text())
    for entry in entries:
        target=destination(root,entry['path'])
        if entry['exists']:
            source=backup/'files'/entry['path']
            if digest(source)!=entry['sha256']: raise RuntimeError('Backup damaged: '+entry['path'])
            atomic_write(target,source.read_bytes(),entry['mode'],entry['uid'],entry['gid'])
        elif target.exists():
            target.unlink()


def service_stopped():
    active=subprocess.run(['systemctl','is-active','dog-init.service'],text=True,capture_output=True)
    if active.stdout.strip() not in ('inactive','failed'):
        raise RuntimeError('dog-init must be confirmed inactive/failed before updating; status='+active.stdout.strip())
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            cmd=(proc/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
            cwd=os.readlink(proc/'cwd')
        except (OSError,PermissionError): continue
        if int(proc.name)==os.getpid(): continue
        if ('orange_dog.py' in cmd or 'ros2 launch nav' in cmd or
            ('main.py' in cmd and cwd==str(APP/'dog-ctrl')) or
            (str(APP) in cmd and 'start_dog_services.sh' in cmd)):
            raise RuntimeError('Application still running: PID '+proc.name)


def environment_check(manifest):
    if os.geteuid()!=0: raise RuntimeError('Run with sudo on the target Nano')
    if platform.machine()!='aarch64': raise RuntimeError('Requires aarch64')
    model=Path('/proc/device-tree/model').read_text().replace('\0','').lower()
    if 'orin' not in model or 'nano' not in model: raise RuntimeError('Requires Orin Nano')
    if 'VERSION_ID="20.04"' not in Path('/etc/os-release').read_text(): raise RuntimeError('Requires Ubuntu 20.04')
    if 'R35 (release), REVISION: 3.1' not in Path('/etc/nv_tegra_release').read_text(): raise RuntimeError('Requires L4T R35.3.1')
    installed=json.loads(Path('/var/lib/orange-offline/installed.json').read_text())
    if installed.get('release')!=manifest['base_release']: raise RuntimeError('Install the exact 20261013-current base first')
    if installed.get('current_files_sha256')!=json.loads((BUNDLE/'baseline/manifest.json').read_text()).get('current_files_sha256'):
        raise RuntimeError('Base installation marker does not match this patch')
    if shutil.disk_usage('/').free<2*1024**3: raise RuntimeError('Need at least 2 GiB free space')
    for p in (DOG_PY,CTRL_PY,Path('/usr/bin/python3.9')):
        if not p.exists(): raise RuntimeError('Missing runtime: '+str(p))
    if not CONFIG.is_file(): raise RuntimeError('Missing core.env; keep target-specific base configuration')


def sdk_check(manifest, which):
    script='''import hashlib,importlib.metadata as m,json,pathlib,sys
expected=json.loads(sys.stdin.read())
assert m.version('agent-connect-sdk')==expected['version'], 'SDK version mismatch'
base=pathlib.Path(m.distribution('agent-connect-sdk').locate_file(''))
for n,h in expected['files'].items():
 p=base/n
 assert p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==h, 'SDK source drift: '+n
print('SDK source and metadata verified')
'''
    run([DOG_PY,'-c',script],input=json.dumps({'version':manifest['sdk'][which],'files':manifest['sdk'][which+'_files']}),text=True)


def install_sdk(bundle, wheel):
    run([DOG_PY,'-m','pip','install','--no-index','--no-deps','--force-reinstall',bundle/wheel],env=offline_env())


def offline_env():
    return dict(os.environ,PIP_NO_INDEX='1',PIP_DISABLE_PIP_VERSION_CHECK='1',PYTHONDONTWRITEBYTECODE='1',CYCLONEDDS_HOME='/home/unitree/cyclonedds/install')


def verify_runtime(manifest):
    for py in (DOG_PY,CTRL_PY):
        run([py,'-m','pip','check'],env=offline_env())
    run([DOG_PY,'-c','import agent_sdk,aiortc,av,aiohttp,cyclonedds; print("Dog imports OK")'],env=offline_env())
    run([CTRL_PY,'-c','import aiohttp,cyclonedds; from unitree_sdk2py.go2.sport.sport_client import SportClient; print("Control imports OK")'],env=offline_env())
    voice=Path(manifest['voice_dir'])
    run([voice/'venv/bin/python','-m','pip','check'],env=offline_env())
    run([voice/'venv/bin/python',voice/'go2_audio.py','--help'],env=offline_env())
    run([DOG_PY,APP/'dog/orange_dog.py','--help'],env=offline_env())
    run([CTRL_PY,APP/'dog-ctrl/main.py','--help'],env=offline_env())
    sdk_check(manifest,'after')


def install(bundle, manifest):
    root=Path('/')
    verify_base_files(root,manifest)
    voice=Path(manifest['voice_dir'])
    if voice.exists(): raise RuntimeError('Voice runtime already exists; use rollback or investigate')
    sdk_check(manifest,'before')
    backup=BACKUPS/(time.strftime('%Y%m%d-%H%M%S')+'-'+str(os.getpid()))
    backup.mkdir(parents=True,mode=0o700)
    names=[e['path'] for e in manifest['files']]+[str(CONFIG).lstrip('/')]
    entries=snapshot(root,backup,names)
    by_name={e['path']:e for e in entries}
    shutil.copy2(bundle/'baseline/agent_connect_sdk-0.17.11-py3-none-any.whl',backup/'old-sdk.whl')
    state={'release':manifest['release'],'phase':'preparing','backup':str(backup),'voice_dir':str(voice),'sdk_modified':False}
    STATE.mkdir(parents=True,exist_ok=True)
    marker=STATE/(manifest['release']+'.json')
    def save(): atomic_write(marker,(json.dumps(state,indent=2)+'\n').encode(),0o600)
    save()
    try:
        for entry in manifest['files']:
            target=destination(root,entry['path']);target.parent.mkdir(parents=True,exist_ok=True)
            previous=by_name[entry['path']]
            atomic_write(target,(bundle/'payload'/entry['path']).read_bytes(),entry['mode'],previous.get('uid',0),previous.get('gid',0))
        # The vendor base has Python 3.9 but may lack ensurepip: bootstrap from the bundled pip wheel.
        run(['/usr/bin/python3.9','-m','venv','--without-pip',voice/'venv'],env=offline_env())
        pip_wheel=next((bundle/'voice-wheelhouse').glob('pip-*.whl'))
        env=dict(offline_env(),PYTHONPATH=str(pip_wheel))
        run([voice/'venv/bin/python','-m','pip','install','--no-index','--find-links',bundle/'voice-wheelhouse','pip==25.0.1','-r',voice/'requirements.txt'],env=env)
        state['sdk_modified']=True;save()
        install_sdk(bundle,Path('wheelhouse')/manifest['sdk']['wheel'])
        st=CONFIG.stat()
        # Until bound to this robot, fail before executing Content (player precheck below).
        updated=merged_env(CONFIG.read_text(),{'DOG_WARNING_DIR':str(voice),'DOG_WARNING_AUDIO_ID':'REPLACE_WITH_DEVICE_AUDIO_ID'})
        atomic_write(CONFIG,updated.encode(),st.st_mode&0o777,st.st_uid,st.st_gid)
        verify_runtime(manifest)
        state['phase']='installed_audio_unbound';save()
        print('Installed. Services remain stopped. Run setup_audio.py to upload/bind audio before startup.')
        print('BACKUP',backup)
    except BaseException:
        state['phase']='failed';save()
        try:
            rollback(bundle,manifest,state)
        except Exception as error:
            print('Automatic rollback failed:',error,file=sys.stderr)
        raise


def rollback(bundle,manifest,state):
    backup=Path(state['backup'])
    if backup.parent!=BACKUPS or not (backup/'files.json').is_file(): raise RuntimeError('Invalid backup path')
    restore_files(Path('/'),backup)
    if state.get('sdk_modified'):
        install_sdk(bundle,Path('baseline/agent_connect_sdk-0.17.11-py3-none-any.whl'))
    voice=Path(state['voice_dir'])
    if str(voice)!=manifest['voice_dir'] or voice.is_symlink(): raise RuntimeError('Invalid voice path')
    if voice.exists(): shutil.rmtree(voice)
    state['phase']='rolled_back'
    atomic_write(STATE/(manifest['release']+'.json'),(json.dumps(state,indent=2)+'\n').encode(),0o600)
    print('Rolled back. Services remain stopped. Identity and target network configuration preserved.')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['check','install','verify','rollback'])
    args=p.parse_args()
    manifest=verify_bundle(BUNDLE)
    environment_check(manifest)
    STATE.mkdir(parents=True,exist_ok=True)
    with (STATE/'update.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        marker=STATE/(manifest['release']+'.json')
        state=json.loads(marker.read_text()) if marker.exists() else None
        if args.command=='verify':
            if not state or state['phase'] not in ('installed_audio_unbound','ready'): raise RuntimeError('Patch not installed')
            for e in manifest['files']:
                if digest(destination(Path('/'),e['path']))!=e['after']: raise RuntimeError('Installed file drift: '+e['path'])
            verify_runtime(manifest)
            print('Verified; audio state:',state['phase']);return
        service_stopped()
        if args.command=='rollback':
            if not state or state['phase']=='rolled_back': raise RuntimeError('No active patch to rollback')
            rollback(BUNDLE,manifest,state);return
        if state and state['phase']!='rolled_back':
            raise RuntimeError('Patch state '+state['phase']+'; use verify or rollback, not reinstall')
        verify_base_files(Path('/'),manifest);sdk_check(manifest,'before')
        if args.command=='check': print('Preflight passed. No robot actions or network requests made.');return
        install(BUNDLE,manifest)


BUNDLE=Path(__file__).resolve().parent
if __name__=='__main__':
    try: main()
    except Exception as exc:
        print('ERROR:',exc,file=sys.stderr);sys.exit(1)
