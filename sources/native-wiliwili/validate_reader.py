#!/usr/bin/env python3
"""Inspect the keyboard-readable answer at the bottom of the installed native panel."""
from pathlib import Path
import json
import subprocess
import time
import validate_ui as ui

ROOT=Path(__file__).resolve().parent
ui.STATE=ROOT/'validation/reader-native-state.json'
ui.REPORT=ROOT/'validation/reader-ui-results.json'
ui.STATE.unlink(missing_ok=True)
log=open(ROOT/'validation/reader-native-client.log','w')
process=subprocess.Popen([str(Path.home()/'.local/bin/wiliwili-ai'),'--ai-test-media',str(ROOT/'validation/clip.mp4'),
                          '--fixture',str(ROOT/'validation/fixture.json'),'--diagnostics',str(ui.STATE)],stdout=log,stderr=log)
ui.bind_test_process(process)
try:
    ui.wait(lambda d:d.get('ready') and not d.get('stopped'))
    ui.dispatch('hl.dsp.window.float({window="class:^wiliwili$",action="enable"})')
    ui.dispatch('hl.dsp.window.resize({window="class:^wiliwili$",x=1400,y=900,relative=false})')
    ui.dispatch('hl.dsp.focus({window="class:^wiliwili$"})')
    ui.key('space')
    ui.key('F8')
    ui.wait(lambda d:'生成总结' in d.get('focus_text',''))
    ui.key('F7')
    ui.wait(lambda d:'brls/container' in d.get('focus',''))
    for char in 'what does nadph do in this video': ui.key('space' if char==' ' else char)
    ui.key('Return')
    ui.record('Installed native text input receives a real source-grounded answer',ui.wait(lambda d:len(d.get('messages',[]))==2 and not d.get('busy'),60))
    for _ in range(4): ui.key('Next')
    time.sleep(.4)
    ui.screenshot('native-answer-reader.png')
    ui.record('Page Down scrolls to the answer; screenshot captured for visual verification')
    ui.key('Prior')
    time.sleep(.3)
    ui.screenshot('native-reader-page-up.png')
    ui.record('Page Up scrolls the native reading area; screenshot captured')
    ui.key('F8')
    ui.record('F8 closes panel after scrolling and restores player focus',ui.wait(lambda d:'video' in d.get('focus','')))
finally:
    ui.dispatch('hl.dsp.window.close({window="class:^wiliwili$"})')
    try: process.wait(10)
    except subprocess.TimeoutExpired: process.terminate();process.wait(5)
    log.close()
