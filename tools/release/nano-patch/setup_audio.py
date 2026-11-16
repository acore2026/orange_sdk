#!/usr/bin/env python3
"""Upload/bind this robot's audio locally. Never plays audio or sends motion."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import update


def audio_entries(output, prefix):
    replies=[line[len(prefix):] for line in output.splitlines() if line.startswith(prefix)]
    if not replies: raise RuntimeError('Audio list response missing; inspect log before retrying')
    response=json.loads(replies[-1])
    data=response['data']
    if data['header']['status']['code']!=0: raise RuntimeError('Audio list rejected')
    body=data['data']
    if isinstance(body,str): body=json.loads(body)
    return body.get('audio_list',[])


def choose(entries, name, requested=None):
    matches=[e for e in entries if e.get('CUSTOM_NAME')==name and (not requested or e.get('UNIQUE_ID')==requested)]
    if len(matches)>1: raise RuntimeError('Multiple matching recordings; inspect and pass --audio-id explicitly')
    if not matches: return None
    audio_id=matches[0].get('UNIQUE_ID','')
    if not re.fullmatch(r'[A-Za-z0-9-]{1,128}',audio_id): raise RuntimeError('Invalid audio ID')
    return audio_id


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ip',default='192.168.123.161',help='Dog body IP, not SSH forwarding address')
    p.add_argument('--audio-id',help='Select an existing matching library entry explicitly')
    a=p.parse_args()
    if a.ip!='192.168.123.161':
        p.error('This release uses body IP 192.168.123.161; custom IP needs a runtime configuration change')
    manifest=update.verify_bundle(update.BUNDLE)
    update.environment_check(manifest)
    update.service_stopped()
    with (update.STATE/'update.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        marker=update.STATE/(manifest['release']+'.json')
        state=json.loads(marker.read_text())
        if state['phase'] not in ('installed_audio_unbound','ready'): raise RuntimeError('Install patch first')
        voice=Path(manifest['voice_dir'])
        for e in manifest['files']:
            if e['path'].startswith(manifest['voice_dir'].lstrip('/')+'/'):
                if update.digest(Path('/')/e['path'])!=e['after']: raise RuntimeError('Voice files differ from package')
        log_dir=Path('/var/log/orange-patch');log_dir.mkdir(parents=True,exist_ok=True)
        log=log_dir/('audio-'+time.strftime('%Y%m%d-%H%M%S')+'-'+str(os.getpid())+'.log')
        base=[str(voice/'venv/bin/python'),str(voice/'go2_audio.py'),'--ip',a.ip]
        def call(extra):
            try:
                r=subprocess.run(base+extra,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=120)
            except subprocess.TimeoutExpired as exc:
                with log.open('ab') as f: f.write(exc.stdout or b'')
                raise RuntimeError('Audio operation timed out; query library before retrying. Log: '+str(log))
            with log.open('a') as f: f.write(r.stdout)
            if r.returncode: raise RuntimeError('Audio operation failed; inspect '+str(log))
            return r.stdout
        # File stem becomes audiohub CUSTOM_NAME. Hash suffix distinguishes releases.
        audio=voice/'warning.wav'
        name='orange_warning_'+update.digest(audio)[:12]
        entries=audio_entries(call([]),'RESPONSE audiohub 1001 ')
        audio_id=choose(entries,name,a.audio_id)
        if a.audio_id and audio_id is None: raise RuntimeError('Requested ID not in the expected named recording')
        if audio_id is None:
            upload=voice/(name+'.wav')
            upload.write_bytes(audio.read_bytes())
            try:
                output=call(['--upload',str(upload)])
            finally:
                upload.unlink()
            replies=[json.loads(x[len('UPLOAD_RESULT '):]) for x in output.splitlines() if x.startswith('UPLOAD_RESULT ')]
            if not replies or replies[-1]['data']['header']['status']['code']!=0: raise RuntimeError('Missing successful upload acknowledgement')
            audio_id=choose(audio_entries(output,'SAVED_AUDIO_LIST '),name)
            if audio_id is None: raise RuntimeError('Uploaded audio not found')
        st=update.CONFIG.stat()
        config=update.merged_env(update.CONFIG.read_text(),{'DOG_WARNING_DIR':str(voice),'DOG_WARNING_AUDIO_ID':audio_id})
        update.atomic_write(update.CONFIG,config.encode(),st.st_mode&0o777,st.st_uid,st.st_gid)
        state.update(phase='ready',audio_id=audio_id,audio_name=name,audio_sha256=update.digest(audio))
        update.atomic_write(marker,(json.dumps(state,indent=2)+'\n').encode(),0o600)
        print('Audio bound:',audio_id,'Log:',log)
        print('No playback or motion was sent. Services remain stopped; review allocation before starting.')

if __name__=='__main__':
    try: main()
    except Exception as exc: print('ERROR:',exc,file=sys.stderr);sys.exit(1)
