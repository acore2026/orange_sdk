import json,pathlib,subprocess
required=json.load(open('base-packages.json'))
installed={}
for line in subprocess.check_output(['dpkg-query','-W','-f=${Package}\t${Version}\t${db:Status-Status}\n'],text=True).splitlines():
 fields=line.split('\t')
 if len(fields)==3 and fields[2]=='installed':installed[fields[0]]=fields[1]
missing=[f'{k}={v}' for k,v in required.items() if installed.get(k)!=v]
if missing:raise SystemExit('Base-image package mismatch:\n'+'\n'.join(missing))
print('Base-image package versions match.')
