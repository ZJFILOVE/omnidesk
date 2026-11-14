#!/usr/bin/env python3
"""Measure isolated native 4K playback without changing the user's preferences.

The GUI requires a desktop session. Private credentials, IPC, diagnostics and raw
client logs live in a temporary 0700 directory and are removed on exit. Only
allowlisted playback metrics are written to the requested report.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parent
MEDIA_FIELDS = (
    'width', 'height', 'duration', 'fps', 'codec', 'audio_codec', 'source',
    'selected_quality', 'video_bitrate', 'hwdec', 'hwdec_current',
    'decoder_dropped_frames', 'dropped_frames', 'avsync', 'mistimed_frames',
)


def private_write(path, text):
    with open(path, 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        stream.write(text)


def read_state(path):
    try:
        result = json.loads(path.read_text())
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def process_metrics(pid):
    # /proc/PID/stat's command name can contain spaces and parentheses.
    values = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    ticks = int(values[11]) + int(values[12])
    return ticks / os.sysconf('SC_CLK_TCK'), int(values[21]) * os.sysconf('SC_PAGE_SIZE')


def ipc_command(path, *command):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(str(path))
        connection.sendall((json.dumps({'command': command, 'request_id': 1}) + '\n').encode())
        with connection.makefile('rb') as response:
            while True:
                line = response.readline()
                if not line:
                    raise RuntimeError('Native player IPC closed before replying')
                result = json.loads(line)
                if result.get('request_id') == 1:
                    if result.get('error') != 'success':
                        raise RuntimeError('Native player IPC rejected benchmark command')
                    return result.get('data')


def sanitized_state(state):
    media = state.get('media', {})
    desktop = state.get('desktop', {})
    return {
        'position': state.get('position', 0), 'speed': state.get('speed', 0),
        'paused': state.get('paused', True), 'stopped': state.get('stopped', True),
        'media': {field: media[field] for field in MEDIA_FIELDS if field in media},
        'window': {field: desktop[field] for field in ('window_width', 'window_height', 'player_fullscreen')
                   if field in desktop},
    }


def playback_health(ipc):
    # Probe from the external benchmark, never synchronously in the render loop.
    health = {}
    for name in ('paused-for-cache', 'demuxer-cache-duration', 'avsync',
                 'estimated-display-fps', 'frame-drop-count',
                 'decoder-frame-drop-count', 'vo-delayed-frame-count'):
        try:
            value = ipc_command(ipc, 'get_property', name)
            if isinstance(value, (bool, int, float)):
                health[name] = value
        except RuntimeError:
            pass  # Properties may be unavailable for a particular output driver.
    return health


def run(args):
    app = (args.app_path or (Path.home() / '.local/opt/wiliwili-ai' if args.installed else ROOT / 'build')).resolve()
    binary = app / 'bin/wiliwili' if (app / 'bin/wiliwili').exists() else app / 'wiliwili'
    if not binary.is_file():
        raise RuntimeError('Native playback binary is missing')
    if not args.media and not args.session.is_file():
        raise RuntimeError('Approved App session is missing')
    result = {
        'status': 'incomplete', 'installed': args.installed, 'bvid': None if args.media else args.bv,
        'local_media': bool(args.media), 'fps_limit': args.fps_limit,
        'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'requested_codec': args.codec, 'requested_speed': args.speed,
        'requested_hwdec': args.hwdec, 'hwdec_method': args.hwdec_method,
        'fullscreen': args.fullscreen, 'danmaku': False, 'ai_companion': False,
        'cpu_percent_definition': '100% equals one fully occupied CPU core',
        'logical_cpus': os.cpu_count(), 'samples': [],
    }
    process = None
    state = {}
    previous_handlers = {}

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, interrupted)
        with tempfile.TemporaryDirectory(prefix='wiliwili-performance-') as temporary:
            private = Path(temporary)
            private.chmod(0o700)
            config_dir = private / 'config/wiliwili'
            config_dir.mkdir(parents=True, mode=0o700)
            session = config_dir / 'android-session.json'
            if not args.media:
                shutil.copyfile(args.session, session)
                session.chmod(0o600)
            original_config = Path.home() / '.config/wiliwili/wiliwili_config.json'
            original = read_state(original_config)
            config = {
                'cookie': {} if args.media else original.get('cookie', {}),
                'setting': {
                    'app_ui_scale': '1080p', 'search_tv_mode': False,
                    'fullscreen': args.fullscreen, 'player_auto_fullscreen': args.fullscreen,
                    'video_quality': 120, 'video_codec': args.codec,
                    'video_unlock_quality': True, 'history_report': False,
                    'player_hwdec': args.hwdec == 'on',
                    'player_low_quality': True, 'danmaku': False,
                },
            }
            if args.hwdec_method:
                config['setting']['player_hwdec_custom'] = args.hwdec_method
            if args.fps_limit is not None:
                config['setting']['limited_fps'] = args.fps_limit
            private_write(config_dir / 'wiliwili_config.json', json.dumps(config))
            ipc = private / 'mpv.sock'
            private_write(config_dir / 'mpv.conf', f'input-ipc-server={ipc}\nmute=yes\n')
            state_path = private / 'state.json'
            log_path = private / 'client.log'
            env = os.environ.copy()
            env.pop('WILI_AI_TOKEN', None)
            env.pop('WILI_AI_PORT', None)
            env['XDG_CONFIG_HOME'] = str(private / 'config')
            env['WILI_ANDROID_SESSION'] = str(session)
            env['WILI_AI_DIAGNOSTICS'] = str(state_path)
            if env.get('HYPRLAND_INSTANCE_SIGNATURE') and env.get('WAYLAND_DISPLAY') and env.get('DISPLAY'):
                env.setdefault('SDL_VIDEODRIVER', 'x11')
            launched = time.monotonic()
            with open(log_path, 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as log:
                source = ['--ai-test-media', str(args.media.resolve())] if args.media else ['--bv', args.bv]
                process = subprocess.Popen(['stdbuf', '-oL', '-eL', str(binary), *source],
                                           cwd=app, env=env, stdout=log, stderr=log)

                def wait(predicate, timeout):
                    nonlocal state
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        state = read_state(state_path) or state
                        if predicate(state):
                            return state
                        if process.poll() is not None:
                            raise RuntimeError('Native benchmark player exited unexpectedly')
                        time.sleep(0.1)
                    raise RuntimeError('Native benchmark playback timed out')

                try:
                    def decoded(data):
                        media = data.get('media', {})
                        return (not data.get('stopped', True) and not data.get('paused', True)
                                and data.get('position', 0) > 2
                                and max(media.get('width', 0), media.get('height', 0)) >= 3840
                                and min(media.get('width', 0), media.get('height', 0)) >= 1600)

                    wait(decoded, args.timeout)
                    result['startup_to_4k_seconds'] = round(time.monotonic() - launched, 3)
                    ipc_command(ipc, 'set_property', 'speed', args.speed)
                    wait(lambda data: abs(data.get('speed', 0) - args.speed) < 0.01, 10)
                    position = state.get('position', 0)
                    wait(lambda data: data.get('position', 0) > position + args.speed * 2, 15)
                    initial = sanitized_state(state)
                    result['initial_health'] = playback_health(ipc)
                    initial_cpu, _ = process_metrics(process.pid)
                    started = previous = time.monotonic()
                    previous_cpu = initial_cpu
                    result['initial'] = initial
                    while time.monotonic() - started < args.seconds:
                        time.sleep(max(0, min(1, args.seconds - (time.monotonic() - started))))
                        if process.poll() is not None:
                            raise RuntimeError('Native benchmark player exited during measurement')
                        now = time.monotonic()
                        cpu, memory = process_metrics(process.pid)
                        state = read_state(state_path) or state
                        result['samples'].append({
                            'elapsed_seconds': round(now - started, 3),
                            'cpu_percent': round((cpu - previous_cpu) / (now - previous) * 100, 2),
                            'rss_mib': round(memory / 1048576, 2),
                            **sanitized_state(state),
                            'health': playback_health(ipc),
                        })
                        previous, previous_cpu = now, cpu
                    final = sanitized_state(state)
                    elapsed = time.monotonic() - started
                    result['final'] = final
                    result['final_health'] = playback_health(ipc)
                    result['buffering_samples'] = sum(s['health'].get('paused-for-cache', False) for s in result['samples'])
                    result['measured_seconds'] = round(elapsed, 3)
                    result['cpu_percent_mean'] = round((cpu - initial_cpu) / elapsed * 100, 2)
                    result['rss_mib_peak'] = max(sample['rss_mib'] for sample in result['samples'])
                    result['playback_progress_seconds'] = round(final['position'] - initial['position'], 3)
                    result['playback_progress_ratio'] = round(result['playback_progress_seconds'] / (elapsed * args.speed), 3)
                    for field in ('dropped_frames', 'decoder_dropped_frames', 'mistimed_frames'):
                        before, after = initial['media'].get(field), final['media'].get(field)
                        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
                            result[field + '_delta'] = max(0, after - before)
                    hardware = re.findall(r'\bHW:\s*([A-Za-z0-9_-]+)', log_path.read_text(errors='replace'))
                    result['observed_hwdec'] = final['media'].get('hwdec_current') or final['media'].get('hwdec') or (hardware[-1] if hardware else 'unknown')
                    result['status'] = 'measured'
                finally:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
    except (OSError, RuntimeError, KeyboardInterrupt) as error:
        # Exception strings from OS/network/native tools can include private paths.
        result['status'] = 'failed'
        result['error'] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        result['final'] = sanitized_state(state)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in result.items() if key != 'samples'}, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'measured' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    app = parser.add_mutually_exclusive_group()
    app.add_argument('--installed', action='store_true')
    app.add_argument('--app-path', type=Path)
    parser.add_argument('--codec', type=int, choices=(7, 12, 13), default=7)
    parser.add_argument('--seconds', type=float, default=15)
    parser.add_argument('--speed', type=float, default=1)
    parser.add_argument('--hwdec', choices=('on', 'off'), default='on')
    parser.add_argument('--hwdec-method', choices=('auto-safe', 'auto', 'auto-copy', 'nvdec', 'nvdec-copy', 'vaapi', 'vaapi-copy'))
    parser.add_argument('--fullscreen', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--fps-limit', type=int, choices=(0, 30, 60, 90, 120), help='Omit to use the binary default')
    parser.add_argument('--bv', default='BV1heam6TExz')
    parser.add_argument('--media', type=Path, help='Optional local 4K media for repeatable decoding measurements')
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--session', type=Path, default=Path.home() / '.config/wiliwili/android-session.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.seconds < 1 or args.timeout < 1 or not 0.25 <= args.speed <= 4:
        parser.error('seconds/timeout must be positive and speed must be between 0.25 and 4')
    if not re.fullmatch(r'BV[A-Za-z0-9]{10}', args.bv):
        parser.error('bv must be a Bilibili BV identifier')
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
