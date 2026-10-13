import subprocess,os
base='/home/aicor/compute_network/sandbox-demo'
env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',CYCLONEDDS_HOME='/home/unitree/cyclonedds/install')
for service in ('dog','dog-ctrl'):
 py=f'{base}/{service}/.venv/bin/python'
 imports='import aiohttp,cv2,numpy,cyclonedds; from unitree_sdk2py.go2.sport.sport_client import SportClient'
 if service=='dog':imports+='; import agent_sdk,aiortc,av,aioquic; from importlib.metadata import version; assert version("agent-connect-sdk")=="0.17.11"'
 subprocess.run([py,'-c',imports],env=env,check=True)
 subprocess.run([py,'-m','pip','check'],env=env,check=True)
print('Import and dependency checks passed. No camera opened and no DDS motion client initialized.')
