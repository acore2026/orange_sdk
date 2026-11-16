#!/usr/bin/python3
"""Attach to the orange_dog child managed by dog-init.service."""
import fcntl
import pathlib
import subprocess
import time
ROOT = pathlib.Path('/var/lib/orange-video-capture')
ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
lock_file = open(ROOT / 'watch.lock', 'w')
fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
GDB_TEMPLATE = r'''set pagination off
set confirm off
set print elements 32
handle SIGPIPE nostop noprint pass
handle SIGBUS stop print pass
handle SIGSEGV stop print pass
continue
python
import gdb, glob, shutil
if gdb.selected_inferior().threads():
    gdb.execute('p $_siginfo')
    gdb.execute('thread apply all bt full 40')
    gdb.execute('info registers')
    gdb.execute('info proc mappings')
    root = '/var/lib/orange-video-capture'
    if len(glob.glob(root+'/*.core')) < 2 and shutil.disk_usage(root).free > 8*1024**3:
        gdb.execute('generate-core-file CORE_PATH')
    gdb.execute('continue')
end
'''
while True:
    try:
        raw = subprocess.check_output(['pgrep','-f',r'/orange_dog\.py '], text=True, stderr=subprocess.DEVNULL)
        pids = [int(item) for item in raw.split() if any(line.endswith('/system.slice/dog-init.service') for line in pathlib.Path('/proc', item, 'cgroup').read_text().splitlines())]
        pid = min(pids) if pids else 0
        if pid <= 1:
            time.sleep(2); continue
        dest = ROOT / (time.strftime('%Y%m%d-%H%M%S') + '-' + str(pid))
        script = ROOT / 'capture.gdb'
        script.write_text(GDB_TEMPLATE.replace('CORE_PATH', str(dest) + '.core'))
        with open(str(dest) + '.log', 'w') as log:
            print('ATTACH', pid, str(dest), flush=True)
            subprocess.run(['gdb','-q','-batch','-p',str(pid),'-x',str(script)], stdout=log, stderr=subprocess.STDOUT)
        time.sleep(3)
    except Exception as exc:
        print(type(exc).__name__, str(exc), flush=True)
        time.sleep(5)
