#!/usr/bin/env python3
"""Build a cumulative application patch for the exact 20261013 Nano bundle."""
import argparse
import difflib
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

BASE_RELEASE = 'orange-nano-offline-20261013-current'
APP = 'home/aicor/compute_network/sandbox-demo'
BASE_DIGEST = '44de185939b608f5bde7fe40c52cd4cc91bc6e724430841335ad40f93221b0e4'

def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()

def git(repo, *args):
    return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()

def snapshot_repository(repo, destination):
    """Export only committed regular files; reject tracked worktree/index drift."""
    head = git(repo, 'rev-parse', 'HEAD')
    if git(repo, 'status', '--porcelain', '--untracked-files=no'):
        raise ValueError('Tracked changes must be committed before packaging: ' + str(repo))
    entries = subprocess.check_output(['git', '-C', str(repo), 'ls-tree', '-rz', head])
    files = {}
    for entry in entries.split(b'\0'):
        if not entry:
            continue
        meta, raw_name = entry.split(b'\t', 1)
        mode, kind, oid = meta.decode().split()
        name = raw_name.decode()
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise ValueError('Only regular tracked files may be packaged: ' + name)
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Unsafe tracked path: ' + name)
        source = repo / relative
        if source.is_symlink() or any(p.is_symlink() for p in source.parents if p != repo and repo in p.parents):
            raise ValueError('Symlink in source path: ' + name)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(['git', '-C', str(repo), 'cat-file', 'blob', oid]))
        target.chmod(int(mode[-3:], 8))
        files[name] = target
    return files, {'head': head, 'branch': git(repo, 'branch', '--show-current'),
                   'status': 'clean tracked files; untracked files excluded'}


def select_live_dog(committed, live, reason):
    live = committed if live is None else live
    if live.is_symlink() or not live.is_file():
        raise ValueError('live-dog must be a regular file')
    changed = live.read_bytes() != committed.read_bytes()
    if changed and not (reason and reason.strip()):
        raise ValueError('A differing live-dog requires --overlay-reason')
    return live, {'file': 'dog/orange_dog.py', 'sha256': sha(live),
                  'matches_repository_head': not changed,
                  'description': reason.strip() if changed else 'Exact committed dog source; no live overlay'}


