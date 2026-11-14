#!/usr/bin/env python3
"""Automatic skip in real native playback; clearly labelled fixture and real model."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import validate_ui as ui

ROOT=Path(__file__).resolve().parent
report=ROOT/'validation/release-3.0-auto-skip.json'
results=[]
previous=json.loads(subprocess.check_output(['hyprctl','activeworkspace','-j']))['id']
used={w['id'] for w in json.loads(subprocess.check_output(['hyprctl','workspaces','-j']))}
workspace=next(i for i in range(9900,10000) if i not in used)
with tempfile.TemporaryDirectory(prefix='wiliwili-ad-validation-') as temporary:
    private=Path(temporary);private.chmod(0o700);ui.STATE=private/'state.json'
    with (private/'client.log').open('w') as log:
        (private/'client.log').chmod(0o600)
        process=subprocess.Popen([str(Path.home()/'.local/bin/wiliwili-ai'),'--ai-test-media',str(ROOT/'validation/clip.mp4'),
            '--fixture',str(ROOT/'validation/fixture.json'),'--diagnostics',str(ui.STATE)],stdout=log,stderr=log)
        try:
            ui.bind_test_process(process)
            ui.dispatch('hl.dsp.window.move({window="class:^wiliwili$",workspace='+str(workspace)+'})')
            ui.dispatch('hl.dsp.focus({workspace='+str(workspace)+'})');ui.dispatch('hl.dsp.focus({window="class:^wiliwili$"})')
            ui.wait(lambda d:d.get('ready') and not d.get('stopped'))
            ui.key('space');ui.wait(lambda d:d.get('paused'))
            ui.wait(lambda d:len(d.get('ads',[]))==1 and d['ads'][0]['confidence']>=.7 and not d.get('busy'),90)
            ui.key('1');ui.wait(lambda d:19.5<=d.get('position',0)<=20.5)
            started=time.monotonic();ui.key('space')
            state=ui.wait(lambda d:d.get('position',0)>=40 and '自动跳过' in d.get('toast',''),12)
            elapsed=time.monotonic()-started
            assert elapsed<2.5
            results.append({'check':'Zero-latency automatic skip triggers immediately at ad start in real libmpv',
                'result':'pass','elapsed_seconds':round(elapsed,2),'position_after_skip':state['position'],
                'evidence':'explicit_local_fixture_and_real_existing_model_with_native_libmpv'})
            ui.key('space');ui.wait(lambda d:d.get('paused'))
            ui.key('F10');state=ui.wait(lambda d:20<=d.get('position',0)<27)
            results.append({'check':'Want to watch restores the position before automatic skip',
                'result':'pass','restored_position':state['position'],
                'evidence':'explicit_local_fixture_and_real_existing_model_with_native_libmpv'})
        except Exception as error:
            results.append({'check':'interrupted','result':'fail','error_type':type(error).__name__,
                'evidence':'explicit_local_fixture_and_real_existing_model_with_native_libmpv'})
            raise
        finally:
            report.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n');print(json.dumps(results,ensure_ascii=False),flush=True)
            if process.poll() is None:ui.dispatch('hl.dsp.window.close({window="class:^wiliwili$"})')
            try:process.wait(10)
            except subprocess.TimeoutExpired:process.terminate();process.wait(5)
            ui.dispatch('hl.dsp.focus({workspace='+str(previous)+'})')
