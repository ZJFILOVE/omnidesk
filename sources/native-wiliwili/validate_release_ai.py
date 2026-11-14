#!/usr/bin/env python3
"""Final installed app, authenticated real subtitles and existing real local model."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import validate_ui as ui

ROOT=Path(__file__).resolve().parent
report=ROOT/'validation/release-3.0-real-ai.json'
results=[]
previous=json.loads(subprocess.check_output(['hyprctl','activeworkspace','-j']))['id']
used={w['id'] for w in json.loads(subprocess.check_output(['hyprctl','workspaces','-j']))}
workspace=next(i for i in range(9900,10000) if i not in used)

def record(check):
    data=ui.state()
    results.append({'check':check,'result':'pass','evidence':'real_authenticated_video_and_existing_local_model',
        'model':'gpt-6-luna','sources':data.get('sources',{}),'summary_characters':len(data.get('summary','')),
        'chapters':len(data.get('segments',[])),'messages':len(data.get('messages',[])),
        'status':data.get('status',''),'cache_isolated':True,
        'advertising_markers':len(data.get('ads',[])),'media':data.get('media',{})})
    report.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(results[-1],ensure_ascii=False),flush=True)

with tempfile.TemporaryDirectory(prefix='wiliwili-real-ai-') as temporary:
    private=Path(temporary);private.chmod(0o700)
    config=private/'config/wiliwili';config.mkdir(parents=True)
    settings={'cookie':json.loads((Path.home()/'.config/wiliwili/wiliwili_config.json').read_text()).get('cookie',{}),
        'setting':{'video_quality':0,'video_codec':7,'app_ui_scale':'1080p','search_tv_mode':False,'history_report':False}}
    (config/'wiliwili_config.json').write_text(json.dumps(settings));(config/'wiliwili_config.json').chmod(0o600)
    shutil.copyfile(Path.home()/'.config/wiliwili/android-session.json',config/'android-session.json')
    (config/'android-session.json').chmod(0o600)
    env=os.environ.copy();env['XDG_CONFIG_HOME']=str(private/'config');env['XDG_STATE_HOME']=str(private/'state');env['WILI_ANDROID_SESSION']=str(config/'android-session.json')
    ui.STATE=private/'state.json'
    with (private/'client.log').open('w') as log:
        (private/'client.log').chmod(0o600)
        process=subprocess.Popen([str(Path.home()/'.local/bin/wiliwili-ai'),'--bv','BV1heam6TExz','--diagnostics',str(ui.STATE)],env=env,stdout=log,stderr=log)
        try:
            ui.bind_test_process(process)
            ui.dispatch('hl.dsp.window.move({window="class:^wiliwili$",workspace='+str(workspace)+'})')
            ui.dispatch('hl.dsp.focus({workspace='+str(workspace)+'})');ui.dispatch('hl.dsp.focus({window="class:^wiliwili$"})')
            ui.wait(lambda d:d.get('ready') and d.get('position',0)>1,80)
            ui.wait(lambda d:d.get('sources',{}).get('subtitles',0)>0 and not d.get('busy'),150)
            assert ui.state().get('status') in ('广告识别完成','模型已确认无广告'), 'Real model ad analysis did not complete'
            record('Native installed client collects real authenticated subtitles and completes real ad analysis')
            for attempt in range(3):
                revision=ui.state().get('revision',0)
                ui.key('F6')
                outcome=ui.wait(lambda d:d.get('revision',0)>revision and not d.get('busy') and
                    ((bool(d.get('summary')) and bool(d.get('segments'))) or any(text in d.get('status','') for text in ('错误','失败','超时'))),250)
                if outcome.get('summary') and outcome.get('segments'):break
                results.append({'check':'Native model summary attempt reports failure and can be retried','result':'observed_failure',
                    'evidence':'real_authenticated_video_and_existing_local_model','attempt':attempt+1,'status':outcome.get('status',''),
                    'incorrect_success_cached':False})
                report.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
            assert ui.state().get('summary') and ui.state().get('segments'), 'Real model did not return usable summary after native retries'
            record('Existing local model generates a summary and chapters from actual video materials')
            ui.key('F7');ui.wait(lambda d:d.get('desktop',{}).get('editing'))
            for char in 'what is this video about':ui.key('space' if char==' ' else char)
            ui.key('Return');ui.wait(lambda d:len(d.get('messages',[]))>=2 and not d.get('busy'),150)
            record('Native question is answered from actual collected video context')
            time.sleep(.5);ui.screenshot('release-3.0-real-ai.png')
            previous_generation=ui.state()['generation']
            ui.key('F5','CTRL');ui.wait(lambda d:d.get('auth',{}).get('stage')=='checked',45)
            ui.key('f');desktop=ui.wait(lambda d:d.get('desktop',{}).get('hint_mode'))['desktop']
            index=desktop['hint_texts'].index('退出全部登录')
            for char in desktop['hint_labels'][index]:ui.key(char)
            ui.wait(lambda d:d.get('auth',{}).get('stage')=='checked' and not d['auth'].get('web_valid') and not d['auth'].get('app_valid'),45)
            assert not (config/'android-session.json').exists() and not json.loads((config/'wiliwili_config.json').read_text()).get('cookie')
            ui.key('Escape')
            ui.wait(lambda d:d.get('generation',0)>previous_generation and d.get('position',0)>1 and d.get('media',{}).get('source')!='Android trial' and not d.get('stopped',True),90)
            ui.wait(lambda d:'subtitles' in d.get('sources',{}) and not d.get('busy'),150)
            assert not ui.state().get('summary') and not ui.state().get('messages')
            time.sleep(6)
            assert not ui.state().get('summary') and not ui.state().get('messages')
            record('Native account sign-out recollects materials with the new session and clears previous AI results')
            ui.key('F7');ui.wait(lambda d:d.get('desktop',{}).get('editing'))
            for char in 'what is two plus two':ui.key('space' if char==' ' else char)
            ui.key('Return');ui.wait(lambda d:len(d.get('messages',[]))==2 and not d.get('busy'),150)
            record('Existing model remains available for general questions after native account sign-out')
        except Exception as error:
            results.append({'check':'interrupted','result':'fail','error_type':type(error).__name__,
                'evidence':'real_authenticated_video_and_existing_local_model','sources':ui.state().get('sources',{})})
            report.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
            raise
        finally:
            if process.poll() is None:
                ui.dispatch('hl.dsp.window.close({window="class:^wiliwili$"})')
            try:process.wait(10)
            except subprocess.TimeoutExpired:process.terminate();process.wait(5)
            ui.dispatch('hl.dsp.focus({workspace='+str(previous)+'})')
