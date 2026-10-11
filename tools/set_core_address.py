#!/usr/bin/env python3
"""Local operator command: change RayNeo or dog core host without rebuilding APKs."""
import argparse
import base64
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
from dog_core_config import validate_host

PACKAGE='com.rayneo.agent.example.rayneo'
ACTIVITY=PACKAGE+'/com.rayneo.agent.example.RayNeoMainActivity'
RECEIVER=PACKAGE+'/com.rayneo.agent.example.DemoConfigReceiver'

def adb_binary(explicit=None):
    for candidate in (explicit, shutil.which('adb'), '/home/huawei/android-sdk/platform-tools/adb'):
        if candidate and pathlib.Path(candidate).is_file(): return candidate
    raise SystemExit('adb not found: supply --adb PATH')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target',choices=['glasses','dog'])
    parser.add_argument('--host',help='New core IPv4/DNS host; omit for glasses pairing/config query')
    parser.add_argument('--adb')
    parser.add_argument('--serial',default='BC942497EA1C042')
    parser.add_argument('--ssh-host',default='192.168.137.14')
    parser.add_argument('--ssh-port',type=int,default=40032)
    parser.add_argument('--ssh-user',default='unitree')
    parser.add_argument('--ssh',help='SSH binary; Windows ssh.exe used automatically in WSL')
    parser.add_argument('--launcher',default='/home/aicor/compute_network/sandbox-demo/start_dog_services.sh')
    parser.add_argument('--restart',action='store_true',help='Dog only: explicitly restart existing dog-init services')
    parser.add_argument('--no-restart',action='store_true',help='Glasses only: persist without relaunching app')
    args=parser.parse_args()
    host=validate_host(args.host) if args.host else None
    if args.target=='glasses':
        adb=[adb_binary(args.adb),'-s',args.serial]
        command=adb+['shell','am','broadcast','-n',RECEIVER,'-a','com.rayneo.demo.CONFIG']
        if host: command+=['--es','core_host',host]
        result=subprocess.run(command,text=True,capture_output=True,check=True)
        print(result.stdout.strip())
        if 'result=0' not in result.stdout or 'OK core_host=' not in result.stdout:
            raise SystemExit('Configuration not acknowledged. Install the new RayNeo APK first.')
        if host and not args.no_restart:
            subprocess.run(adb+['shell','am','force-stop',PACKAGE],check=True)
            subprocess.run(adb+['shell','am','start','-n',ACTIVITY],check=True)
            print('Glasses relaunched with persisted core host. No robot task dispatched.')
        subprocess.run(adb+['shell','ip','-4','addr','show','wlan0'],check=True)
    else:
        if not host: raise SystemExit('--host is required for dog')
        ssh=args.ssh or ('/mnt/c/Windows/System32/OpenSSH/ssh.exe' if pathlib.Path('/mnt/c/Windows/System32/OpenSSH/ssh.exe').exists() else shutil.which('ssh'))
        if not ssh: raise SystemExit('ssh not found')
        validate_host(args.ssh_host)
        if not 1<=args.ssh_port<=65535 or not re.fullmatch(r'[a-z_][a-z0-9_-]*',args.ssh_user): raise SystemExit('Invalid SSH user/port')
        helper=pathlib.Path(__file__).with_name('dog_core_config.py').read_bytes()
        encoded=base64.b64encode(helper).decode('ascii')
        code="import base64; exec(compile(base64.b64decode('"+encoded+"'), '<dog-core-config>', 'exec'))"
        remote=['sudo','python3','-c',code,'--host',host,'--launcher',args.launcher]
        if args.restart: remote+=['--restart']
        subprocess.run([ssh,'-t','-o','ConnectTimeout=10','-p',str(args.ssh_port),args.ssh_user+'@'+args.ssh_host,shlex.join(remote)],check=True)

if __name__=='__main__': main()
