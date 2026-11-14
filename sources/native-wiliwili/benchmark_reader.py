#!/usr/bin/env python3
"""Measure the real reader with deterministic synthetic data and a local video.

No model, account, launcher, or user configuration is used. The synthetic HTTP
companion, diagnostic snapshots and client log live in a private temporary
directory. Only numeric metrics and regression results enter the report.
"""
import argparse
from contextlib import ExitStack
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parent


def read_state(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def process_metrics(pid):
    values = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return ((int(values[11]) + int(values[12])) / os.sysconf('SC_CLK_TCK'),
            int(values[21]) * os.sysconf('SC_PAGE_SIZE'))


def private_write(path, text):
    with open(path, 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        stream.write(text)


def stop_process(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try: process.wait(8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(5)


class Fixture:
    def __init__(self):
        self.lock = threading.Lock()
        self.version = 1
        self.video = {}
        self.polls = 0
        self.commands = []
        paragraph = ('这是明确标记的本地性能测试资料，用于检查长文阅读、章节跳转和异步更新。'
                     '内容完全由测试脚本生成，不是视频总结，也不调用任何模型。')
        self.summary = '\n\n'.join(f'## 合成段落 {i + 1}\n\n' + paragraph * 5 for i in range(48))
        self.messages = [{'role': 'user' if i % 2 == 0 else 'assistant',
                          'content': f'合成对话 {i + 1}\n\n' + paragraph * 10} for i in range(40)]
        self.segments = [{'start': i * 8, 'end': min(200, (i + 1) * 8),
                          'label': f'合成章节 {i + 1}', 'digest': paragraph * 2,
                          'type': 'content'} for i in range(24)]

    def response(self):
        # The caller holds lock. Repeated polling returns identical content.
        version = self.version
        identity = f"{self.video.get('bvid', '')}:{self.video.get('cid', 0)}:{self.video.get('p', 1)}"
        segments = [dict(chapter) for chapter in self.segments]
        segments[0]['label'] = f'READER-REV-{version}'
        return {'id': 'synthetic-reader', 'identity': identity, 'video': self.video,
                'status': f'本地合成测试资料 · 版本 {version}', 'busy': False,
                'summary': f'READER-REV-{version}\n\n' + self.summary,
                'segments': segments, 'messages': self.messages,
                'ads': [], 'sources': {'subtitles': 400, 'danmaku': 0, 'comments': 0},
                'warnings': ['本地合成性能测试，不是真实视频资料。']}


def companion(fixture, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def handle_request(self):
            if self.headers.get('X-Wili-Auth') != token:
                self.send_error(403)
                return
            length = int(self.headers.get('Content-Length', '0'))
            body = json.loads(self.rfile.read(length)) if length else {}
            with fixture.lock:
                if self.command == 'POST' and self.path == '/session':
                    fixture.video = body['video']
                elif self.command == 'POST' and self.path == '/session/synthetic-reader/command':
                    action = body.get('action')
                    fixture.commands.append(action)
                    if action == 'summary':
                        fixture.version += 1
                    elif action == 'ask':
                        fixture.messages.extend([
                            {'role': 'user', 'content': body.get('question', '')},
                            {'role': 'assistant', 'content': 'READER-ANSWER-UPDATED'}])
                elif self.command == 'GET' and self.path == '/session/synthetic-reader':
                    fixture.polls += 1
                payload = json.dumps(fixture.response(), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = do_POST = do_DELETE = handle_request

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def run(args):
    app = (args.app_path or (Path.home() / '.local/opt/wiliwili-ai' if args.installed else ROOT / 'build')).resolve()
    binary = app / 'bin/wiliwili' if (app / 'bin/wiliwili').exists() else app / 'wiliwili'
    if not binary.is_file() or not args.media.is_file():
        raise RuntimeError('Native binary or local test media is missing')
    result = {'status': 'incomplete', 'installed': args.installed,
              'fixture': 'generated_synthetic_reader_data_and_real_local_video',
              'real_model': False, 'fps_limit': args.fps_limit,
              'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'media_sha256': hashlib.sha256(args.media.read_bytes()).hexdigest(),
              'cpu_percent_definition': '100% equals one fully occupied CPU core',
              'samples': [], 'checks': []}
    previous_workspace = None
    process = None
    server = None
    state = {}
    handlers = {}

    def dispatch(expression):
        reply = subprocess.check_output(['hyprctl', 'dispatch', expression], text=True)
        if reply.strip() != 'ok':
            raise RuntimeError('Compositor rejected a targeted benchmark operation')

    def interrupted(*_):
        raise KeyboardInterrupt

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, interrupted)
        previous_workspace = json.loads(subprocess.check_output(['hyprctl', 'activeworkspace', '-j']))['id']
        used = {w['id'] for w in json.loads(subprocess.check_output(['hyprctl', 'workspaces', '-j']))}
        workspace = next(i for i in range(9900, 10000) if i not in used)
        with tempfile.TemporaryDirectory(prefix='wiliwili-reader-performance-') as temporary, ExitStack() as cleanup:
            private = Path(temporary)
            private.chmod(0o700)
            config = private / 'config/wiliwili'
            config.mkdir(parents=True, mode=0o700)
            private_write(config / 'wiliwili_config.json', json.dumps({'cookie': {}, 'setting': {
                'app_ui_scale': '1080p', 'fullscreen': False, 'search_tv_mode': False,
                'history_report': False, 'danmaku': False, 'player_hwdec': True,
                'player_low_quality': True, 'limited_fps': args.fps_limit}}))
            private_write(config / 'mpv.conf', 'mute=yes\n')
            fixture = Fixture()
            token = secrets.token_hex(24)
            server, server_thread = companion(fixture, token)
            state_path = private / 'state.json'
            env = os.environ.copy()
            env.update({'XDG_CONFIG_HOME': str(private / 'config'), 'WILI_AI_TOKEN': token,
                        'WILI_AI_PORT': str(server.server_port), 'WILI_AI_DIAGNOSTICS': str(state_path),
                        'WILI_ANDROID_SESSION': str(config / 'absent-android-session.json')})
            if env.get('HYPRLAND_INSTANCE_SIGNATURE') and env.get('WAYLAND_DISPLAY') and env.get('DISPLAY'):
                env.setdefault('SDL_VIDEODRIVER', 'x11')
            with (private / 'client.log').open('w') as log:
                process = subprocess.Popen([str(binary), '--ai-test-media', str(args.media.resolve())],
                                           cwd=app, env=env, stdout=log, stderr=log)
                cleanup.callback(stop_process, process)
                selector = f'pid:{process.pid}'

                def wait(predicate, description, timeout=15):
                    nonlocal state
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        state = read_state(state_path) or state
                        if predicate(state):
                            return state
                        if process.poll() is not None:
                            raise RuntimeError('Owned benchmark app exited: ' + description)
                        time.sleep(.1)
                    raise RuntimeError('Timed out: ' + description)

                def tap(key, mods=''):
                    commands = []
                    held = {'CTRL': 'Control_L', 'SHIFT': 'Shift_L'}
                    def event(name, pressed, metadata=''):
                        commands.append('dispatch hl.dsp.send_key_state({window=' + json.dumps(selector) +
                                        ',mods=' + json.dumps(metadata) + ',key=' + json.dumps(name) +
                                        ',state=' + json.dumps(pressed) + '})')
                    for modifier in mods.split(): event(held[modifier], 'down')
                    event(key, 'down', mods)
                    event(key, 'up', mods)
                    for modifier in reversed(mods.split()): event(held[modifier], 'up')
                    reply = subprocess.check_output(['hyprctl', '--batch', ';'.join(commands)], text=True)
                    if any(line.strip() != 'ok' for line in reply.splitlines() if line.strip()):
                        raise RuntimeError('Compositor rejected an owned-window key event')
                    time.sleep(.25)

                def check(name):
                    result['checks'].append({'check': name, 'result': 'pass'})

                def select_tab(key, label):
                    # Rebuilding a long column can briefly block input. Tab
                    # selection is idempotent, so retry after a settled frame.
                    for attempt in range(4):
                        if read_state(state_path).get('panel_tab') == label:
                            return
                        time.sleep(.6)
                        tap(key)
                        try:
                            wait(lambda d: d.get('panel_tab') == label, label + ' tab', 2)
                            return
                        except RuntimeError as error:
                            if not str(error).startswith('Timed out:') or attempt == 3:
                                raise

                def layout_valid(data):
                    dock = data.get('dock', {})
                    if not dock.get('open') or not dock.get('video') or not dock.get('panel'):
                        return False
                    v, p = dock['video'], dock['panel']
                    return (min(v['width'], v['height']) > 100 and
                            (v['x'] + v['width'] <= p['x'] + 2 or v['y'] + v['height'] <= p['y'] + 2))

                deadline = time.monotonic() + 20
                while not any(c['pid'] == process.pid for c in json.loads(subprocess.check_output(['hyprctl', 'clients', '-j']))):
                    if time.monotonic() > deadline: raise RuntimeError('Owned benchmark window did not appear')
                    time.sleep(.1)
                dispatch(f'hl.dsp.window.move({{window="{selector}",workspace={workspace}}})')
                dispatch(f'hl.dsp.focus({{workspace={workspace}}})')
                dispatch(f'hl.dsp.window.float({{window="{selector}",action="enable"}})')
                dispatch(f'hl.dsp.window.resize({{window="{selector}",x=1400,y=900,relative=false}})')
                dispatch(f'hl.dsp.focus({{window="{selector}"}})')
                wait(lambda d: d.get('ready') and not d.get('stopped') and not d.get('paused')
                     and d.get('position', 0) > 1 and d.get('summary', '').startswith('READER-REV-1'), 'local media and synthetic fixture')
                tap('F8')
                wait(lambda d: layout_valid(d) and d.get('panel_tab') == '总结', 'summary panel')
                check('Synthetic summary opens beside the playing local video')
                time.sleep(1.5)
                initial = read_state(state_path)
                initial_cpu, _ = process_metrics(process.pid)
                started = previous = time.monotonic()
                previous_cpu = initial_cpu
                initial_rebuilds = initial.get('dock', {}).get('rebuild_count')
                with fixture.lock:
                    result['fixture_bytes'] = len(json.dumps(fixture.response(), ensure_ascii=False).encode())
                    initial_polls = fixture.polls
                while time.monotonic() - started < args.seconds:
                    time.sleep(min(1, max(.01, args.seconds - (time.monotonic() - started))))
                    cpu, memory = process_metrics(process.pid)
                    now = time.monotonic()
                    state = read_state(state_path) or state
                    if not layout_valid(state) or state.get('paused') or state.get('stopped'):
                        raise RuntimeError('Reader measurement lost its layout or playback')
                    result['samples'].append({'elapsed_seconds': round(now - started, 3),
                        'cpu_percent': round((cpu - previous_cpu) / (now - previous) * 100, 2),
                        'rss_mib': round(memory / 1048576, 2), 'position': state.get('position', 0),
                        'reader_offset': state.get('reader_offset', 0),
                        'rebuild_count': state.get('dock', {}).get('rebuild_count')})
                    previous, previous_cpu = now, cpu
                elapsed = time.monotonic() - started
                result['measured_seconds'] = round(elapsed, 3)
                result['cpu_percent_mean'] = round((cpu - initial_cpu) / elapsed * 100, 2)
                result['rss_mib_peak'] = max(sample['rss_mib'] for sample in result['samples'])
                result['playback_progress_ratio'] = round((state['position'] - initial['position']) / elapsed, 3)
                result['media'] = {key: state.get('media', {}).get(key) for key in ('width', 'height', 'fps', 'codec', 'hwdec_current')}
                if initial_rebuilds is not None:
                    result['stable_content_rebuilds'] = state['dock']['rebuild_count'] - initial_rebuilds
                    if result['stable_content_rebuilds'] != 0:
                        raise RuntimeError('Unchanged reader content caused repeated rebuilding')
                    check('Unchanged companion polls cause zero reader rebuilds')
                with fixture.lock:
                    result['companion_polls_during_measurement'] = fixture.polls - initial_polls
                with fixture.lock:
                    fixture.version = 2
                    fixture.summary += '\n\nREADER-SUMMARY-UPDATED'
                    fixture.segments.append({'start': 192, 'end': 200, 'label': 'READER-NEW-CHAPTER',
                                             'digest': '合成新增章节', 'type': 'content'})
                wait(lambda d: d.get('summary', '').startswith('READER-REV-2')
                     and d['summary'].endswith('READER-SUMMARY-UPDATED')
                     and len(d.get('segments', [])) == 25, 'asynchronous summary and chapter refresh')
                if 'rendered_summary_bytes' in state.get('dock', {}):
                    wait(lambda d: d['dock']['rendered_summary_bytes'] == len(d['summary'].encode())
                         and d['dock']['rendered_segments'] == 25
                         and '版本 2' in d['dock'].get('status', '')
                         and d['dock']['rebuild_count'] > initial_rebuilds,
                         'rendered summary and chapter refresh')
                check('Changed summary and chapters invalidate and refresh the open reader')
                select_tab('2', '章节')
                if 'rendered_segments' in state.get('dock', {}):
                    wait(lambda d: d['dock']['rendered_segments'] == 25, 'new chapter rendered in chapters tab')
                check('Chapter tab renders the updated 25-chapter fixture')
                select_tab('3', '问答')
                with fixture.lock:
                    fixture.messages.extend([
                        {'role': 'user', 'content': '本地合成异步更新测试'},
                        {'role': 'assistant', 'content': 'READER-ANSWER-UPDATED'}])
                wait(lambda d: len(d.get('messages', [])) == 42 and d.get('panel_tab') == '问答'
                     and d['messages'][-1].get('content') == 'READER-ANSWER-UPDATED', 'synthetic answer update')
                if 'rendered_messages' in state.get('dock', {}):
                    wait(lambda d: d.get('dock', {}).get('rendered_messages') == 42, 'rendered new answer')
                check('Asynchronous companion answer refreshes the open answer tab')
                dispatch(f'hl.dsp.window.resize({{window="{selector}",x=900,y=850,relative=false}})')
                wait(lambda d: layout_valid(d) and d['dock']['layout'] == 'bottom', 'narrow reader layout')
                check('Narrow reader remains separate from the video')
                tap('F8')
                wait(lambda d: not d.get('dock', {}).get('open') and not d.get('paused'), 'close reader')
                check('Closing the reader preserves playback')
                result['status'] = 'pass'
    except (OSError, RuntimeError, KeyboardInterrupt) as error:
        result['status'] = 'failed'
        result['error'] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        result['last_ui'] = {'position': state.get('position'), 'panel_tab': state.get('panel_tab'),
                             'reader_offset': state.get('reader_offset'), 'focus': state.get('focus'),
                             'focus_text': state.get('focus_text'), 'dock': state.get('dock'),
                             'editing': state.get('desktop', {}).get('editing'),
                             'input_text': state.get('desktop', {}).get('input_text')}
    finally:
        stop_process(process)
        if server is not None:
            server.shutdown()
            server.server_close()
            server_thread.join(3)
        if previous_workspace is not None:
            dispatch(f'hl.dsp.focus({{workspace={previous_workspace}}})')
        for signum, handler in handlers.items(): signal.signal(signum, handler)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'samples'}, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed', action='store_true')
    parser.add_argument('--app-path', type=Path)
    parser.add_argument('--media', type=Path, default=ROOT / 'validation/clip.mp4')
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--fps-limit', type=int, default=60)
    parser.add_argument('--output', type=Path, default=ROOT / 'validation/performance-reader.json')
    arguments = parser.parse_args()
    if arguments.seconds < 1: parser.error('--seconds must be at least 1')
    raise SystemExit(run(arguments))
