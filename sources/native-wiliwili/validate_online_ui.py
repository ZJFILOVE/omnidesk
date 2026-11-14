#!/usr/bin/env python3
"""Native PlayerActivity and real public Bilibili playback; no browser login import."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import validate_ui as ui

ROOT=Path(__file__).resolve().parent
ui.STATE=ROOT/'validation/online-native-state.json'
ui.REPORT=ROOT/'validation/online-ui-results.json'

def run(installed=False):
    videos=json.loads((ROOT/'validation/online-results.json').read_text())
    first,second=[row['video'] for row in videos if 'video' in row][:2]
    ui.STATE.unlink(missing_ok=True)
    log=open(ROOT/'validation/online-native-client.log','w')
    launcher=[str(Path.home()/'.local/bin/wiliwili-ai')] if installed else ['python3',str(ROOT/'launch.py')]
    process=subprocess.Popen([*launcher,'--bv',first['bvid'],
                              '--diagnostics',str(ui.STATE)],stdout=log,stderr=log)
    ui.bind_test_process(process)
    try:
        ui.wait(lambda d:d.get('ready') and d.get('position',0)>0,60)
        ui.dispatch('hl.dsp.window.float({window="class:^wiliwili$",action="enable"})')
        ui.dispatch('hl.dsp.window.resize({window="class:^wiliwili$",x=1400,y=900,relative=false})')
        ui.dispatch('hl.dsp.focus({window="class:^wiliwili$"})')
        ui.record('Real unauthenticated Bilibili stream plays in native libmpv')
        ui.key('space')
        ui.record('Space pauses actual Bilibili stream',ui.wait(lambda d:d.get('paused')))
        position=ui.state()['position']
        ui.key('bracketright')
        ui.record('Keyboard seeks actual Bilibili stream by five seconds',ui.wait(lambda d:d.get('position',0)>=position+4))
        ui.record('Real video metadata, danmaku and comments collected',ui.wait(lambda d:d.get('sources',{}).get('danmaku',0)>0 and d.get('sources',{}).get('comments',0)>0 and not d.get('busy'),60))
        ui.record('No subtitles remains unavailable, without advertising markers',ui.wait(lambda d:'无字幕' in d.get('status','') and not d.get('ads')))
        ui.key('F8')
        ui.record('F8 native AI panel on real PlayerActivity',ui.wait(lambda d:'生成总结' in d.get('focus_text','')))
        ui.screenshot('online-native-panel.png')
        ui.key('Escape')
        ui.key('l','CTRL')
        ui.wait(lambda d:'brls/container' in d.get('focus',''))
        # BV is case-sensitive; type it with a genuine native keyboard text input.
        for char in second['bvid']:
            ui.key(char.lower(), 'SHIFT' if char.isupper() else '')
        ui.key('Return')
        ui.record('Ctrl L opens a second real Bilibili video',ui.wait(lambda d:d.get('identity','').startswith(second['bvid']+':') and d.get('ready'),60))
        ui.record('Real video switch changes generation and clears old results',ui.wait(lambda d:d.get('video',{}).get('cid')==second['cid'] and not d.get('ads') and not d.get('summary')))
        ui.record('Second actual video plays after keyboard switch',ui.wait(lambda d:d.get('identity','').startswith(second['bvid']+':') and not d.get('stopped') and d.get('position',0)>0,60))
        ui.screenshot('online-native-player.png')
    except Exception as error:
        ui.results.append(dict(check='interrupted',result='fail',error=str(error),state=ui.state()))
        ui.REPORT.write_text(json.dumps(ui.results,ensure_ascii=False,indent=2))
        ui.screenshot('online-native-failure.png')
        raise
    finally:
        ui.dispatch('hl.dsp.window.close({window="class:^wiliwili$"})')
        try: process.wait(10)
        except subprocess.TimeoutExpired: process.terminate();process.wait(5)
        log.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--installed',action='store_true')
    run(parser.parse_args().installed)
