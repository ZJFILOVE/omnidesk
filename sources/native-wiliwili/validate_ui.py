#!/usr/bin/env python3
"""Actual desktop keyboard events and libmpv assertions; explicitly synthetic media."""
import argparse
import http.client
import json
from pathlib import Path
import subprocess
import time

ROOT=Path(__file__).resolve().parent
STATE=ROOT/'validation/native-state.json'
REPORT=ROOT/'validation/ui-results.json'
results=[]
EVIDENCE=None
SELECTOR=None
COMPANION_PORT=None
def bind_test_process(process):
    global SELECTOR, COMPANION_PORT
    def owned(pid):
        for _ in range(12):
            if pid==process.pid: return True
            try: pid=int(Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[1])
            except (OSError,ValueError): return False
        return False
    until=time.monotonic()+20
    while time.monotonic()<until:
        for client in json.loads(subprocess.check_output(['hyprctl','clients','-j'])):
            if client['class']=='wiliwili' and owned(client['pid']):
                SELECTOR=f"pid:{client['pid']}"
                for entry in Path(f"/proc/{client['pid']}/environ").read_bytes().split(b'\0'):
                    if entry.startswith(b'WILI_AI_PORT='): COMPANION_PORT=int(entry.split(b'=',1)[1])
                return
        if process.poll() is not None:raise AssertionError('Test launcher exited')
        time.sleep(.1)
    raise AssertionError('Owned native test window did not appear')
def state():
    try: return json.loads(STATE.read_text())
    except (ValueError,OSError): return {}
def wait(predicate,timeout=45):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        data=state()
        if predicate(data): return data
        time.sleep(.1)
    raise AssertionError('Timed out: '+json.dumps(state(),ensure_ascii=False))
def dispatch(expression):
    if 'class:^wiliwili$' in expression:
        if not SELECTOR: raise AssertionError('Bind an owned test process before UI control')
        expression=expression.replace('class:^wiliwili$',SELECTOR)
    result=subprocess.run(['hyprctl','dispatch',expression],capture_output=True,text=True,check=True)
    if result.stdout.strip()!='ok': raise AssertionError(result.stdout)
def key(name,mods=''):
    # Wayland modifier metadata alone does not populate SDL's physical key state.
    # Send modifier key events too, as a real keyboard does.
    modifiers={'SHIFT':'Shift_L','CTRL':'Control_L','ALT':'Alt_L','SUPER':'Super_L'}
    held=[modifiers[value] for value in mods.split()]
    for modifier in held:
        dispatch('hl.dsp.send_key_state({window="class:^wiliwili$",mods="",key='+json.dumps(modifier)+',state="down"})')
        time.sleep(.1)
    for value in ['down','up']:
        dispatch('hl.dsp.send_key_state({window="class:^wiliwili$",mods='+json.dumps(mods)+',key='+json.dumps(name)+',state='+json.dumps(value)+'})')
        time.sleep(.12 if value=='down' else .25)
    for modifier in reversed(held):
        dispatch('hl.dsp.send_key_state({window="class:^wiliwili$",mods="",key='+json.dumps(modifier)+',state="up"})')
        time.sleep(.1)
def record(name,data=None):
    results.append(dict(check=name,result='pass',state=data or state()))
    if EVIDENCE: results[-1]['evidence']=EVIDENCE
    REPORT.write_text(json.dumps(results,ensure_ascii=False,indent=2))
    print(name+': PASS',flush=True)
def screenshot(name):
    clients=json.loads(subprocess.check_output(['hyprctl','clients','-j']))
    window=next(c for c in clients if SELECTOR==f"pid:{c['pid']}")
    x,y=window['at']; w,h=window['size']
    subprocess.run(['grim','-g',f'{x},{y} {w}x{h}',str(ROOT/'validation'/name)],check=True)

