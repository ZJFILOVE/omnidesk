#!/usr/bin/env python3
"""Verify native cancellation reaches the real bridge's running Codex CLI processes."""
import json
from pathlib import Path
import re
import subprocess
import time
import validate_ui as ui

ROOT=Path(__file__).resolve().parent
ui.STATE=ROOT/'validation/cancel-native-state.json'
ui.REPORT=ROOT/'validation/transport-cancel-results.json'
listeners=subprocess.check_output(['ss','-ltnp','sport = :43188'],text=True)
bridge=int(re.search(r'pid=(\d+)',listeners).group(1))
def children():
    result=set()
    # Linux records forked children against the thread that called Popen;
    # HTTP model requests run in bridge worker threads, rather than its main thread.
    for path in Path(f'/proc/{bridge}/task').glob('*/children'):
        try: result.update(int(value) for value in path.read_text().split())
        except OSError: pass
    return result
initial=children()
seen=set(initial)
def pending():
    until=time.monotonic()+30
    while time.monotonic()<until:
        active=children()-seen
        if active and ui.state().get('busy'):
            seen.update(active)
            return active
        time.sleep(.05)
    raise AssertionError('No new real Codex CLI process started')
def cancelled(pids):
    until=time.monotonic()+5
    while time.monotonic()<until:
        if not any(Path(f'/proc/{pid}').exists() for pid in pids): return
        time.sleep(.05)
    raise AssertionError('Cancelled request still owns a real Codex CLI process')
ui.STATE.unlink(missing_ok=True)
log=open(ROOT/'validation/cancel-native-client.log','w')
process=subprocess.Popen([str(Path.home()/'.local/bin/wiliwili-ai'),'--ai-test-media',str(ROOT/'validation/clip.mp4'),
                          '--fixture',str(ROOT/'validation/fixture.json'),'--diagnostics',str(ui.STATE)],stdout=log,stderr=log)
ui.bind_test_process(process)
try:
    ui.wait(lambda d:d.get('ready'))
    ui.dispatch('hl.dsp.focus({window="class:^wiliwili$"})')
    ui.key('space')
    active=pending()
    ui.record('Real model CLI is running before native video switch')
    ui.key('F12')
    ui.wait(lambda d:d.get('identity')=='BVAItest0002:2:1')
    cancelled(active)
    ui.record('Native video switch terminates the old real Codex CLI')
    active=pending()
    ui.key('a','CTRL SHIFT')
    ui.wait(lambda d:d.get('enabled') is False)
    cancelled(active)
    ui.record('Disabling native AI terminates its real Codex CLI')
    ui.key('a','CTRL SHIFT')
    ui.wait(lambda d:d.get('enabled') is True)
    active=pending()
    ui.key('F11')
    ui.wait(lambda d:d.get('identity')=='BVAItest0001:3:2')
    cancelled(active)
    ui.record('Changing cid and P terminates the previous real Codex CLI')
    active=pending()
    ui.record('Real Codex CLI is running immediately before client exit')
    ui.dispatch('hl.dsp.window.close({window="class:^wiliwili$"})')
    process.wait(10)
    cancelled(active)
    ui.record('Exiting native client terminates its real Codex CLI',dict(client_exit_code=process.returncode))
finally:
    if process.poll() is None:
        ui.dispatch('hl.dsp.window.close({window="class:^wiliwili$"})')
        try: process.wait(10)
        except subprocess.TimeoutExpired: process.terminate();process.wait(5)
    log.close()
