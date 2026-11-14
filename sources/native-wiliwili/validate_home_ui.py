#!/usr/bin/env python3
"""Verify responsive native covers and home shortcuts in a separate test window."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent


def run(installed=False, shortcuts_only=False):
    state_path = ROOT / 'validation/home-native-state.json'
    report_path = ROOT / ('validation/home-shortcuts-results.json' if shortcuts_only else 'validation/home-ui-results.json')
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

    def wait(predicate, timeout=15):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            data = state()
            if predicate(data):
                return data
            time.sleep(.1)
        raise AssertionError('Timed out: ' + json.dumps(state(), ensure_ascii=False))

    def dispatch(expression):
        output = subprocess.check_output(['hyprctl', 'dispatch', expression], text=True)
        assert output.strip() == 'ok', output

    def window():
        return next(c for c in json.loads(subprocess.check_output(['hyprctl', 'clients', '-j']))
                    if c['pid'] == process.pid)

    def key(name, mods=''):
        held = [{'CTRL': 'Control_L', 'SHIFT': 'Shift_L'}[m] for m in mods.split()]
        commands = []
        for k, action, metadata in ([(m, 'down', '') for m in held]
                                    + [(name, 'down', mods), (name, 'up', mods)]
                                    + [(m, 'up', '') for m in reversed(held)]):
            commands.append('dispatch hl.dsp.send_key_state({window=' + json.dumps(selector)
                            + ',mods=' + json.dumps(metadata) + ',key=' + json.dumps(k)
                            + ',state=' + json.dumps(action) + '})')
        output = subprocess.check_output(['hyprctl', '--batch', ';'.join(commands)], text=True)
        assert all(line.strip() == 'ok' for line in output.splitlines() if line.strip()), output
        time.sleep(.3)

    def refresh_check(name):
        before = state()['desktop'].get('home_refresh_count', 0)
        selected = state()['desktop']['home_tab_index']
        key('r')
        data = wait(lambda d: d.get('desktop', {}).get('home_refresh_count') == before + 1)
        assert data['desktop']['home_tab_index'] == selected
        assert data['desktop']['activity_count'] == 1
        record(name, data)

    def resize(width, height):
        dispatch(f'hl.dsp.window.resize({{window="{selector}",x={width},y={height},relative=false}})')
        dispatch(f'hl.dsp.window.center({{window="{selector}"}})')
        wait(lambda d: d.get('desktop', {}).get('window_width') == width)

    def screenshot(name):
        time.sleep(.4)
        win = window()
        x, y = win['at']; w, h = win['size']
        subprocess.run(['grim', '-g', f'{x},{y} {w}x{h}',
                        str(ROOT / f'validation/home-{name}.png')], check=True)

    def record(name, data):
        results.append({'check': name, 'result': 'pass', 'desktop': data.get('desktop', {})})
        report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(name + ': PASS', flush=True)

    def grid(data, name='home/recommends/recyclingGrid'):
        return next((g for g in data.get('desktop', {}).get('grids', []) if name in g['view']), {})

    def cover(data, name='home/recommends/recyclingGrid', body=104):
        g = grid(data, name)
        if g.get('image_width', 0) <= 0 or g.get('cover_height', 0) <= 0:
            return False
        assert abs(g['cover_width'] / g['cover_height'] - 16 / 9) < .015, g
        assert g['card_height'] - g['cover_height'] >= body - 2, g
        assert abs(g['card_height'] - g['row_height']) < 2, g
        return True

    def tab(data, index):
        desktop = data.get('desktop', {})
        return (desktop.get('home_tab_index') == index
                and desktop.get('focused_tab_label', '').startswith(str(index + 1) + ' '))

    with tempfile.TemporaryDirectory(prefix='wiliwili-home-') as temporary, \
            open(ROOT / 'validation/home-client.log', 'w') as log:
        env = os.environ.copy()
        env.pop('WILI_AI_TOKEN', None)
        env['XDG_CONFIG_HOME'] = temporary
        env['WILI_AI_DIAGNOSTICS'] = str(state_path)
        config = Path(temporary) / 'wiliwili/wiliwili_config.json'
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({'setting': {'app_ui_scale': '1080p', 'search_tv_mode': False}}))
        process = subprocess.Popen([str(binary)], cwd=app, env=env, stdout=log, stderr=log)
        selector = f'pid:{process.pid}'
        try:
            wait(lambda d: 'desktop' in d)
            dispatch(f'hl.dsp.window.move({{window="{selector}",workspace={workspace}}})')
            dispatch(f'hl.dsp.focus({{workspace={workspace}}})')
            dispatch(f'hl.dsp.window.float({{window="{selector}",action="enable"}})')
            resize(1259, 677)
            dispatch(f'hl.dsp.focus({{window="{selector}"}})')
            if shortcuts_only:
                for index in [1, 0, 2, 3, 4]:
                    key(str(index + 1))
                    wait(lambda d: tab(d, index))
                    refresh_check(f'r refreshes home tab {index + 1}')
                before = state()['desktop']['home_refresh_count']
                key('r', 'SHIFT')
                assert state()['desktop']['home_refresh_count'] == before
                record('Shift+r does not trigger the plain r shortcut', state())
                key('slash'); wait(lambda d: d.get('desktop', {}).get('editing'))
                key('r')
                data = wait(lambda d: d.get('desktop', {}).get('input_text') == 'r')
                assert data['desktop']['home_refresh_count'] == before
                record('r types normally in search without refreshing the home page', data)
                key('Escape'); wait(lambda d: not d.get('desktop', {}).get('editing'))
                return
            key('2')
            data = wait(lambda d: tab(d, 1) and cover(d), 30)
            assert data['desktop']['home_tab_labels'] == ['1 直播', '2 推荐', '3 热门', '4 追番', '5 影视']
            record('Quarter-screen covers keep 16:9 with room for two title lines', data)
            screenshot('quarter-covers')

            key('Return')
            for _ in range(14):
                key('j')
            data = wait(lambda d: grid(d).get('offset', 0) > 350
                        and d.get('desktop', {}).get('focused_item_index', 0) >= 18)
            offset = grid(data)['offset']
            record('Keyboard focus reaches cards far below the home header', data)
            screenshot('scrolled')
            # Let the animated scroll settle before comparing the preserved offset.
            time.sleep(.6)
            offset = grid(state())['offset']
            key('2')
            data = wait(lambda d: tab(d, 1))
            assert abs(grid(data)['offset'] - offset) < 2, grid(data)
            record('Current number returns focus to the top tab and preserves scrolling', data)
            key('Return')
            refresh_check('r refreshes recommendations from a scrolled list without opening settings')
            wait(lambda d: cover(d) and grid(d).get('offset', -1) == 0, 30)
            refresh_check('r refreshes recommendations with focus on the top tab')
            wait(cover, 30)
            key('Return'); key('1')
            record('A number switches directly from a scrolled list to live', wait(lambda d: tab(d, 0)))
            for index in [1, 2, 3, 4]:
                key(str(index + 1))
                record(f'Home key {index + 1} selects its matching top tab', wait(lambda d: tab(d, index)))
            key('2'); wait(lambda d: tab(d, 1))
            key('1', 'CTRL')
            assert state()['desktop']['home_tab_index'] == 1
            record('Modified digits leave the selected home tab unchanged', state())

            key('slash'); wait(lambda d: d.get('desktop', {}).get('editing'))
            before_refresh = state()['desktop'].get('home_refresh_count', 0)
            key('r')
            assert state()['desktop']['editing']
            wait(lambda d: d.get('desktop', {}).get('input_text') == 'r')
            assert state()['desktop'].get('home_refresh_count', 0) == before_refresh
            record('r does not refresh behind the search editor', state())
            for digit in '12345':
                key(digit)
            data = wait(lambda d: d.get('desktop', {}).get('input_text') == 'r12345')
            record('Home digits type normally in the search editor', data)
            key('Escape'); wait(lambda d: not d.get('desktop', {}).get('editing'))
            assert state()['desktop']['home_tab_index'] == 1

            key('g'); key('g')
            key('2')
            key('F11')
            data = wait(lambda d: d.get('desktop', {}).get('window_width', 0) >= 1900 and cover(d))
            record('Fullscreen cover proportions and card height follow the wider columns', data)
            screenshot('full-covers')
            key('F11'); wait(lambda d: d['desktop']['window_width'] == 1259 and cover(d))
            resize(900, 850)
            data = wait(cover)
            assert grid(data)['columns'] == 3 and grid(data)['cover_width'] >= 250, grid(data)
            record('Narrow window reduces to three readable columns with full covers', data)
            screenshot('narrow-covers')

            key('3'); wait(lambda d: tab(d, 2))
            record('Popular video cards share the corrected cover layout',
                   wait(lambda d: cover(d, 'home/hots/all/recyclingGrid'), 30))
            screenshot('popular-covers')
            refresh_check('r refreshes the active popular subtab')
            key('1'); wait(lambda d: tab(d, 0))
            record('Live cards keep 16:9 and a separate author line',
                   wait(lambda d: cover(d, 'home/live/recyclingGrid', 70), 30))
            screenshot('live-covers')
            refresh_check('r refreshes live listings instead of opening the category picker')
            key('2'); wait(lambda d: tab(d, 1))
            resize(1259, 677)
            data = wait(lambda d: cover(d) and grid(d).get('columns') == 4)
            record('Half-screen width keeps a readable four-column grid', data)
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
    parser.add_argument('--shortcuts-only', action='store_true')
    args = parser.parse_args()
    run(args.installed, args.shortcuts_only)
