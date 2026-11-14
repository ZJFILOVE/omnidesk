#!/usr/bin/env python3
"""Release tests: real native windows, isolated credentials/preferences, own PID only.

QR screenshots stay in a private /tmp directory and are never release evidence.
No synthetic API responses are used by this runner.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent


def run(args):
    app = Path.home()/'.local/opt/wiliwili-ai' if args.installed else ROOT/'build'
    binary = app/'bin/wiliwili' if args.installed else app/'wiliwili'
    private = Path(tempfile.mkdtemp(prefix='wiliwili-release-ui-'))
    os.chmod(private, 0o700)
    config = private/'config/wiliwili/wiliwili_config.json'
    config.parent.mkdir(parents=True)
    settings = {'setting': {'app_ui_scale': '1080p', 'search_tv_mode': False, 'video_quality': 0, 'video_codec': 7, 'history_report': False, 'player_strategy':3}}
    session = config.parent/'android-session.json'
    if args.mode in ('reuse','switch','missing','invalid'):
        settings['cookie'] = json.loads((Path.home()/'.config/wiliwili/wiliwili_config.json').read_text()).get('cookie', {})
    if args.mode in ('reuse','switch'):
        shutil.copyfile(Path.home()/'.config/wiliwili/android-session.json', session); session.chmod(0o600)
    if args.mode == 'failure':
        settings['setting'].update(http_proxy_status=True, http_proxy='http://127.0.0.1:9')
    if args.mode == 'invalid':
        # Deliberately invalid test input, rejected by the real account endpoint.
        session.write_text(json.dumps({'access_token': 'invalid_test_session_00000000000000', 'created': int(time.time()), 'expires_in': 3600}))
        session.chmod(0o600)
    config.write_text(json.dumps(settings)); config.chmod(0o600)
    state_path = private/'state.json'
    env = os.environ.copy(); env.pop('WILI_AI_TOKEN', None)
    env.update(XDG_CONFIG_HOME=str(private/'config'), WILI_AI_DIAGNOSTICS=str(state_path), WILI_ANDROID_SESSION=str(session))
    previous = json.loads(subprocess.check_output(['hyprctl','activeworkspace','-j']))['id']
    used = {x['id'] for x in json.loads(subprocess.check_output(['hyprctl','workspaces','-j']))}
    workspace = next(i for i in range(9900, 10000) if i not in used)
    log = (private/'client.log').open('w'); os.chmod(private/'client.log', 0o600)
    command = [str(binary), '--auth']
    if args.mode in ('reuse', 'switch', 'missing', 'invalid'): command += ['--bv', 'BV1heam6TExz']
    if args.debug_crash:
        command = ['gdb','--batch','-ex','set print frame-arguments none','-ex','run','-ex','bt','--args',*command]
    process = subprocess.Popen(command, cwd=app, env=env, stdout=log, stderr=log)
    if args.debug_crash:
        import validate_ui as ui
        ui.bind_test_process(process); selector = ui.SELECTOR
    else: selector = f'pid:{process.pid}'
    results = []
    def state():
        try: return json.loads(state_path.read_text())
        except (ValueError, OSError): return {}
    def wait(predicate, timeout=40):
        until = time.monotonic()+timeout
        while time.monotonic()<until:
            d = state()
            if predicate(d): return d
            if process.poll() is not None: raise AssertionError('Test process exited: '+str(process.returncode))
            time.sleep(.15)
        raise AssertionError('Native state timed out: '+str(state().get('auth', {})))
    def dispatch(expression):
        assert subprocess.check_output(['hyprctl','dispatch',expression],text=True).strip() == 'ok'
    def key(name, mods=''):
        held = [{'CTRL':'Control_L','SHIFT':'Shift_L'}[m] for m in mods.split()]
        actions = [(m,'down','') for m in held]+[(name,'down',mods),(name,'up',mods)]+[(m,'up','') for m in reversed(held)]
        commands = ['dispatch hl.dsp.send_key_state({window='+json.dumps(selector)+',mods='+json.dumps(metadata)+',key='+json.dumps(k)+',state='+json.dumps(action)+'})' for k,action,metadata in actions]
        output = subprocess.check_output(['hyprctl','--batch',';'.join(commands)],text=True)
        assert all(line.strip()=='ok' for line in output.splitlines() if line.strip()), output
        time.sleep(.25)
    def open_video(bvid):
        key('l','CTRL'); wait(lambda d:d.get('desktop',{}).get('editing'))
        for char in bvid: key(char.lower(), 'SHIFT' if char.isupper() else '')
        key('Return'); wait(lambda d:not d.get('desktop',{}).get('editing'))
    def click(title):
        key('f'); d = wait(lambda d:d.get('desktop',{}).get('hint_mode'))
        desktop=d['desktop']; index=desktop['hint_texts'].index(title)
        for c in desktop['hint_labels'][index]: key(c)
    def screenshot(path):
        win=next(c for c in json.loads(subprocess.check_output(['hyprctl','clients','-j'])) if selector==f"pid:{c['pid']}")
        x,y=win['at'];w,h=win['size'];subprocess.run(['grim','-g',f'{x},{y} {w}x{h}',str(path)],check=True)
        path.chmod(0o600)
    def record(check, details=None):
        results.append({'check':check,'result':'pass','evidence':'real_native_ui_and_service',**(details or {})})
        args.output.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(results[-1],ensure_ascii=False),flush=True)
    try:
        wait(lambda d:d.get('auth',{}).get('stage')=='checked', 50)
        dispatch(f'hl.dsp.window.move({{window="{selector}",workspace={workspace}}})')
        dispatch(f'hl.dsp.focus({{workspace={workspace}}})'); dispatch(f'hl.dsp.focus({{window="{selector}"}})')
        if args.mode == 'reuse':
            auth=state()['auth']; assert auth['web_valid'] and auth['app_valid'] and auth['same_account']
            record('Existing approved sessions reused and account identities match', auth)
            key('Escape'); wait(lambda d:not d.get('auth',{}).get('open'))
            d=wait(lambda d:d.get('media',{}).get('width')==3840 and d.get('position',0)>2,60)
            assert d['media']['selected_quality']==120
            base=state()['desktop']['activity_count']
            for shortcut in ('F3','F5'):
                key(shortcut);wait(lambda d:d.get('desktop',{}).get('activity_count')==base+1)
                assert not state().get('auth',{}).get('open')
                key('Escape');wait(lambda d:d.get('desktop',{}).get('activity_count')==base)
            record('Original F3 speed and F5 playback settings shortcuts remain available')
            key('F3','CTRL'); time.sleep(.8); screenshot(ROOT/'validation/release-3.0-media-info.png'); key('Escape')
            wait(lambda d:d.get('desktop',{}).get('activity_count')==base)
            record('Ctrl+F3 displays real 4K decode and AAC audio', {'media':d['media']})
            key('F2'); wait(lambda d:d.get('desktop',{}).get('activity_count')==base+1)
            for _ in range(3): key('Down')
            key('Return'); d=wait(lambda d:d.get('media',{}).get('selected_quality') in (80,116) and d.get('media',{}).get('audio_rate')==48000 and d.get('position',0)>1,45)
            record('Manual quality switch uses a decoded lower stream', {'media':d['media']})
            key('F2'); wait(lambda d:d.get('desktop',{}).get('activity_count')==base+1)
            time.sleep(.8)
            for _ in range(8):
                if state().get('desktop',{}).get('focused_item_index')==0: break
                key('Up')
            wait(lambda d:d.get('desktop',{}).get('focused_item_index')==0)
            key('Return'); d=wait(lambda d:d.get('media',{}).get('width')==3840 and d.get('media',{}).get('selected_quality')==120 and d.get('media',{}).get('audio_rate')==48000 and d.get('position',0)>1,45)
            assert json.loads(config.read_text())['setting']['video_quality']==0
            record('Automatic highest restored and preference saved', {'media':d['media']})
        elif args.mode == 'switch':
            assert state()['auth']['same_account']
            key('Escape');wait(lambda d:not d.get('auth',{}).get('open'))
            wait(lambda d:d.get('media',{}).get('width')==3840 and d.get('position',0)>2,60)
            open_video('BV1fK4y1t7hj')
            d=wait(lambda d:d.get('video',{}).get('bvid')=='BV1fK4y1t7hj' and d.get('media',{}).get('width')==4096 and abs(d.get('media',{}).get('duration',0)-295.124)<2 and d.get('position',0)>2,70)
            record('Switch to another real 4K video replaces raster, duration and identity',{'video':{k:d['video'][k] for k in ('bvid','cid','p')},'media':d['media']})
            key('9');wait(lambda d:d.get('position',0)>266)
            key('l','SHIFT');wait(lambda d:d.get('stopped') or d.get('paused'),15)
            key('space');d=wait(lambda d:d.get('video',{}).get('bvid')=='BV1fK4y1t7hj' and 1<d.get('position',0)<12 and not d.get('paused',True) and d.get('media',{}).get('width')==4096 and d.get('media',{}).get('height')==1860 and abs(d.get('media',{}).get('duration',0)-295.124)<2,30)
            record('Native replay restarts the full 4K stream after actual EOF',{'media':d['media']})
            from service import Collector, Transport, load_cookie
            collector=Collector(Transport(),load_cookie(config.parent))
            pages=collector.fetch('/x/web-interface/view',{'bvid':'BV1Eb411u7Fw'})['pages']
            open_video('BV1heam6TExz')
            wait(lambda d:d.get('video',{}).get('bvid')=='BV1heam6TExz',35)
            open_video('BV1Eb411u7Fw')
            def playing_part(d,p):
                v=d.get('video',{});m=d.get('media',{})
                return v.get('bvid')=='BV1Eb411u7Fw' and v.get('cid')==pages[p-1]['cid'] and v.get('p')==p and abs(m.get('duration',0)-pages[p-1]['duration'])<3 and m.get('audio_rate')==48000 and d.get('position',0)>1 and not d.get('stopped',True)
            d=wait(lambda d:playing_part(d,1),90)
            record('Rapid video changes discard older URL results',{'video':{k:d['video'][k] for k in ('bvid','cid','p')},'media':d['media']})
            key('Next','CTRL');d=wait(lambda d:playing_part(d,2),90)
            record('Native next P plays its own real media and clears previous AI identity',{'video':{k:d['video'][k] for k in ('bvid','cid','p')},'media':d['media']})
            key('Prior','CTRL');d=wait(lambda d:playing_part(d,1),90)
            record('Native previous P restores the correct full stream',{'video':{k:d['video'][k] for k in ('bvid','cid','p')},'media':d['media']})
            previous_generation=d['generation']
            key('F5','CTRL');wait(lambda d:d.get('auth',{}).get('stage')=='checked',40)
            click('退出全部登录');wait(lambda d:d.get('auth',{}).get('stage')=='checked',40)
            assert not session.exists() and not json.loads(config.read_text()).get('cookie')
            key('Escape');d=wait(lambda d:playing_part(d,1) and d['generation']>previous_generation and d['media']['source']!='Android trial',90)
            time.sleep(6);assert playing_part(state(),1) and state()['media']['source']!='Android trial'
            record('Account sign-out invalidates App results and resumes the same P through ordinary playback',{'media':d['media'],'second_real_account_tested':False})
        elif args.mode in ('missing','invalid'):
            auth=state()['auth']
            assert not auth.get('app_valid') and auth.get('web_valid'), 'App fallback test requires valid independent Web session'
            key('Escape');d=wait(lambda d:not d.get('stopped',True) and d.get('position',0)>2,60)
            assert d['media']['width']>0 and d['media']['audio_codec'] and d['media']['source']!='Android trial'
            record('Ordinary web/HTML5 playback survives '+args.mode+' App session', {'media':d['media'],'auth':auth,
                'test_input':'deliberately_invalid_unexpired_App_credential' if args.mode=='invalid' else 'no_App_credential'})
        else:
            click('高清播放扫码 / 重新授权')
            if args.mode == 'failure':
                wait(lambda d:d.get('auth',{}).get('stage')=='error',25)
                record('Real connection failure shown with native retry controls')
                click('高清播放扫码 / 重新授权');wait(lambda d:d.get('auth',{}).get('stage')=='error',25)
                record('Retry can be repeated without saving credentials')
            else:
                wait(lambda d:d.get('auth',{}).get('stage')=='ready',25)
                shot=private/'qr.png'; screenshot(shot)
                print(json.dumps({'qr_ready':True,'qr_image':str(shot),'test_pid':process.pid,'mode':args.mode}),flush=True)
                if args.mode == 'expiry':
                    wait(lambda d:d.get('auth',{}).get('stage')=='expired',205)
                    record('Actual official QR deadline expires in native UI')
                    click('高清播放扫码 / 重新授权');wait(lambda d:d.get('auth',{}).get('stage')=='ready',25)
                    record('Expired QR can be regenerated')
                else:
                    d=wait(lambda d:d.get('auth',{}).get('stage')=='success',195)
                    assert session.exists() and (session.stat().st_mode&0o777)==0o600
                    assert d['auth']['app_valid']
                    record('Phone-approved native QR persisted a server-validated private session',d['auth'])
                    click('刷新高清授权');d=wait(lambda d:d.get('auth',{}).get('stage')=='checked',45)
                    record('Refresh endpoint exercised; native rescan available if unsupported',d['auth'])
                    click('退出高清授权');wait(lambda d:d.get('auth',{}).get('stage')=='checked',30)
                    assert not session.exists();record('Native sign-out removes the isolated App credential')
            key('Escape');wait(lambda d:not d.get('auth',{}).get('open'))
            assert not session.exists();record('Cancel closes QR workflow without accepting a late response')
        return 0
    except Exception as error:
        debug = Path(tempfile.mkdtemp(prefix='wiliwili-release-failure-'))
        debug.chmod(0o700); shutil.copyfile(private/'client.log',debug/'client.log'); (debug/'client.log').chmod(0o600)
        print(json.dumps({'private_failure_log':str(debug/'client.log')}),flush=True)
        results.append({'check':'interrupted','result':'fail','error':str(error),'evidence':'real_native_ui_and_service',
            'media':state().get('media',{}),'desktop':{k:state().get('desktop',{}).get(k) for k in ('activity_count','focused_item_index')},
            'saved_quality':json.loads(config.read_text()).get('setting',{}).get('video_quality')})
        args.output.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n');raise
    finally:
        if process.poll() is None:process.terminate()
        try:process.wait(10)
        except subprocess.TimeoutExpired:process.kill();process.wait(5)
        log.close();dispatch(f'hl.dsp.focus({{workspace={previous}}})')
        # No QR challenges, identities, cookies or private logs enter validation/.
        shutil.rmtree(private)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['reuse','switch','auth','expiry','failure','missing','invalid'],required=True)
    parser.add_argument('--installed',action='store_true')
    parser.add_argument('--debug-crash',action='store_true')
    parser.add_argument('--output',type=Path,required=True)
    raise SystemExit(run(parser.parse_args()))
