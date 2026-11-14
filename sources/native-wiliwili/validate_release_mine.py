#!/usr/bin/env python3
"""Authenticated native account/history/favorites UI; counts only, no private images."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import validate_ui as ui

ROOT=Path(__file__).resolve().parent
report=ROOT/'validation/release-3.0-account-ui.json'
results=[]
previous=json.loads(subprocess.check_output(['hyprctl','activeworkspace','-j']))['id']
used={w['id'] for w in json.loads(subprocess.check_output(['hyprctl','workspaces','-j']))}
workspace=next(i for i in range(9900,10000) if i not in used)

def grids():return ui.state().get('desktop',{}).get('grids',[])
def record(check):
    results.append({'check':check,'result':'pass','evidence':'real_authenticated_native_ui',
        'grids':[{'view':g['view'],'items':g['item_count']} for g in grids()],
        'auth':ui.state().get('auth',{})})
    report.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(results[-1],ensure_ascii=False),flush=True)

with tempfile.TemporaryDirectory(prefix='wiliwili-account-ui-') as temporary:
    private=Path(temporary);private.chmod(0o700)
    config=private/'config/wiliwili';config.mkdir(parents=True)
    settings={'cookie':json.loads((Path.home()/'.config/wiliwili/wiliwili_config.json').read_text()).get('cookie',{}),
        'setting':{'app_ui_scale':'1080p','search_tv_mode':False,'history_report':False}}
    (config/'wiliwili_config.json').write_text(json.dumps(settings));(config/'wiliwili_config.json').chmod(0o600)
    shutil.copyfile(Path.home()/'.config/wiliwili/android-session.json',config/'android-session.json');(config/'android-session.json').chmod(0o600)
    ui.STATE=private/'state.json'
    env=os.environ.copy();env.pop('WILI_AI_TOKEN',None)
    env.update(XDG_CONFIG_HOME=str(private/'config'),XDG_STATE_HOME=str(private/'state'),XDG_CACHE_HOME=str(private/'cache'),
        WILI_AI_DIAGNOSTICS=str(ui.STATE),WILI_ANDROID_SESSION=str(config/'android-session.json'))
    with (private/'client.log').open('w') as log:
        (private/'client.log').chmod(0o600)
        app=Path.home()/'.local/opt/wiliwili-ai'
        process=subprocess.Popen([str(app/'bin/wiliwili')],cwd=app,env=env,stdout=log,stderr=log)
        try:
            ui.bind_test_process(process)
            ui.dispatch('hl.dsp.window.move({window="class:^wiliwili$",workspace='+str(workspace)+'})')
            ui.dispatch('hl.dsp.focus({workspace='+str(workspace)+'})');ui.dispatch('hl.dsp.focus({window="class:^wiliwili$"})')
            ui.wait(lambda d:'desktop' in d)
            for _ in range(8):
                if ui.state().get('desktop',{}).get('focused_tab_label')=='首页':break
                ui.key('Left')
            ui.wait(lambda d:d.get('desktop',{}).get('focused_tab_label')=='首页')
            ui.key('Down')
            ui.wait(lambda d:d.get('desktop',{}).get('focused_tab_label')=='动态')
            # These grids show 30 skeleton cells while waiting for the service.
            # The real account fixtures below return 17 / 20 / 1 / 20+ entries.
            ui.wait(lambda d:any('dynamic/videoList' in g['view'] and g['item_count']>0 and g['item_count']!=30 for g in d.get('desktop',{}).get('grids',[])),45)
            record('Authenticated native dynamic page receives its video list')
            ui.key('Down')
            ui.wait(lambda d:d.get('desktop',{}).get('focused_tab_label')=='我的')
            ui.wait(lambda d:any('history' in g['view'] and g['item_count']>0 and g['item_count']!=30 for g in d.get('desktop',{}).get('grids',[])),45)
            record('Authenticated My page displays the real native history list')
            ui.key('Right');ui.key('Next')
            ui.wait(lambda d:d.get('desktop',{}).get('focused_tab_label')=='我的收藏')
            ui.wait(lambda d:any('collection' in g['view'] and 0<g['item_count']<30 for g in d.get('desktop',{}).get('grids',[])),45)
            record('Native favorites loads real folders after session reuse')
            ui.key('F5','CTRL');ui.wait(lambda d:d.get('auth',{}).get('stage')=='checked',45)
            for _ in range(2):
                if ui.state()['auth'].get('same_account') and ui.state()['auth'].get('web_valid'):break
                ui.key('f');d=ui.wait(lambda d:d.get('desktop',{}).get('hint_mode'))['desktop']
                index=d['hint_texts'].index('检查会话 / 复用授权')
                for char in d['hint_labels'][index]:ui.key(char)
                ui.wait(lambda d:d.get('auth',{}).get('stage')=='checked',45)
            assert ui.state()['auth']['same_account'] and ui.state()['auth']['web_valid']
            record('Native authorization remains reachable from authenticated account pages')
            ui.key('Escape');ui.wait(lambda d:not d.get('auth',{}).get('open'))
            ui.key('slash');ui.wait(lambda d:d.get('desktop',{}).get('editing'))
            for char in 'wiliwili':ui.key(char)
            ui.key('Return')
            ui.wait(lambda d:any('search' in g['view'] and g['item_count']>0 and g['item_count']!=30 for g in d.get('desktop',{}).get('grids',[])),50)
            record('Native keyword search returns real video results after login reuse')
            for _ in range(3):
                if not any('search' in g['view'] for g in ui.state().get('desktop',{}).get('grids',[])):break
                ui.key('Escape')
            ui.wait(lambda d:not any('search' in g['view'] for g in d.get('desktop',{}).get('grids',[])))
            ui.key('F5','CTRL');ui.wait(lambda d:d.get('auth',{}).get('stage')=='checked',45)
            ui.key('f');d=ui.wait(lambda d:d.get('desktop',{}).get('hint_mode'))['desktop']
            index=d['hint_texts'].index('退出全部登录')
            for char in d['hint_labels'][index]:ui.key(char)
            ui.wait(lambda d:d.get('auth',{}).get('stage')=='checked' and not d['auth'].get('web_valid') and not d['auth'].get('app_valid'),45)
            assert not (config/'android-session.json').exists() and not json.loads((config/'wiliwili_config.json').read_text()).get('cookie')
            ui.key('Escape');ui.wait(lambda d:not d.get('auth',{}).get('open'))
            def empty_private_lists(d):
                private_grids=[g for g in d.get('desktop',{}).get('grids',[]) if 'mine' in g['view']]
                return private_grids and all(g['item_count']==0 for g in private_grids)
            ui.wait(empty_private_lists,25)
            time.sleep(12)
            assert empty_private_lists(ui.state())
            record('Native sign-out clears favorites and late replies do not restore old account lists')
            ui.key('Prior')
            ui.wait(lambda d:any('mine/history' in g['view'] and g['item_count']==0 for g in d.get('desktop',{}).get('grids',[])),20)
            record('Previously loaded real history also remains empty after account sign-out')
            for _ in range(8):
                if ui.state().get('desktop',{}).get('focused_tab_label')=='我的':break
                ui.key('Left')
            ui.wait(lambda d:d.get('desktop',{}).get('focused_tab_label')=='我的')
            ui.key('Up')
            ui.wait(lambda d:d.get('desktop',{}).get('focused_tab_label')=='动态')
            ui.wait(lambda d:any('dynamic/videoList' in g['view'] for g in d.get('desktop',{}).get('grids',[])))
            assert all(g['item_count']==0 for g in grids() if 'dynamic/videoList' in g['view'])
            time.sleep(5)
            assert all(g['item_count']==0 for g in grids() if 'dynamic/videoList' in g['view'])
            record('Account sign-out also clears the old dynamic video list')
        except Exception as error:
            results.append({'check':'interrupted','result':'fail','error_type':type(error).__name__,
                'evidence':'real_authenticated_native_ui','auth':ui.state().get('auth',{}),'grids':[{'view':g['view'],'items':g['item_count']} for g in grids()]})
            report.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
            raise
        finally:
            if process.poll() is None:process.terminate()
            try:process.wait(10)
            except subprocess.TimeoutExpired:process.kill();process.wait(5)
            ui.dispatch('hl.dsp.focus({workspace='+str(previous)+'})')
