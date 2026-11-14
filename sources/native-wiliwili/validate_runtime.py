#!/usr/bin/env python3
"""Launch the untouched fallback and check bridge/companion lifecycle without logging auth."""
import hashlib
import http.client
import json
from pathlib import Path
import subprocess
import time

ROOT=Path(__file__).resolve().parent
home=Path.home()
results=[]
def record(check,**details):
    results.append(dict(check=check,result='pass',**details))
    print(check+': PASS',flush=True)
settings=json.loads((home/'.config/bili-helper-codex/config.json').read_text())
conn=http.client.HTTPConnection('127.0.0.1',43188,timeout=2)
try:
    conn.request('GET','/health',headers={'Authorization':'Bearer '+settings['token']})
    response=conn.getresponse()
    health=json.loads(response.read())
    assert response.status==200 and health['model']=='gpt-6-luna'
    record('Existing local model bridge remains healthy',model=health['model'])
finally: conn.close()
conn=http.client.HTTPConnection('127.0.0.1',43189,timeout=1)
try:
    conn.connect()
    raise AssertionError('Companion still listening after client exit')
except ConnectionRefusedError:
    record('No companion left after client exit')
finally: conn.close()
log=open(ROOT/'validation/backup-native-client.log','w')
process=subprocess.Popen([str(home/'.local/bin/wiliwili-original')],stdout=log,stderr=log)
try:
    for _ in range(100):
        clients=json.loads(subprocess.check_output(['hyprctl','clients','-j']))
        windows=[w for w in clients if w['pid']==process.pid and w['class']=='wiliwili']
        if windows: break
        if process.poll() is not None: raise AssertionError('Fallback exited before opening window')
        time.sleep(.1)
    else: raise AssertionError('Fallback native window missing')
    record('Unmodified upstream backup opens its own native window',title=windows[0]['title'])
finally:
    subprocess.run(['hyprctl','dispatch','hl.dsp.window.close({window="class:^wiliwili$"})'],capture_output=True,check=True)
    try: process.wait(10)
    except subprocess.TimeoutExpired: process.terminate();process.wait(5)
    log.close()
assert process.returncode==0
record('Unmodified fallback exits cleanly',exit_code=process.returncode)
(ROOT/'validation/runtime-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
