#!/usr/bin/env python3
"""Review native home/search and real PlayerActivity using isolated preferences."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import validate_ui as ui
from validate_keyboard_ui import tap, screenshot
from validate_dock_ui import disjoint, resize

ROOT = Path(__file__).resolve().parent
ui.key = tap

def run(installed=False):
    ui.STATE = ROOT/'validation/redesign-native-state.json'
    ui.REPORT = ROOT/'validation/redesign-ui-results.json'
    ui.results = []
    previous = json.loads(subprocess.check_output(['hyprctl', 'activeworkspace', '-j']))['id']
    used = {w['id'] for w in json.loads(subprocess.check_output(['hyprctl', 'workspaces', '-j']))}
    workspace = next(i for i in range(9900,10000) if i not in used)
    app = Path.home()/'.local/opt/wiliwili-ai' if installed else ROOT/'build'
    binary = app/'bin/wiliwili' if installed else app/'wiliwili'
    process = None
    log = open(ROOT/'validation/redesign-client.log', 'w')
    with tempfile.TemporaryDirectory(prefix='wiliwili-ui-review-') as temporary:
        env = os.environ.copy()
        env['XDG_CONFIG_HOME'] = temporary
        env['WILI_AI_DIAGNOSTICS'] = str(ui.STATE)
        config = Path(temporary)/'wiliwili/wiliwili_config.json'
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({'setting': {'app_ui_scale':'1080p','search_tv_mode':False}}))
        def launch(*args):
            ui.STATE.unlink(missing_ok=True)
            proc = subprocess.Popen([str(binary), *args], cwd=app, env=env, stdout=log, stderr=log)
            ui.wait(lambda d: 'desktop' in d)
            ui.dispatch('hl.dsp.window.move({window="class:^wiliwili$",workspace='+str(workspace)+'})')
            ui.dispatch('hl.dsp.focus({workspace='+str(workspace)+'})')
            ui.dispatch('hl.dsp.window.float({window="class:^wiliwili$",action="enable"})')
            resize(1400,900)
            ui.dispatch('hl.dsp.focus({window="class:^wiliwili$"})')
            return proc
        def close(proc):
            ui.dispatch('hl.dsp.window.close({window="class:^wiliwili$"})')
            try: proc.wait(10)
            except subprocess.TimeoutExpired: proc.terminate(); proc.wait(5)
        try:
            process = launch()
            time.sleep(5)
            screenshot('redesign-home.png')
            ui.record('Home renders unified navigation and video cards')
            ui.key('slash'); ui.wait(lambda d:d.get('desktop',{}).get('editing'))
            for char in 'photosynthesis': ui.key(char)
            screenshot('redesign-search-input.png')
            ui.key('Return'); ui.wait(lambda d:not d.get('desktop',{}).get('editing'))
            time.sleep(5)
            screenshot('redesign-search-results.png')
            ui.record('Desktop search renders and accepts native keyboard input')
            close(process); process = None
            process = launch('--bv', 'BV1heam6TExz')
            ui.record('Real Bilibili video plays in redesigned PlayerActivity', ui.wait(lambda d:d.get('ready') and not d.get('stopped') and d.get('position',0)>0,60))
            ui.key('space'); ui.wait(lambda d:d.get('paused'))
            screenshot('redesign-real-player.png')
            ui.key('F8')
            ui.record('Real PlayerActivity keeps reading column outside video', ui.wait(lambda d:d.get('dock',{}).get('open') and disjoint(d['dock']['video'],d['dock']['panel']),10))
            screenshot('redesign-real-dock.png')
            ui.key('F7'); ui.wait(lambda d:d.get('desktop',{}).get('editing'))
            ui.record('Real PlayerActivity keeps question editor outside video', ui.wait(lambda d:disjoint(d['dock']['video'],d['desktop']['input_rect'])))
            ui.key('Escape'); ui.wait(lambda d:not d.get('desktop',{}).get('editing'))
            ui.key('F8'); ui.wait(lambda d:not d.get('dock',{}).get('open'))
            ui.key('v'); time.sleep(.8); ui.key('F8')
            ui.record('Real fullscreen player keeps video and assistant separate', ui.wait(lambda d:d.get('dock',{}).get('open') and disjoint(d['dock']['video'],d['dock']['panel']),10))
            ui.key('F8'); ui.wait(lambda d:not d.get('dock',{}).get('open')); ui.key('v')
            ui.record('Fullscreen closes back to normal player after assistant use', ui.wait(lambda d:'VideoView' in d.get('focus','') and not d.get('dock',{}).get('open')))
        except Exception as error:
            ui.results.append({'check':'interrupted','result':'fail','error':str(error),'state':ui.state()})
            ui.REPORT.write_text(json.dumps(ui.results,ensure_ascii=False,indent=2))
            try: screenshot('redesign-real-failure.png')
            except Exception: pass
            raise
        finally:
            if process is not None: close(process)
            ui.dispatch('hl.dsp.focus({workspace='+str(previous)+'})')
            log.close()

if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--installed',action='store_true')
    run(parser.parse_args().installed)
