#!/usr/bin/env python3
"""Exercise the desktop redesign with real native key events and libmpv."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import validate_ui as ui

ROOT = Path(__file__).resolve().parent

def tap(name, mods=''):
    # Send a complete tap in one compositor transaction; separate IPC calls
    # can turn a 120 ms test key into an OS repeat under desktop load.
    modifiers = {'SHIFT':'Shift_L','CTRL':'Control_L','ALT':'Alt_L','SUPER':'Super_L'}
    held = [modifiers[value] for value in mods.split()]
    commands = []
    def event(key, state, metadata=''):
        assert ui.SELECTOR, 'Bind the owned test process before sending keys'
        commands.append('dispatch hl.dsp.send_key_state({window='+json.dumps(ui.SELECTOR)+',mods='+json.dumps(metadata)+',key='+json.dumps(key)+',state='+json.dumps(state)+'})')
    for modifier in held: event(modifier, 'down')
    event(name, 'down', mods); event(name, 'up', mods)
    for modifier in reversed(held): event(modifier, 'up')
    result = subprocess.run(['hyprctl','--batch',';'.join(commands)],capture_output=True,text=True,check=True)
    if any(line.strip() != 'ok' for line in result.stdout.splitlines() if line.strip()): raise AssertionError(result.stdout)
    time.sleep(.25)

ui.key = tap

def screenshot(name):
    previous = json.loads(subprocess.check_output(['hyprctl','activeworkspace','-j']))['id']
    clients = json.loads(subprocess.check_output(['hyprctl','clients','-j']))
    window = next(c for c in clients if ui.SELECTOR == f"pid:{c['pid']}")
    workspace = window['workspace']['id']
    try:
        ui.dispatch('hl.dsp.focus({workspace='+str(workspace)+'})')
        ui.dispatch('hl.dsp.focus({window="class:^wiliwili$"})')
        time.sleep(.6)
        ui.screenshot(name)
    finally:
        if previous != workspace: ui.dispatch('hl.dsp.focus({workspace='+str(previous)+'})')

def run(installed=False):
    ui.STATE = ROOT / 'validation/keyboard-native-state.json'
    ui.REPORT = ROOT / 'validation/keyboard-ui-results.json'
    ui.results = []
    ui.EVIDENCE = 'explicit_local_fixture_and_real_existing_model_with_native_libmpv'
    ui.STATE.unlink(missing_ok=True)
    previous_workspace = json.loads(subprocess.check_output(['hyprctl','activeworkspace','-j']))['id']
    used = {w['id'] for w in json.loads(subprocess.check_output(['hyprctl','workspaces','-j']))}
    test_workspace = next(i for i in range(9900,10000) if i not in used)
    launch = [str(Path.home()/'.local/bin/wiliwili-ai')] if installed else ['python3', str(ROOT/'launch.py')]
    log = open(ROOT/'validation/keyboard-client.log', 'w')
    process = subprocess.Popen([*launch, '--ai-test-media', str(ROOT/'validation/clip.mp4'),
        '--fixture', str(ROOT/'validation/fixture.json'), '--diagnostics', str(ui.STATE)], stdout=log, stderr=log)
    ui.bind_test_process(process)
    clipboard_type = None
    clipboard = None
    try:
        ui.wait(lambda d: d.get('ready') and not d.get('stopped'))
        ui.dispatch('hl.dsp.window.move({window="class:^wiliwili$",workspace='+str(test_workspace)+'})')
        ui.dispatch('hl.dsp.focus({workspace='+str(test_workspace)+'})')
        ui.dispatch('hl.dsp.window.float({window="class:^wiliwili$",action="enable"})')
        ui.dispatch('hl.dsp.window.resize({window="class:^wiliwili$",x=1400,y=900,relative=false})')
        ui.dispatch('hl.dsp.window.center({window="class:^wiliwili$"})')
        ui.dispatch('hl.dsp.focus({window="class:^wiliwili$"})')
        ui.key('space')
        ui.record('Space pauses real video', ui.wait(lambda d: d.get('paused')))
        ui.key('5'); ui.wait(lambda d: 99.5 <= d.get('position', 0) <= 100.5)
        ui.key('l'); ui.record('l seeks forward 5 seconds', ui.wait(lambda d: 104.5 <= d.get('position', 0) <= 105.5))
        ui.key('h'); ui.record('h seeks backward 5 seconds', ui.wait(lambda d: 99.5 <= d.get('position', 0) <= 100.5))
        ui.key('l', 'SHIFT'); ui.record('L seeks forward 30 seconds', ui.wait(lambda d: 129.5 <= d.get('position', 0) <= 130.5))
        ui.key('h', 'SHIFT'); ui.record('H seeks backward 30 seconds', ui.wait(lambda d: 99.5 <= d.get('position', 0) <= 100.5))
        ui.key('period'); ui.record('period increases persistent speed', ui.wait(lambda d: abs(d.get('speed', 0)-1.25)<.01))
        ui.key('comma'); ui.record('comma decreases persistent speed', ui.wait(lambda d: abs(d.get('speed', 0)-1)<.01))
        ui.key('comma'); ui.wait(lambda d: abs(d.get('speed', 0)-.75)<.01)
        ui.key('equal'); ui.record('equal resets persistent speed', ui.wait(lambda d: abs(d.get('speed', 0)-1)<.01))
        for _ in range(5): ui.key('comma')
        ui.record('Speed clamps at 0.25x', ui.wait(lambda d: abs(d.get('speed', 0)-.25)<.01))
        ui.key('equal')
        volume = ui.state()['volume']
        ui.key('j'); ui.record('j lowers video volume', ui.wait(lambda d: d.get('volume') == max(0, volume-5)))
        ui.key('k'); ui.record('k raises video volume', ui.wait(lambda d: d.get('volume') == volume))
        ui.key('slash'); ui.record('Slash opens desktop text input', ui.wait(lambda d: d.get('desktop', {}).get('editing')))
        text = 'fhjklgp'
        for char in text: ui.key(char)
        ui.record('Vim keys type normally in search; no playback shortcuts fire', ui.wait(lambda d:
            d.get('desktop', {}).get('input_text') == text and abs(d.get('speed',0)-1)<.01 and 99.5<=d.get('position',0)<=100.5))
        ui.key('a', 'CTRL'); ui.key('x')
        ui.record('Ctrl+A replaces the whole search text', ui.wait(lambda d: d.get('desktop', {}).get('input_text') == 'x'))
        ui.key('Home'); ui.key('h'); ui.key('End'); ui.key('l')
        ui.record('Home and End edit the caret position', ui.wait(lambda d: d.get('desktop', {}).get('input_text') == 'hxl'))
        # Preserve the user's clipboard without printing its contents.
        types = subprocess.run(['wl-paste', '--list-types'], capture_output=True, text=True).stdout.splitlines()
        if types:
            clipboard_type = 'image/png' if 'image/png' in types else types[0]
            saved = subprocess.run(['wl-paste', '--type', clipboard_type], capture_output=True)
            if saved.returncode == 0: clipboard = saved.stdout
        subprocess.run(['wl-copy', '--type', 'text/plain;charset=utf-8'], input='光合作用'.encode(), check=True)
        until = time.monotonic() + 3
        while subprocess.run(['wl-paste', '--no-newline'], capture_output=True).stdout != '光合作用'.encode():
            if time.monotonic() > until: raise AssertionError('Test clipboard ownership was not ready')
            time.sleep(.1)
        # wl-copy can briefly focus its Wayland surface while acquiring the selection.
        ui.dispatch('hl.dsp.focus({window="class:^wiliwili$"})')
        time.sleep(.4)
        ui.key('a', 'CTRL'); ui.key('v', 'CTRL')
        ui.record('Native search accepts Chinese text through Ctrl+V', ui.wait(lambda d: d.get('desktop', {}).get('input_text') == '光合作用'))
        screenshot('desktop-search-input.png')
        if clipboard is not None:
            subprocess.run(['wl-copy','--type',clipboard_type], input=clipboard, check=True)
            clipboard = None
        ui.key('Escape'); ui.wait(lambda d: not d.get('desktop', {}).get('editing'))
        ui.key('F8'); ui.record('F8 opens reader-first panel', ui.wait(lambda d: d.get('panel_tab') == '总结'))
        ui.record('Typography is reduced globally', ui.wait(lambda d: abs(d.get('desktop',{}).get('font_scale',0)-.82)<.01))
        ui.key('f'); hints = ui.wait(lambda d: d.get('desktop', {}).get('hint_mode'))['desktop']
        ui.record('f exposes native clickable controls', ui.wait(lambda d: d.get('desktop',{}).get('hint_count',0)>=6))
        screenshot('desktop-click-hints.png')
        ui.key('Escape'); ui.record('Esc cancels hints without closing the panel', ui.wait(lambda d: not d.get('desktop',{}).get('hint_mode') and d.get('panel_tab')=='总结'))
        # The nonmodal layout also exposes video controls. Select by visible text.
        ui.key('f'); hints = ui.wait(lambda d: d.get('desktop', {}).get('hint_mode'))['desktop']
        chapter_target = next(i for i, text in enumerate(hints['hint_texts']) if text.startswith('章节'))
        for char in hints['hint_labels'][chapter_target]: ui.key(char)
        ui.record('Hint letters activate the native chapters tab', ui.wait(lambda d: d.get('panel_tab')=='章节' and not d.get('desktop',{}).get('hint_mode')))
        ui.key('h'); ui.record('h returns to summary tab', ui.wait(lambda d: d.get('panel_tab')=='总结'))
        ui.key('F6'); ui.record('Real model returns readable summary and chapters', ui.wait(lambda d: d.get('summary') and len(d.get('segments',[]))>=3 and not d.get('busy'), 90))
        screenshot('desktop-summary-reader.png')
        ui.record('Completed summary remains at the top after layout and window focus', ui.wait(lambda d: d.get('reader_offset',100000)<5))
        ui.key('j'); ui.record('j scrolls summary reader without seeking video', ui.wait(lambda d: d.get('reader_offset',0)>20 and 99.5<=d.get('position',0)<=100.5))
        offset = ui.state()['reader_offset']
        ui.key('k'); ui.record('k scrolls reader up', ui.wait(lambda d: d.get('reader_offset',100000)<offset))
        ui.key('Next'); ui.record('Page Down scrolls long content', ui.wait(lambda d: d.get('reader_offset',0)>100))
        time.sleep(.8)  # Let the reader's scroll animation and diagnostic snapshot settle.
        offset = ui.state()['reader_offset']
        ui.key('2'); ui.wait(lambda d: d.get('panel_tab')=='章节')
        ui.key('1'); ui.record('Tab changes preserve each page reading position', ui.wait(lambda d: d.get('panel_tab')=='总结' and abs(d.get('reader_offset',0)-offset)<5))
        screenshot('desktop-summary-timeline.png')
        ui.key('F7'); ui.wait(lambda d: d.get('desktop',{}).get('editing'))
        for char in 'what does nadph do': ui.key('space' if char==' ' else char)
        ui.record('Question input retains focus while underlying tab updates', ui.wait(lambda d: d.get('desktop',{}).get('input_text')=='what does nadph do'))
        ui.key('Return'); ui.record('Question submits to real model from native input', ui.wait(lambda d: len(d.get('messages',[]))>=2 and not d.get('busy'),90))
        ui.wait(lambda d: d.get('panel_tab')=='问答')
        screenshot('desktop-chat-reader.png')
        ui.key('F8'); ui.record('F8 returns to the player', ui.wait(lambda d: 'video' in d.get('focus','')))
        ui.key('0'); ui.record('0 seeks back to video start', ui.wait(lambda d: 0<=d.get('position',-1)<=.5))
        ui.wait(lambda d:len(d.get('ads',[]))==1 and not d.get('busy'),60)
        ui.key('1');ui.wait(lambda d:19.5<=d.get('position',0)<=20.5)
        ui.key('F9');ui.record('Native advertising skip seeks real libmpv to the fixture ad end',ui.wait(lambda d:39.5<=d.get('position',0)<=40.5))
        ui.key('F10');ui.record('Want to watch restores the actual pre-skip position',ui.wait(lambda d:19.5<=d.get('position',0)<=20.5))
        ui.key('F10','CTRL');ui.record('Native human correction removes the model advertising marker',ui.wait(lambda d:not d.get('ads')))
        ui.key('m','CTRL');ui.wait(lambda d:d.get('marking_start',-1)>=19.5)
        ui.key('2');ui.wait(lambda d:39.5<=d.get('position',0)<=40.5)
        ui.key('m','CTRL');ui.record('Native manual correction persists a real playback-time interval',ui.wait(lambda d:any(a.get('source')=='manual' and 19.5<=a['start']<=20.5 and 39.5<=a['end']<=40.5 for a in d.get('ads',[]))))
        ui.key('F6');ui.wait(lambda d:d.get('busy'))
        before=ui.state()['generation'];ui.key('F12')
        ui.record('Video change cancels a real pending model request and clears previous identity',ui.wait(lambda d:d.get('generation',0)>before and d.get('identity')=='BVAItest0002:2:1' and not d.get('summary') and not d.get('ads'),60))
        time.sleep(3);ui.record('Late model results cannot restore previous video markers or seek its replacement',ui.wait(lambda d:d.get('identity')=='BVAItest0002:2:1' and not d.get('summary') and not d.get('ads') and d.get('position',999)<15))
    except Exception as error:
        ui.results.append({'check':'interrupted','result':'fail','error':str(error),'state':ui.state()})
        ui.REPORT.write_text(json.dumps(ui.results, ensure_ascii=False, indent=2))
        try: screenshot('desktop-keyboard-failure.png')
        except (StopIteration, subprocess.CalledProcessError): pass
        raise
    finally:
        if clipboard is not None:
            subprocess.run(['wl-copy','--type',clipboard_type], input=clipboard, check=True)
        ui.dispatch('hl.dsp.window.close({window="class:^wiliwili$"})')
        ui.dispatch('hl.dsp.focus({workspace='+str(previous_workspace)+'})')
        try: process.wait(10)
        except subprocess.TimeoutExpired: process.terminate(); process.wait(5)
        log.close()

if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--installed',action='store_true')
    run(parser.parse_args().installed)
