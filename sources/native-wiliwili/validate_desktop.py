#!/usr/bin/env python3
"""Launch the real desktop file without test fixtures or diagnostic flags."""
import argparse
import json
from pathlib import Path
import subprocess
import time

ROOT=Path(__file__).resolve().parent
parser=argparse.ArgumentParser()
parser.add_argument('--keep-open',action='store_true')
args=parser.parse_args()
with open(ROOT/'validation/desktop-native-client.log','w') as log:
    subprocess.run(['gio','launch',str(Path.home()/'.local/share/applications/wiliwili-ai.desktop')],stdout=log,stderr=log,check=True,start_new_session=True)
    for _ in range(100):
        clients=json.loads(subprocess.check_output(['hyprctl','clients','-j']))
        windows=[w for w in clients if w['class']=='wiliwili']
        if windows: break
        time.sleep(.1)
    else: raise AssertionError('Desktop launch created no native window; inspect desktop-native-client.log')
time.sleep(1)
clients=json.loads(subprocess.check_output(['hyprctl','clients','-j']))
assert any(w['pid']==windows[0]['pid'] for w in clients),'Native desktop window did not stay running'
result=[dict(check='Actual desktop entry opens native client without diagnostic flags',result='pass',title=windows[0]['title'])]
(ROOT/'validation/desktop-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(result[0]['check']+': PASS',flush=True)
if not args.keep_open:
    subprocess.run(['hyprctl','dispatch','hl.dsp.window.close({window="class:^wiliwili$"})'],capture_output=True,check=True)
