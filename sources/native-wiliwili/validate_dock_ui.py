#!/usr/bin/env python3
"""Native playback, layout and input regression; local synthetic media, real model."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import validate_ui as ui
from validate_keyboard_ui import tap, screenshot

ROOT = Path(__file__).resolve().parent
ui.key = tap

def disjoint(a, b):
    return (a['x'] + a['width'] <= b['x'] + 2 or b['x'] + b['width'] <= a['x'] + 2
            or a['y'] + a['height'] <= b['y'] + 2 or b['y'] + b['height'] <= a['y'] + 2)

def layout(name):
    return ui.wait(lambda d: d.get('dock', {}).get('layout') == name
                   and min(d['dock']['video']['width'], d['dock']['video']['height']) > 100
                   and disjoint(d['dock']['video'], d['dock']['panel']), 10)

def resize(width, height):
    ui.dispatch('hl.dsp.window.resize({window="class:^wiliwili$",x='+str(width)+',y='+str(height)+',relative=false})')
    ui.dispatch('hl.dsp.window.center({window="class:^wiliwili$"})')
    time.sleep(.7)

def run(installed=False):
    ui.STATE = ROOT/'validation/dock-native-state.json'
    ui.REPORT = ROOT/'validation/dock-ui-results.json'
    ui.results = []
    ui.STATE.unlink(missing_ok=True)
    previous = json.loads(subprocess.check_output(['hyprctl', 'activeworkspace', '-j']))['id']
    used = {w['id'] for w in json.loads(subprocess.check_output(['hyprctl', 'workspaces', '-j']))}
    workspace = next(i for i in range(9900, 10000) if i not in used)
    launch = [str(Path.home()/'.local/bin/wiliwili-ai')] if installed else ['python3', str(ROOT/'launch.py')]
    log = open(ROOT/'validation/dock-client.log', 'w')
    clip = ROOT/'validation/dock-clip.mp4'
    if not clip.exists(): clip = ROOT/'validation/clip.mp4'
    process = subprocess.Popen([*launch, '--ai-test-media', str(clip), '--fixture', str(ROOT/'validation/fixture.json'), '--diagnostics', str(ui.STATE)], stdout=log, stderr=log)
    ui.bind_test_process(process)
    try:
        ui.wait(lambda d: d.get('ready') and not d.get('stopped'))
        ui.dispatch('hl.dsp.window.move({window="class:^wiliwili$",workspace='+str(workspace)+'})')
        ui.dispatch('hl.dsp.focus({workspace='+str(workspace)+'})')
        ui.dispatch('hl.dsp.window.float({window="class:^wiliwili$",action="enable"})')
        resize(1400, 900)
        ui.dispatch('hl.dsp.focus({window="class:^wiliwili$"})')
        ui.key('g', 'CTRL')  # Isolate UI from automatic sponsor seeking.
        ui.key('F8')
        ui.record('Wide reading column never overlaps video', layout('right'))
        started = ui.state()['position']
        ui.record('Opening assistant preserves active playback', ui.wait(lambda d: not d.get('paused') and d.get('position', 0) > started + 1, 8))
        ui.key('space'); ui.record('Space pauses while reading', ui.wait(lambda d: d.get('paused')))
        ui.key('period'); ui.record('Speed remains available while reading', ui.wait(lambda d: abs(d.get('speed', 0)-1.25)<.01))
        ui.key('equal')
        ui.key('F6')
        ui.record('Real model summary and chapters render in the column', ui.wait(lambda d: d.get('summary') and len(d.get('segments', [])) >= 3 and not d.get('busy'), 90))
        screenshot('redesign-dock-summary.png')
        ui.key('2'); ui.wait(lambda d: d.get('panel_tab') == '章节')
        ui.key('Tab'); ui.key('Return')
        ui.record('Chapter action seeks without dismissing reading column', ui.wait(lambda d: d.get('dock', {}).get('open') and d.get('position', 999) < 1))
        screenshot('redesign-dock-chapters.png')
        ui.key('Tab', 'CTRL')
        ui.record('Ctrl+Tab gives control back to video', ui.wait(lambda d: not d.get('dock', {}).get('reading_focus')))
        ui.key('l'); ui.record('Video focus restores normal seek keys', ui.wait(lambda d: 4.5 <= d.get('position', 0) <= 5.5))
        ui.key('Tab', 'CTRL'); ui.wait(lambda d: d.get('dock', {}).get('reading_focus'))
        ui.key('1'); ui.key('j')
        ui.record('Reading keys scroll without seeking', ui.wait(lambda d: d.get('reader_offset', 0)>10 and 4.5 <= d.get('position', 0) <= 5.5))
        ui.key('F7'); ui.wait(lambda d: d.get('desktop', {}).get('editing'))
        ui.record('Question editor stays outside video', ui.wait(lambda d: disjoint(d['desktop']['input_rect'], d['dock']['video'])))
        for char in 'what does nadph do': ui.key('space' if char == ' ' else char)
        ui.record('Typing isolates playback and Vim shortcuts', ui.wait(lambda d: d.get('desktop', {}).get('input_text') == 'what does nadph do' and abs(d.get('speed', 0)-1)<.01 and 4.5<=d.get('position',0)<=5.5))
        screenshot('redesign-dock-input.png')
        ui.key('Return')
        ui.record('Question reaches real model and answer stays in reading column', ui.wait(lambda d: len(d.get('messages', []))>=2 and not d.get('busy') and d.get('dock', {}).get('open'), 90))
        screenshot('redesign-dock-answer.png')
        resize(900, 850)
        ui.record('Narrow window moves reader below intact video', layout('bottom'))
        screenshot('redesign-dock-narrow.png')
        ui.key('F7'); ui.wait(lambda d: d.get('desktop', {}).get('editing'))
        ui.record('Narrow question editor never overlaps video', ui.wait(lambda d: disjoint(d['desktop']['input_rect'], d['dock']['video'])))
        ui.key('Escape'); ui.wait(lambda d: not d.get('desktop', {}).get('editing'))
        resize(1400, 900); layout('right')
        ui.key('F8'); ui.record('F8 removes dock and restores video focus', ui.wait(lambda d: not d.get('dock', {}).get('open') and 'video' in d.get('focus', '')))
        ui.key('v'); time.sleep(.8)
        ui.key('F8'); ui.record('Fullscreen video can share space with assistant', layout('right'))
        screenshot('redesign-dock-fullscreen.png')
        ui.key('Escape'); ui.wait(lambda d: not d.get('dock', {}).get('open'))
        ui.key('space'); before = ui.state()['position']
        ui.record('Closing assistant keeps playback running', ui.wait(lambda d: not d.get('paused') and d.get('position',0)>before+1,8))
        ui.key('space')
        screenshot('redesign-player-restored.png')
    except Exception as error:
        ui.results.append({'check':'interrupted', 'result':'fail', 'error':str(error), 'state':ui.state()})
        ui.REPORT.write_text(json.dumps(ui.results,ensure_ascii=False,indent=2))
        try: screenshot('redesign-dock-failure.png')
        except Exception: pass
        raise
    finally:
        ui.dispatch('hl.dsp.window.close({window="class:^wiliwili$"})')
        ui.dispatch('hl.dsp.focus({workspace='+str(previous)+'})')
        try: process.wait(10)
        except subprocess.TimeoutExpired: process.terminate(); process.wait(5)
        log.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--installed', action='store_true')
    run(parser.parse_args().installed)