def verify_sdk_wheel(repo, wheel):
    head = git(repo, 'rev-parse', 'HEAD')
    paths = git(repo, 'ls-tree', '-r', '--name-only', head, '--', 'python/src/agent_sdk').splitlines()
    expected = {p.removeprefix('python/src/'): subprocess.check_output(
        ['git', '-C', str(repo), 'show', head+':'+p]) for p in paths}
    with zipfile.ZipFile(wheel) as archive:
        actual = {p: archive.read(p) for p in archive.namelist()
                  if p.startswith('agent_sdk/') and not p.endswith('/')}
        metadata = [p for p in archive.namelist() if p.endswith('.dist-info/METADATA')]
        if len(metadata) != 1 or '\nVersion: 0.17.11+orange.20261110\n' not in archive.read(metadata[0]).decode():
            raise ValueError('SDK wheel must have version 0.17.11+orange.20261110')
    if not expected or actual != expected:
        raise ValueError('SDK wheel modules do not match repository HEAD')
    return head

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--orange-root',type=Path,required=True)
    p.add_argument('--base',type=Path,required=True)
    p.add_argument('--live-dog',type=Path,help='Optional exported dog source; defaults to repository HEAD')
    p.add_argument('--overlay-reason',help='Required provenance explanation when live-dog differs from HEAD')
    p.add_argument('--sdk-wheel',type=Path,required=True)
    p.add_argument('--voice-wheels',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--release-date',default='20261111',help='YYYYMMDD cumulative release date')
    a=p.parse_args()
    if len(a.release_date)!=8 or not a.release_date.isdecimal():p.error('release-date must be YYYYMMDD')
    release='orange-nano-patch-'+a.release_date
    voice='opt/orange-runtime/voice-'+a.release_date
    if sha(a.base)!=BASE_DIGEST: raise SystemExit('Wrong base archive')
    sdk_head = verify_sdk_wheel(a.orange_root/'sdk', a.sdk_wheel)
    # Snapshot before creating output; untracked files (including local config) stay out.
    with tempfile.TemporaryDirectory(prefix='orange-patch-sources-') as tmp:
        snapshots = {}
        repos = {}
        for name in ('dog', 'dog-ctrl'):
            snapshots[name], repos[name] = snapshot_repository(
                a.orange_root/'compute_backend'/name, Path(tmp)/name)
        live, overlay = select_live_dog(snapshots['dog']['orange_dog.py'], a.live_dog, a.overlay_reason)
        live_snapshot = Path(tmp)/'live-dog.py'
        shutil.copy2(live, live_snapshot)
        if sha(live_snapshot) != overlay['sha256']:
            raise ValueError('live-dog changed while being captured')
        build(a, release, voice, snapshots, repos, live_snapshot, overlay, sdk_head)


def build(a, release, voice, snapshots, repos, live, overlay, sdk_head):
    a.output.mkdir(parents=True,exist_ok=False)
    out=a.output
    with tarfile.open(a.base) as archive:
        def read(name): return archive.extractfile(BASE_RELEASE+'/'+name).read()
        baseline=json.loads(read('manifest.json'))
        payload_bytes=read('payload.tar.gz')
        old_wheel=read('wheelhouse/agent_connect_sdk-0.17.11-py3-none-any.whl')
    with tarfile.open(fileobj=io.BytesIO(payload_bytes)) as payload:
        old={m.name:payload.extractfile(m).read() for m in payload if m.isfile() and m.name.startswith(APP+'/')}
    (out/'baseline').mkdir()
    (out/'baseline/manifest.json').write_text(json.dumps(baseline,indent=2)+'\n')
    (out/'baseline/agent_connect_sdk-0.17.11-py3-none-any.whl').write_bytes(old_wheel)
    candidates={}
    for name in ('dog','dog-ctrl'):
        for name_in_repo, src in snapshots[name].items():
            candidates[APP+'/'+name+'/'+name_in_repo]=src
    candidates[APP+'/dog/orange_dog.py']=live
    for name in ('go2_audio.py','play_warning.sh','requirements.txt'):
        candidates[voice+'/'+name]=snapshots['dog-ctrl']['voice/'+name]
    candidates[voice+'/warning.wav']=snapshots['dog-ctrl']['voice/assets/warning_dualtone_900_1400_loud.wav']
    files=[]
    for dest, src in sorted(candidates.items()):
        raw=src.read_bytes()
        if old.get(dest)==raw: continue
        path=out/'payload'/dest;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(raw)
        mode=0o755 if src.stat().st_mode & 0o111 else 0o644
        path.chmod(mode)
        files.append({'path':dest,'before':hashlib.sha256(old[dest]).hexdigest() if dest in old else None,'after':sha(path),'mode':mode})
    sdkrepo=a.orange_root/'sdk'
    repos['sdk']={'head':sdk_head,'branch':git(sdkrepo,'branch','--show-current'),'python_changes':git(sdkrepo,'diff','--stat','bed37bce',sdk_head,'--','python')}
    (out/'wheelhouse').mkdir()
    shutil.copy2(a.sdk_wheel,out/'wheelhouse'/a.sdk_wheel.name)
    (out/'voice-wheelhouse').mkdir()
    for src in a.voice_wheels.glob('*.whl'): shutil.copy2(src,out/'voice-wheelhouse'/src.name)
    def wheel_hashes(raw):
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            return {n:hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist() if n.startswith('agent_sdk/') and not n.endswith('/')}
    # Preserve Python SDK source and its tests so the packaged wheel is reviewable.
    raw=subprocess.check_output(['git','-C',str(sdkrepo),'archive',sdk_head,'python'])
    (out/'sources').mkdir()
    committed_dog=snapshots['dog']['orange_dog.py'].read_text()
    (out/'sources/live-dog-overlay.patch').write_text(''.join(difflib.unified_diff(
        committed_dog.splitlines(keepends=True),live.read_text().splitlines(keepends=True),
        fromfile='repository-HEAD/dog/orange_dog.py',tofile='live-overlay/dog/orange_dog.py')))
    with tarfile.open(fileobj=io.BytesIO(raw)) as t:
        for m in t:
            if m.isfile():
                target=out/'sources/sdk'/m.name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(t.extractfile(m).read())
    cfg=out/'sources/sdk/python/setup.cfg'
    cfg.write_text(cfg.read_text().replace('version = 0.17.11','version = 0.17.11+orange.20261110'))
    # Base package may have extra files compared with git; do not infer deletions.
    manifest={'format':1,'release':release,'base_release':BASE_RELEASE,'base_archive_sha256':BASE_DIGEST,
              'target':{'architecture':'aarch64','ubuntu':'20.04','l4t':'R35.3.1','model':'Orin Nano'},
              'repositories':repos,'files':files,'voice_dir':'/'+voice,
              'sdk':{'before':'0.17.11','after':'0.17.11+orange.20261110','wheel':a.sdk_wheel.name,
                     'before_files':wheel_hashes(old_wheel),'after_files':wheel_hashes(a.sdk_wheel.read_bytes())},
              'live_overlay':overlay,
              'preserve':['Agent identity and private keys','core.env existing values except DOG_WARNING_*','runtime_config.json','camera/interface/dog name','ROS/nav/native libraries','systemd units and startup scripts'],
              'external_components':{'android':'SDK Release2.0 '+repos['sdk']['head'][:7]+'; update separately','sandbox':'pruned_sandbox/orange_sandbox d90c9b6 application baseline; update separately','studio':'free6gc/RELEASE2.0 a12c0ec and later checkpoint/Warning/Live commits; deploy separately on port 7865'},
              'validation':{'status':'new bundle; isolated ARM64 validation and target hardware acceptance pending'}}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n')
    template=Path(__file__).parent
    for name in ('update.py','setup_audio.py','README.md','CHANGELOG.md'):
        shutil.copy2(template/name,out/name)
    for name in ('README.md','CHANGELOG.md'):
        path=out/name
        path.write_text(path.read_text().replace('@RELEASE_DATE@',a.release_date))
    for category,name in [('installer','test_update.py'),('overlay','test_studio_overlay.py'),('sdk','test_sdk_adaptive.py')]:
        directory=out/'validation'/(category+'-tests');directory.mkdir(parents=True,exist_ok=True)
        shutil.copy2(template/'tests'/name,directory/name)
    retained=sorted(set(old)-set(candidates))
    (out/'baseline-to-current.json').write_text(json.dumps({'source_commits':baseline['source_release']['source_commits'],'target':repos,'files':files,
        'baseline_only_retained':retained,'unchanged_compared_files':sum(old.get(dest)==src.read_bytes() for dest,src in candidates.items())},indent=2,ensure_ascii=False)+'\n')
    checksum={str(x.relative_to(out)):sha(x) for x in sorted(out.rglob('*')) if x.is_file() and x.name!='checksums.json'}
    (out/'checksums.json').write_text(json.dumps(checksum,indent=2)+'\n')
    print(out)

if __name__=='__main__':main()