def run(installed=False):
    time.sleep(1)
    launcher=[str(Path.home()/'.local/bin/wiliwili-ai')] if installed else ['python3',str(ROOT/'launch.py')]
    command=[*launcher,'--ai-test-media',str(ROOT/'validation/clip.mp4'),'--fixture',str(ROOT/'validation/fixture.json'),'--diagnostics',str(STATE),'-d']
    STATE.unlink(missing_ok=True)
    log=open(ROOT/'validation/native-client.log','w')
    process=subprocess.Popen(command,stdout=log,stderr=log)
    bind_test_process(process)
    try:
        wait(lambda d:d.get('ready') and d.get('position',0)<10)
        dispatch('hl.dsp.window.float({window="class:^wiliwili$",action="enable"})')
        dispatch('hl.dsp.window.resize({window="class:^wiliwili$",x=1400,y=900,relative=false})')
        dispatch('hl.dsp.focus({window="class:^wiliwili$"})')
        key('space')
        record('Space pauses real libmpv',wait(lambda d:d.get('paused')))
        record('Real GPT-6 Luna detects English sponsor',wait(lambda d:len(d.get('ads',[]))==1 and not d.get('busy'),60))
        key('F8')
        record('F8 opens native AI panel with action focus',wait(lambda d:'生成总结' in d.get('focus_text','')))
        key('Tab')
        record('Tab advances keyboard focus',wait(lambda d:'ai/reader' in d.get('focus','')))
        key('Tab','SHIFT')
        record('Shift Tab reverses focus',wait(lambda d:'生成总结' in d.get('focus_text','')))
        key('Down')
        record('Arrow Down navigates native actions',wait(lambda d:'生成总结' not in d.get('focus_text','')))
        key('Up')
        record('Arrow Up navigates native actions',wait(lambda d:'生成总结' in d.get('focus_text','')))
        key('Return')
        record('Enter generates summary via real model',wait(lambda d:len(d.get('segments',[]))>=3 and not d.get('busy'),60))
        record('Summary refresh retains the keyboard action focus',wait(lambda d:'重新生成' in d.get('focus_text','')))
        screenshot('native-panel.png')
        # The chapter page exposes timeline actions directly after the reader.
        key('2')
        wait(lambda d:d.get('panel_tab')=='章节')
        key('Tab')
        record('Keyboard reaches first chapter',wait(lambda d:d.get('focus_text','').startswith('00:00')))
        key('Tab')
        record('Keyboard reaches sponsor chapter',wait(lambda d:d.get('focus_text','').startswith('00:20')))
        key('Return')
        record('Chapter selection seeks real libmpv to 20s',wait(lambda d:19.5<=d.get('position',0)<=20.5))
        screenshot('native-chapters.png')
        key('Escape')
        key('space')
        record('Automatic skip after three-second prompt uses real libmpv',wait(lambda d:d.get('position',0)>=40 and '自动跳过' in d.get('toast',''),10))
        key('space')
        key('F10')
        record('Want to watch undoes automatic skip',wait(lambda d:20<=d.get('position',0)<=25))
        key('F9')
        record('F9 seeks real libmpv to ad end',wait(lambda d:39.5<=d.get('position',0)<=40.5))
        key('F10')
        record('Want to watch restores pre-skip position',wait(lambda d:20<=d.get('position',0)<=25))
        key('F10','CTRL')
        record('Not an ad removes native marker',wait(lambda d:not d.get('ads')))
        key('m','CTRL')
        record('Manual advertising start uses real playback time',wait(lambda d:d.get('marking_start',-1)>=19))
        # The existing forward shortcut is right, inspect defaults if a build changes it.
        key('bracketright')
        time.sleep(.6)
        key('m','CTRL')
        record('Manual range appears in native timeline',wait(lambda d:any(a.get('source')=='manual' for a in d.get('ads',[]))))
        key('m','CTRL')
        key('m','CTRL SHIFT')
        record('Manual marking can be cancelled from keyboard',wait(lambda d:d.get('marking_start')==-1))
        key('F8')
        key('F7')
        record('F7 opens native keyboard text input',wait(lambda d:'brls/container' in d.get('focus','')))
        for char in 'explain nadph': key('space' if char==' ' else char)
        key('Return')
        record('Native keyboard question answered by real model',wait(lambda d:len(d.get('messages',[]))>=2 and not d.get('busy'),60))
        for _ in range(3): key('Next')
        time.sleep(.4)
        screenshot('native-question.png')
        key('Escape')
        key('F6')
        wait(lambda d:d.get('busy'))
        before=state()['generation']
        key('F12')
        record('Switching test video cancels pending summary and clears old state',wait(lambda d:d.get('generation',0)>before and d.get('identity')=='BVAItest0002:2:1' and not d.get('summary') and not d.get('ads'),60))
        time.sleep(3)
        record('Late old response cannot seek or restore old ads',wait(lambda d:d.get('identity')=='BVAItest0002:2:1' and not d.get('ads') and d.get('position',200)<15))
        key('F6')
        wait(lambda d:d.get('busy'))
        key('a','CTRL SHIFT')
        record('Disabling AI cancels pending work',wait(lambda d:d.get('enabled') is False and not d.get('ads')))
        time.sleep(3)
        record('Late results stay disabled',wait(lambda d:d.get('enabled') is False and not d.get('summary') and not d.get('ads')))
        key('a','CTRL SHIFT')
        wait(lambda d:d.get('enabled') is True)
        key('F12')
        record('Human correction survives a new playback session',wait(lambda d:d.get('identity')=='BVAItest0001:1:1' and not d.get('busy') and all(a.get('source')=='manual' for a in d.get('ads',[])),60))
        key('F6')
        wait(lambda d:d.get('busy'))
        before=state()['generation']
        key('F11')
        record('Same BV different cid and P cancels the previous request',wait(lambda d:d.get('generation',0)>before and d.get('identity')=='BVAItest0001:3:2' and not d.get('summary') and not d.get('ads'),60))
        record('New P receives its own results without previous P corrections',wait(lambda d:d.get('identity')=='BVAItest0001:3:2' and not d.get('busy') and not d.get('ads'),60))
        key('F6')
        wait(lambda d:d.get('busy'))
        record('Exit begins with a real model request pending')
    except Exception as error:
        results.append(dict(check='interrupted',result='fail',error=str(error),state=state()))
        REPORT.write_text(json.dumps(results,ensure_ascii=False,indent=2))
        print(str(error),flush=True)
        screenshot('native-failure.png')
        raise
    finally:
        dispatch('hl.dsp.window.close({window="class:^wiliwili$"})')
        try: process.wait(10)
        except subprocess.TimeoutExpired: process.terminate(); process.wait(5)
        log.close()
    assert COMPANION_PORT, 'Owned companion port was not recorded'
    connection=http.client.HTTPConnection('127.0.0.1',COMPANION_PORT,timeout=1)
    try:
        connection.connect()
        raise AssertionError('Companion still listening after client exit')
    except ConnectionRefusedError:
        record('Client exit removes companion and its pending connections',dict(client_exit_code=process.returncode))
    finally: connection.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--installed',action='store_true')
    run(parser.parse_args().installed)
