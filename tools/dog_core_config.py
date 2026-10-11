#!/usr/bin/env python3
"""Persist the core endpoint in the existing dog-init launch path. No restart by default."""
import argparse
import ipaddress
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time

def validate_host(value):
    host = value.strip().lower()
    if not 1 <= len(host) <= 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host):
        raise ValueError('Use an IPv4 address or DNS hostname without scheme/port')
    if any(not label or len(label)>63 or label.startswith('-') or label.endswith('-') for label in host.split('.')):
        raise ValueError('Invalid hostname')
    if all(c.isdigit() or c=='.' for c in host): ipaddress.IPv4Address(host)
    return host

def validate_launcher(text):
    """Require the version-controlled launcher; never edit deployed source."""
    if ('# ORANGE_DEMO_CORE_CONFIG:' not in text
            or 'source /etc/orange-demo/core.env' not in text
            or '--runtime-ip "$DOG_AGENT_RUNTIME_IP"' not in text):
        raise ValueError('Install the current dog/start_dog_services.sh source before changing the core address')

def atomic_write(path,text,mode):
    path.parent.mkdir(parents=True,exist_ok=True)
    previous = path.stat() if path.exists() else None
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.core-config-')
    try:
        with os.fdopen(fd,'w') as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        if previous is not None:
            os.chown(tmp, previous.st_uid, previous.st_gid)
        os.chmod(tmp,mode)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',required=True)
    parser.add_argument('--launcher',default='/home/aicor/compute_network/sandbox-demo/start_dog_services.sh')
    parser.add_argument('--restart',action='store_true',help='Explicitly restart original dog-init (interrupts all dog services)')
    args=parser.parse_args()
    host=validate_host(args.host)
    if os.geteuid()!=0: raise SystemExit('Run this helper through sudo')
    launcher=pathlib.Path(args.launcher)
    original=launcher.read_text()
    validate_launcher(original)
    subprocess.run(['bash','-n'],input=original,text=True,check=True)
    env=pathlib.Path('/etc/orange-demo/core.env')
    dropin=pathlib.Path('/etc/systemd/system/dog-init.service.d/core-endpoint.conf')
    stamp=time.strftime('%Y%m%d-%H%M%S')
    for path in (env,dropin):
        if path.exists(): shutil.copy2(path,str(path)+'.before-core-'+stamp)
    # The environment contains only host/ports, never device credentials.
    atomic_write(env,f'DOG_AGENT_RUNTIME_IP={host}\nDOG_AGENT_MASQUE_URL=https://{host}:8444/.well-known/masque/ip\n',0o644)
    atomic_write(dropin,'[Service]\nEnvironmentFile=-/etc/orange-demo/core.env\n',0o644)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    print(f'SAVED runtime={host}:8089 masque=https://{host}:8444/.well-known/masque/ip',flush=True)
    if args.restart:
        subprocess.run(['systemctl','restart','dog-init.service'],check=True)
        subprocess.run(['systemctl','is-active','dog-init.service'],check=True)
        print('Original dog-init restarted; endpoint bootstrap should be checked in dog.log')
    else:
        print('No process restarted. Applies on next original dog-init/Agent launch. Use --restart only when interruption is intended.')

if __name__=='__main__': main()
