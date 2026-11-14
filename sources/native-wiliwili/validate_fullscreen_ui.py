#!/usr/bin/env python3
"""Fullscreen/typography regression using a separate PID and isolated preferences.

Local media and native libmpv; no model requests or changes to the user's player.
Pass --real to review a public Bilibili player, or --installed after installation.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent


def run(installed=False, real=False):
    suffix = 'real' if real else 'local'
    state_path = ROOT / f'validation/fullscreen-{suffix}-state.json'
    report_path = ROOT / f'validation/fullscreen-{suffix}-results.json'
    state_path.unlink(missing_ok=True)
    results = []
    previous = json.loads(subprocess.check_output(['hyprctl', 'activeworkspace', '-j']))['id']
    used = {w['id'] for w in json.loads(subprocess.check_output(['hyprctl', 'workspaces', '-j']))}
    workspace = next(i for i in range(9900, 10000) if i not in used)
    app = Path.home() / '.local/opt/wiliwili-ai' if installed else ROOT / 'build'
    binary = app / 'bin/wiliwili' if installed else app / 'wiliwili'

    def state():
        try:
            return json.loads(state_path.read_text())
        except (OSError, ValueError):
            return {}

    def wait(predicate, timeout=12):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            data = state()
            if predicate(data):
                return data
            time.sleep(.1)
        raise AssertionError('Timed out: ' + json.dumps(state(), ensure_ascii=False))

    def dispatch(expression):
        result = subprocess.run(['hyprctl', 'dispatch', expression], capture_output=True, text=True, check=True)
        if result.stdout.strip() != 'ok':
            raise AssertionError(result.stdout)

    def window():
        return next(c for c in json.loads(subprocess.check_output(['hyprctl', 'clients', '-j']))
                    if c['pid'] == process.pid)

    def key(name, mods=''):
        held = [{'CTRL': 'Control_L', 'SHIFT': 'Shift_L'}[m] for m in mods.split()]
        commands = []
        for k, action, metadata in ([ (m, 'down', '') for m in held ]
                                    + [(name, 'down', mods), (name, 'up', mods)]
                                    + [(m, 'up', '') for m in reversed(held)]):
            commands.append('dispatch hl.dsp.send_key_state({window=' + json.dumps(selector)
                            + ',mods=' + json.dumps(metadata) + ',key=' + json.dumps(k)
                            + ',state=' + json.dumps(action) + '})')
        output = subprocess.check_output(['hyprctl', '--batch', ';'.join(commands)], text=True)
        assert all(line.strip() == 'ok' for line in output.splitlines() if line.strip()), output
        time.sleep(.35)

    def screenshot(name):
        time.sleep(.5)
        win = window()
        x, y = win['at']; w, h = win['size']
        subprocess.run(['grim', '-g', f'{x},{y} {w}x{h}',
                        str(ROOT / f'validation/fullscreen-{suffix}-{name}.png')], check=True)

    def record(name, data):
        results.append({'check': name, 'result': 'pass', 'state': data})
        report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(name + ': PASS', flush=True)

    def fullscreen(data):
        return data.get('desktop', {}).get('player_fullscreen', False)

    def separate(data):
        dock = data.get('dock', {})
        if not dock.get('open'):
            return False
        a, b = dock['video'], dock['panel']
        return (a['x'] + a['width'] <= b['x'] + 2 or a['y'] + a['height'] <= b['y'] + 2)

    with tempfile.TemporaryDirectory(prefix='wiliwili-fullscreen-') as temporary, \
            open(ROOT / f'validation/fullscreen-{suffix}-client.log', 'w') as log:
        env = os.environ.copy()
        env.pop('WILI_AI_TOKEN', None)
        env['XDG_CONFIG_HOME'] = temporary
        env['WILI_AI_DIAGNOSTICS'] = str(state_path)
        config = Path(temporary) / 'wiliwili/wiliwili_config.json'
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({'setting': {'app_ui_scale': '1080p', 'search_tv_mode': False}}))
        args = ['--bv', 'BV1heam6TExz'] if real else ['--ai-test-media', str(ROOT / 'validation/dock-clip.mp4')]
        process = subprocess.Popen([str(binary), *args], cwd=app, env=env, stdout=log, stderr=log)
        selector = f'pid:{process.pid}'
        try:
            wait(lambda d: d.get('ready') and not d.get('stopped') and d.get('position', 0) > 1, 50 if real else 15)
            dispatch(f'hl.dsp.window.move({{window="{selector}",workspace={workspace}}})')
            dispatch(f'hl.dsp.focus({{workspace={workspace}}})')
            dispatch(f'hl.dsp.window.float({{window="{selector}",action="enable"}})')
            dispatch(f'hl.dsp.window.resize({{window="{selector}",x=1259,y=677,relative=false}})')
            dispatch(f'hl.dsp.window.center({{window="{selector}"}})')
            dispatch(f'hl.dsp.focus({{window="{selector}"}})')
            wait(lambda d: d.get('desktop', {}).get('window_width') == 1259)
            key('space'); wait(lambda d: d.get('paused'))
            base_count = state()['desktop']['activity_count']
            screenshot('quarter-player')

            key('v')
            data = wait(lambda d: fullscreen(d) and d['desktop']['window_width'] >= 1900)
            assert window()['fullscreen'] != 0
            assert data['desktop']['activity_count'] == base_count + 1
            record('v enters player and monitor fullscreen with one key', data)
            key('v')
            data = wait(lambda d: not fullscreen(d) and d['desktop']['window_width'] == 1259)
            assert data['desktop']['activity_count'] == base_count
            record('Second v restores the original window and activity', data)
            if real:
                key('F11')
                data = wait(lambda d: d['desktop']['window_width'] >= 1900)
                assert not fullscreen(data)
                record('Window fullscreen preserves the normal player layout', data)
                screenshot('full-detail')
                key('F11'); wait(lambda d: d['desktop']['window_width'] == 1259)

            key('F8')
            data = wait(lambda d: separate(d) and d.get('desktop', {}).get('body_font_pixels', 0) > 0)
            small_font = data['desktop']['body_font_pixels']
            record('Quarter-screen reader has nonoverlapping video and text', data)
            screenshot('quarter-reader')
            key('v')
            record('v works while focus is in the reading column', wait(fullscreen))
            key('F8')
            data = wait(lambda d: fullscreen(d) and separate(d) and d.get('desktop', {}).get('body_font_pixels', 0) > small_font)
            large_font = data['desktop']['body_font_pixels']
            assert large_font <= small_font * 1.35
            assert abs(large_font - round(large_font)) < .001
            assert data['desktop']['window_scale'] <= 1.001
            record('Fullscreen uses modestly larger pixel-aligned text and native layout scale', data)
            screenshot('full-reader')

            key('F7'); wait(lambda d: d.get('desktop', {}).get('editing'))
            key('v')
            data = wait(lambda d: d.get('desktop', {}).get('input_text') == 'v')
            assert data['desktop']['activity_count'] == base_count + 2
            record('v types normally inside the question editor', data)
            screenshot('full-input')
            key('Escape'); wait(lambda d: not d.get('desktop', {}).get('editing'))
            key('v')
            data = wait(lambda d: not fullscreen(d) and separate(d) and d['desktop']['window_width'] == 1259)
            assert data['desktop']['activity_count'] == base_count
            assert data['paused']
            record('v exits fullscreen with the dock open and preserves pause state', data)

            key('F8'); wait(lambda d: not d.get('dock', {}).get('open'))
            for _ in range(3):
                key('v'); wait(fullscreen)
                key('v'); wait(lambda d: not fullscreen(d) and d['desktop']['activity_count'] == base_count)
            record('Repeated v toggles never stack fullscreen activities', state())
            key('period'); wait(lambda d: abs(d.get('speed', 0) - 1.25) < .01)
            key('equal'); wait(lambda d: abs(d.get('speed', 0) - 1) < .01)
            key('5')
            wait(lambda d: abs(d['position'] - d['video']['duration'] / 2) < 2)
            key('0'); wait(lambda d: d.get('position', 999) < 2)
            key('l'); wait(lambda d: 4 <= d.get('position', 0) <= 6)
            key('h'); wait(lambda d: d.get('position', 999) < 2)
            record('Numeric seek, h/l and speed bindings coexist with v', state())
            key('F8'); wait(separate)
            dispatch(f'hl.dsp.window.resize({{window="{selector}",x=900,y=850,relative=false}})')
            data = wait(lambda d: separate(d) and d['dock']['layout'] == 'bottom')
            assert data['dock']['video']['height'] * data['desktop']['window_scale'] >= 280
            record('Narrow window keeps the companion below video', data)
            screenshot('narrow-reader')
            if real:
                key('F8'); wait(lambda d: not d.get('dock', {}).get('open'))
                key('Escape'); wait(lambda d: d['desktop']['activity_count'] == 1)
                key('F11'); wait(lambda d: d['desktop']['window_width'] >= 1900)
                time.sleep(2)
                screenshot('full-home')
                key('slash'); wait(lambda d: d['desktop']['editing'])
                for char in 'video':
                    key(char)
                record('Fullscreen home search keeps letter shortcuts isolated',
                       wait(lambda d: d['desktop'].get('input_text') == 'video'))
                screenshot('full-search')
        except Exception as error:
            results.append({'check': 'interrupted', 'result': 'fail', 'error': str(error), 'state': state()})
            report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
            if process.poll() is None:
                screenshot('failure')
            raise
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(5)
            dispatch(f'hl.dsp.focus({{workspace={previous}}})')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--installed', action='store_true')
    parser.add_argument('--real', action='store_true')
    args = parser.parse_args()
    run(args.installed, args.real)
