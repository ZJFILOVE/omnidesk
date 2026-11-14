#!/usr/bin/env python3
"""Real public playback and quality-menu regression in an isolated native process.

No cookies are imported. Only the test PID receives keys; preferences are temporary.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent

def run(installed=False):
    app = Path.home()/'.local/opt/wiliwili-ai' if installed else ROOT/'build'
    binary = app/'bin/wiliwili' if installed else app/'wiliwili'
    state_path = ROOT/'validation/quality-state.json'
    state_path.unlink(missing_ok=True)
    log_path = ROOT/'validation/quality-client.log'
    report = ROOT/'validation/quality-results.json'
    results = []
    previous = json.loads(subprocess.check_output(['hyprctl', 'activeworkspace', '-j']))['id']
    used = {w['id'] for w in json.loads(subprocess.check_output(['hyprctl', 'workspaces', '-j']))}
    workspace = next(i for i in range(9900, 10000) if i not in used)

    def state():
        try: return json.loads(state_path.read_text())
        except (OSError, ValueError): return {}
    def selections():
        return [dict(preference=int(p), selected=int(q), codec=int(c), auto_hdr=h == 'true')
                for p, q, c, h in re.findall(r'Native quality: preference=(\d+), selected=(\d+), codec=(\d+), auto_hdr=(true|false)', log_path.read_text())]
    def wait(predicate, timeout=30):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            data = state()
            if predicate(data): return data
            if process.poll() is not None: raise AssertionError('Client exited: '+str(process.returncode))
            time.sleep(.1)
        raise AssertionError('Timeout: '+json.dumps(state(), ensure_ascii=False))
    def dispatch(expression):
        output = subprocess.check_output(['hyprctl', 'dispatch', expression], text=True)
        assert output.strip() == 'ok', output
    def key(name):
        for action in ('down', 'up'):
            dispatch('hl.dsp.send_key_state({window='+json.dumps(selector)+',mods="",key='+json.dumps(name)+',state='+json.dumps(action)+'})')
        time.sleep(.4)
    def window():
        return next(c for c in json.loads(subprocess.check_output(['hyprctl', 'clients', '-j'])) if c['pid'] == process.pid)
    def screenshot(name):
        time.sleep(.5)
        win = window(); x,y = win['at']; w,h = win['size']
        subprocess.run(['grim','-g',f'{x},{y} {w}x{h}',str(ROOT/f'validation/quality-{name}.png')], check=True)
    def record(name):
        results.append(dict(check=name, result='pass', selections=selections()))
        report.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(name+': PASS', flush=True)

    with tempfile.TemporaryDirectory(prefix='wiliwili-quality-') as temporary, log_path.open('w') as log:
        env=os.environ.copy(); env.pop('WILI_AI_TOKEN', None)
        env.pop('WILI_ANDROID_SESSION', None)
        env['XDG_CONFIG_HOME']=temporary; env['WILI_AI_DIAGNOSTICS']=str(state_path)
        config=Path(temporary)/'wiliwili/wiliwili_config.json'
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({'setting':{'app_ui_scale':'1080p','search_tv_mode':False}}))
        process=subprocess.Popen(['stdbuf','-oL','-eL',str(binary),'--bv','BV1heam6TExz'],cwd=app,env=env,stdout=log,stderr=log)
        selector=f'pid:{process.pid}'
        try:
            wait(lambda d: not d.get('stopped') and d.get('position',0)>1 and selections(),50)
            assert selections()[-1]['preference']==0
            record('Fresh preferences automatically select a stream returned for the anonymous account')
            dispatch(f'hl.dsp.window.move({{window="{selector}",workspace={workspace}}})')
            dispatch(f'hl.dsp.focus({{workspace={workspace}}})')
            dispatch(f'hl.dsp.window.float({{window="{selector}",action="enable"}})')
            dispatch(f'hl.dsp.window.resize({{window="{selector}",x=1259,y=800,relative=false}})')
            dispatch(f'hl.dsp.window.center({{window="{selector}"}})')
            dispatch(f'hl.dsp.focus({{window="{selector}"}})')
            base=state()['desktop']['activity_count']
            key('F2'); wait(lambda d: d.get('desktop',{}).get('activity_count')==base+1)
            screenshot('menu')
            record('Quality menu opens with automatic highest option')
            # First fixed option is the highest quality advertised by this video.
            count=len(selections())
            key('Down'); key('Return')
            wait(lambda d: len(selections())>count and not d.get('stopped'))
            assert selections()[-1]['preference']>0
            assert selections()[-1]['selected']<=selections()[-1]['preference']
            record('Manual high quality requests media and falls back without a local login blocker')
            # Return to automatic from the actual selected quality.
            key('F2'); wait(lambda d: d.get('desktop',{}).get('activity_count')==base+1)
            for _ in range(state().get('desktop',{}).get('focused_item_index',0)):
                key('Up')
            wait(lambda d: d.get('desktop',{}).get('focused_item_index')==0)
            count=len(selections()); key('Return')
            wait(lambda d: len(selections())>count and not d.get('stopped'))
            assert selections()[-1]['preference']==0
            assert json.loads(config.read_text())['setting']['video_quality']==0
            screenshot('playing')
            record('Automatic mode can be restored from the menu and persists')
        except Exception as error:
            results.append(dict(check='interrupted',result='fail',error=str(error),selections=selections()))
            report.write_text(json.dumps(results,ensure_ascii=False,indent=2))
            if process.poll() is None: screenshot('failure')
            raise
        finally:
            if process.poll() is None: process.terminate()
            try: process.wait(10)
            except subprocess.TimeoutExpired: process.kill(); process.wait(5)
            dispatch(f'hl.dsp.focus({{workspace={previous}}})')

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--installed',action='store_true')
    run(parser.parse_args().installed)
