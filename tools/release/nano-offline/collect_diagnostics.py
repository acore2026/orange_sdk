#!/usr/bin/env python3
"""Offline, bounded diagnostic snapshot; no service changes or motion commands."""
import argparse, datetime, hashlib, json, os, pathlib, re, subprocess, tarfile, tempfile, time

MAX_FILE = 4 * 1024 * 1024
MAX_TOTAL = 160 * 1024 * 1024

def redact(text):
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[PRIVATE KEY REDACTED]', text, flags=re.S)
    text = re.sub(r'(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)((?:[\w-]*(?:password|passwd|token|secret|private_key|cookie)[\w-]*)["\x27]?\s*[:=]\s*)[^\s,;]+', r'\1[REDACTED]', text)
    return text

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', default='diagnostics')
    args = parser.parse_args()
    os.umask(0o077)
    output = pathlib.Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ') + '-' + str(os.getpid())
    name = 'orange-diagnostics-' + stamp
    directory = pathlib.Path(tempfile.mkdtemp(prefix=name+'-', dir=str(output)))
    records, total = [], 0

    def save(relative, data):
        nonlocal total
        if total >= MAX_TOTAL:
            records.append({'path':relative, 'skipped':'total size limit'})
            return
        raw = data.encode('utf-8') if isinstance(data, str) else data
        truncated = len(raw) > MAX_FILE
        raw = raw[-min(MAX_FILE, MAX_TOTAL-total):]
        text = redact(raw.decode('utf-8', errors='replace'))
        if truncated: text = '[TRUNCATED: last 4 MiB]\n' + text
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        total += path.stat().st_size
        records.append({'path':relative, 'bytes':path.stat().st_size, 'truncated':truncated})

    def file(path):
        path = pathlib.Path(path)
        try:
            if path.is_symlink() or not path.is_file(): return
            if path.suffix in ('.core','.pem','.key','.gz','.xz','.bz2'): return
            with path.open('rb') as stream:
                stream.seek(max(0, path.stat().st_size-MAX_FILE))
                data=stream.read(MAX_FILE)
            if path.stat().st_size > MAX_FILE: data=b'[SOURCE TRUNCATED]\n'+data
            save('files/'+str(path).lstrip('/'),data)
        except OSError as exc: records.append({'source':str(path), 'error':str(exc)})

    def command(name, argv, timeout=15):
        # Spool stdout directly to disk; bound each command by timeout and tail size.
        start = time.monotonic()
        try:
            with tempfile.TemporaryFile() as stream:
                try:
                    proc = subprocess.run(argv,stdout=stream,stderr=subprocess.STDOUT,timeout=timeout,
                        env=dict(os.environ,LC_ALL='C',SYSTEMD_PAGER='cat',PYTHONDONTWRITEBYTECODE='1'))
                    rc = proc.returncode
                except subprocess.TimeoutExpired: rc='timeout'
                stream.seek(0,2); length=stream.tell(); stream.seek(max(0,length-MAX_FILE))
                data=stream.read(MAX_FILE)
            save('commands/'+name+'.txt', 'command='+repr(argv)+'\nexit='+str(rc)+'\nelapsed='+str(round(time.monotonic()-start,2))+'\n'+ ('[OUTPUT TRUNCATED]\n' if length>MAX_FILE else '')+data.decode(errors='replace'))
        except OSError as exc: save('commands/'+name+'.txt',str(exc))

    commands = [
        ('uname',['uname','-a']),('disk',['df','-hT']),('memory',['free','-h']),
        ('uptime',['uptime']),('time',['timedatectl']),
        ('packages',['dpkg-query','-W','-f=${Package}\t${Version}\t${Architecture}\t${db:Status-Status}\n']),
        ('failed-units',['systemctl','--no-pager','--failed']),
        ('dog-unit',['systemctl','cat','dog-init.service']),
        ('dog-status',['systemctl','status','dog-init.service','--no-pager','-l']),
        ('dog-result',['systemctl','show','dog-init.service','-p','MainPID','-p','Result','-p','ExecMainStatus','-p','NeedDaemonReload','-p','NRestarts']),
        ('journal',['journalctl','-u','dog-init.service','-n','3000','--no-pager','-o','short-iso']),
        ('kernel',['journalctl','-k','-b','-n','1200','--no-pager']),
        ('previous-boot',['journalctl','-u','dog-init.service','-b','-1','-n','500','--no-pager']),
        ('dmesg',['dmesg','--ctime']),('addresses',['ip','-br','addr']),('routes',['ip','route','show','table','all']),
        ('rules',['ip','rule']),('network-devices',['nmcli','device','status']),
        ('network-profiles',['nmcli','-f','NAME,UUID,TYPE,DEVICE','connection','show']),
        ('ports',['ss','-lntup']),('usb',['lsusb']),('usb-tree',['lsusb','-t']),
        ('cameras',['v4l2-ctl','--list-devices']),
        ('tun',['ls','-l','/dev/net/tun']),
        ('processes',['ps','-eo','pid,ppid,stat,etimes,%cpu,%mem,rss,comm']),
        ('health',['curl','--fail','--max-time','5','http://127.0.0.1:8080/health']),
        ('agent-health',['curl','--fail','--max-time','5','http://127.0.0.1:8081/health']),
        ('control-status',['curl','--fail','--max-time','5','http://127.0.0.1:8080/status']),
    ]
    print('Collecting local evidence; no motion or service restart.',flush=True)
    for name_,argv in commands:command(name_,argv)
    for path in ['/etc/os-release','/etc/nv_tegra_release','/etc/orange-demo/core.env',
                 '/home/unitree/xpd/script/dog_init.json','/home/unitree/cyclonedds_ws/cyclonedds.xml',
                 '/var/lib/orange-offline/installed.json','/var/lib/orange-offline/backup-path']:
        file(path)
    # Only known settings, not arbitrary environment or private identity directories.
    bundle=pathlib.Path(__file__).resolve().parent
    for name_ in ['manifest.json','SHA256SUMS','deploy.env']:file(bundle/name_)
    log_roots=['/var/log/orange-offline','/tmp/orange-offline',
        '/home/aicor/compute_network/sandbox-demo/.dog-services',
        '/home/unitree/unitree/nav/log','/var/log/orange-demo/ros','/var/lib/orange-video-capture']
    for root in log_roots:
        root=pathlib.Path(root)
        if not root.exists():
            records.append({'source':str(root),'missing':True});continue
        candidates=[]
        for folder,dirs,files in os.walk(str(root),followlinks=False):
            dirs[:]=[d for d in dirs if not pathlib.Path(folder,d).is_symlink()]
            for name_ in files:
                path=pathlib.Path(folder,name_)
                try:
                    if not path.is_symlink() and path.is_file():candidates.append((path.stat().st_mtime,path))
                except OSError:pass
        candidates.sort(key=lambda item:item[0],reverse=True)
        if len(candidates)>150:records.append({'source':str(root),'omitted_older_files':len(candidates)-150})
        for _,path in candidates[:150]:file(path)
    for service in ('dog','dog-ctrl'):
        py='/home/aicor/compute_network/sandbox-demo/'+service+'/.venv/bin/python'
        command(service+'-packages',[py,'-m','pip','list','--format=json'],30)
        command(service+'-pip-check',[py,'-m','pip','check'],30)
    if pathlib.Path('/home/unitree/unitree/nav/install/setup.bash').exists():
        command('ros-nodes',['timeout','15','bash','-c',
            'source /home/unitree/unitree/nav/install/setup.bash; source /home/unitree/cyclonedds_ws/install/setup.bash; export HOME=/root ROS_LOG_DIR=/var/log/orange-demo/ros RMW_IMPLEMENTATION=rmw_cyclonedds_cpp CYCLONEDDS_URI=/home/unitree/cyclonedds_ws/cyclonedds.xml; ros2 node list --no-daemon --spin-time 5'],20)
    hashes={}
    for path in [pathlib.Path('/home/aicor/compute_network/sandbox-demo/dog/orange_dog.py'),
                 pathlib.Path('/home/aicor/compute_network/sandbox-demo/dog-ctrl/main.py'),
                 pathlib.Path('/home/unitree/xpd/script/dog_init.sh')]:
        try:hashes[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:hashes[str(path)]=str(exc)
    save('installed-code-sha256.json',json.dumps(hashes,indent=2))
    for path in pathlib.Path('/sys/class/thermal').glob('thermal_zone*/temp'):file(path)
    for path in pathlib.Path('/sys/class/thermal').glob('thermal_zone*/type'):file(path)
    save('README.txt','Share this .tar.gz and describe the operation/time of failure.\nNo robot actions, service restart, package install, camera stream, or core-network probes performed.\nPrivate identity folders and native .core dumps are excluded. Common secret fields are redacted; IP/device/task metadata may remain.\nFiles are bounded: 4 MiB per file/command, 160 MiB total, newest 150 files per log root. Collection omissions/timeouts are in collection-index.json.\nA running status without end/exit may indicate power loss or killed installer.\n')
    (directory/'collection-index.json').write_text(json.dumps(records,indent=2)+'\n')
    archive=output/(name+'.tar.gz')
    with tarfile.open(str(archive),'w:gz') as tar:tar.add(str(directory),arcname=name)
    digest=hashlib.sha256()
    with archive.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    pathlib.Path(str(archive)+'.sha256').write_text(digest.hexdigest()+'  '+archive.name+'\n')
    # sudo-created files can be copied by the invoking user.
    if os.geteuid()==0 and os.environ.get('SUDO_UID','').isdigit():
        uid,gid=int(os.environ['SUDO_UID']),int(os.environ.get('SUDO_GID','0'))
        for path in (archive,pathlib.Path(str(archive)+'.sha256')):os.chown(str(path),uid,gid)
        os.chmod(str(output),0o755)
    print('Diagnostic archive: '+str(archive),flush=True)
    print('Copy this archive and its .sha256 file; raw collection retained at '+str(directory))

if __name__=='__main__':main()
