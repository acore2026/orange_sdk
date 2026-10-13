#!/usr/bin/env python3
"""Validate new-dog settings and write only application configurations."""
import argparse, json, pathlib, re, subprocess, xml.etree.ElementTree as ET
p=argparse.ArgumentParser(); p.add_argument('--check',action='store_true');p.add_argument('settings');args=p.parse_args()
values={}
allowed={'CORE_HOST','RUNTIME_PORT','MASQUE_PORT','DOG_NAME','CAMERA_ID','CONTROL_INTERFACE','WIRED_CONNECTION_NAME','CORE_ALLOCATION_CONFIRMED'}
for line in pathlib.Path(args.settings).read_text().splitlines():
 line=line.strip()
 if not line or line.startswith('#'):continue
 key,sep,value=line.partition('=')
 if not sep or key not in allowed or key in values:raise SystemExit('Unknown/duplicate configuration: '+key)
 values[key]=value
if values.keys()!=allowed:raise SystemExit('Missing settings: '+str(allowed-values.keys()))
if not re.fullmatch(r'[A-Za-z0-9.-]+',values['CORE_HOST']):raise SystemExit('Invalid core host')
for key in ('RUNTIME_PORT','MASQUE_PORT'):
 if not values[key].isdigit() or not 1<=int(values[key])<=65535:raise SystemExit('Invalid '+key)
for key in ('DOG_NAME','CONTROL_INTERFACE'):
 if not re.fullmatch(r'[A-Za-z0-9_.-]+',values[key]):raise SystemExit('Invalid '+key)
if not values['CAMERA_ID'].isdigit():raise SystemExit('CAMERA_ID must be numeric')
if values['CORE_ALLOCATION_CONFIRMED']!='yes':raise SystemExit('Set CORE_ALLOCATION_CONFIRMED=yes after reserving a distinct role or disconnecting the old dog')
if not pathlib.Path('/sys/class/net',values['CONTROL_INTERFACE']).exists():raise SystemExit('Control interface does not exist')
if not pathlib.Path('/dev/video'+values['CAMERA_ID']).exists():raise SystemExit('Selected camera does not exist')
if not re.fullmatch(r'[A-Za-z0-9_. -]+',values['WIRED_CONNECTION_NAME']):raise SystemExit('Invalid USB profile name')
profiles=subprocess.check_output(['nmcli','-g','NAME','connection','show'],text=True).splitlines()
if values['WIRED_CONNECTION_NAME'] in profiles:
 raise SystemExit('Choose a NEW, unused USB profile name; dog-init may delete/recreate this profile, never select the internal control profile')
if args.check:print('Configuration valid');raise SystemExit(0)
base='/home/aicor/compute_network/sandbox-demo'
config=pathlib.Path('/etc/orange-demo');config.mkdir(parents=True,exist_ok=True)
env={
 'DOG_AGENT_RUNTIME_IP':values['CORE_HOST'],'DOG_AGENT_RUNTIME_PORT':values['RUNTIME_PORT'],
 'DOG_AGENT_MASQUE_URL':f"https://{values['CORE_HOST']}:{values['MASQUE_PORT']}/.well-known/masque/ip",
 'DOG_AGENT_NAME':values['DOG_NAME'],'DOG_AGENT_CAMERA_ID':values['CAMERA_ID'],
 'DOG_CTRL_INTERFACE':values['CONTROL_INTERFACE'],
}
(config/'core.env').write_text(''.join(f'{k}={v}\n' for k,v in env.items()))
init=json.loads(pathlib.Path('config/dog_init.json').read_text())
init.update(sudo_password='',wired_connection_name=values['WIRED_CONNECTION_NAME'],
 dog_services_script_path=base+'/start_dog_services.sh',dog_services_pid_dir=base+'/.dog-services/pids',
 dog_app_config_path=base+'/dog/config.example.json',dog_services_run_user='')
path=pathlib.Path('/home/unitree/xpd/script/dog_init.json');path.parent.mkdir(parents=True,exist_ok=True)
path.write_text(json.dumps(init,indent=2)+'\n');path.chmod(0o600)

xml=pathlib.Path('/home/unitree/cyclonedds_ws/cyclonedds.xml')
ET.register_namespace('', 'https://cdds.io/config')
tree=ET.parse(xml)
for item in tree.iter():
 if item.tag.split('}')[-1]=='NetworkInterface':item.set('name',values['CONTROL_INTERFACE'])
tree.write(xml,encoding='utf-8',xml_declaration=True)
