"""Observe/control an existing Apple Music player; never fetch or play audio.

MPRIS time values are microseconds, while this module's public API uses seconds.
Output routing uses PulseAudio's API (also provided by pipewire-pulse), moving
only the identified player streams, never changing the system default device.

Protocol references:
https://specifications.freedesktop.org/mpris/latest/Player_Interface.html
https://github.com/PipeWire/pipewire/blob/master/README.md
"""
from __future__ import annotations

import ast
import json
import math
import os
import re
import shutil
import subprocess
from urllib.parse import urlparse


PLAYER_INTERFACE = 'org.mpris.MediaPlayer2.Player'
PLAYER_PATH = '/org/mpris/MediaPlayer2'
PLAYER_PREFIX = 'org.mpris.MediaPlayer2.'
_DBUS_NAME = re.compile(r'^org\.mpris\.MediaPlayer2\.[A-Za-z0-9_.-]+$')
_BROWSER = re.compile(r'firefox|chromium|chrome|brave|vivaldi|microsoft.?edge', re.I)
_NATIVE = re.compile(r'^(?:adam|cider|cider2|applemusic|apple_music|apple-music)(?:[._-]|$)', re.I)


class PlaybackError(RuntimeError):
    pass


class _VariantReader:
    """Read gdbus' GVariant text without evaluating executable expressions."""

    def __init__(self, text):
        if len(text) > 1024 * 1024:
            raise ValueError('D-Bus response is too large')
        self.text, self.i = text, 0

    def space(self):
        while self.i < len(self.text) and self.text[self.i].isspace():
            self.i += 1

    def value(self, depth=0):
        if depth > 40:
            raise ValueError('D-Bus response is too deeply nested')
        self.space()
        if self.i >= len(self.text):
            raise ValueError('Missing GVariant value')
        c = self.text[self.i]
        if c == '@':
            while self.i < len(self.text) and not self.text[self.i].isspace():
                self.i += 1
            return self.value(depth + 1)
        if c in "'\"":
            start = self.i
            self.i += 1
            while self.i < len(self.text):
                token = self.text[self.i]
                self.i += 1
                if token == '\\':
                    self.i += 1
                elif token == c:
                    return ast.literal_eval(self.text[start:self.i])
            raise ValueError('Unterminated GVariant string')
        if c == '<':
            self.i += 1
            result = self.value(depth + 1)
            self.space()
            if self.text[self.i:self.i + 1] != '>':
                raise ValueError('Missing GVariant variant terminator')
            self.i += 1
            return result
        if c in '([{':
            close = {'(': ')', '[': ']', '{': '}'}[c]
            self.i += 1
            result = {} if c == '{' else []
            while True:
                self.space()
                if self.text[self.i:self.i + 1] == close:
                    self.i += 1
                    return result
                item = self.value(depth + 1)
                if c == '{':
                    self.space()
                    if self.text[self.i:self.i + 1] != ':':
                        raise ValueError('Missing GVariant dictionary separator')
                    self.i += 1
                    result[item] = self.value(depth + 1)
                else:
                    result.append(item)
                self.space()
                separator = self.text[self.i:self.i + 1]
                if separator == ',':
                    self.i += 1
                elif separator != close:
                    raise ValueError('Invalid GVariant container')
        match = re.match(r'[A-Za-z_][A-Za-z0-9_]*|[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?', self.text[self.i:])
        if not match:
            raise ValueError('Invalid GVariant value')
        token = match.group()
        self.i += len(token)
        if token in ('true', 'false'):
            return token == 'true'
        if token in ('byte', 'int16', 'uint16', 'int32', 'uint32', 'int64', 'uint64',
                     'handle', 'double', 'objectpath', 'signature', 'string'):
            return self.value(depth + 1)
        if re.fullmatch(r'[-+]?\d+', token):
            return int(token)
        try:
            result = float(token)
        except ValueError as exc:
            raise ValueError('Unsupported GVariant token') from exc
        if not math.isfinite(result):
            raise ValueError('Nonfinite GVariant number')
        return result


def parse_gvariant(text):
    parser = _VariantReader(text)
    result = parser.value()
    parser.space()
    if parser.i != len(text):
        raise ValueError('Unexpected trailing D-Bus response')
    return result


def _number(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _apple_url(value):
    try:
        host = (urlparse(str(value)).hostname or '').lower()
        return host in ('music.apple.com', 'itunes.apple.com')
    except ValueError:
        return False


def _empty(error=''):
    return {'available': False, 'backend': 'mpris', 'player': '', 'identity': '',
            'track': {}, 'position': 0.0, 'playing': False, 'volume': None,
            'capabilities': {key: False for key in
                             ('play_pause', 'next', 'previous', 'seek', 'volume', 'device')},
            'devices': [], 'error': error}


class MusicPlayback:
    """A bounded, dependency-free MPRIS client with optional local audio routing.

    Adam and Cider are discovered by player name. Generic browser players are considered
    only if their current metadata identifies music.apple.com/itunes.apple.com.
    QWEN_MUSIC_PLAYER can explicitly pin a single player by its MPRIS name.
    """

    def __init__(self, preferred_player=None, *, runner=None, finder=None, timeout=2.0):
        self.preferred_player = preferred_player or os.environ.get('QWEN_MUSIC_PLAYER', '')
        self.runner = runner or subprocess.run
        self.finder = finder or shutil.which
        self.timeout = max(0.2, min(5.0, float(timeout)))
        self._last = _empty()

    def _run(self, program, *args):
        executable = self.finder(program)
        if not executable:
            raise PlaybackError(f'未找到 {program}')
        try:
            result = self.runner([executable, *map(str, args)], capture_output=True,
                                 text=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PlaybackError(f'{program} 无法响应') from exc
        if result.returncode:
            raise PlaybackError((result.stderr or f'{program} 调用失败').strip()[:240])
        if len(result.stdout) > 1024 * 1024:
            raise PlaybackError(f'{program} 返回内容过大')
        return result.stdout.strip()

    def _dbus(self, destination, method, *args, path=PLAYER_PATH):
        raw = self._run('gdbus', 'call', '--session', '--timeout', str(max(1, math.ceil(self.timeout))),
                        '--dest', destination, '--object-path', path, '--method', method, *args)
        try:
            values = parse_gvariant(raw)
            return values[0] if values else None
        except (ValueError, IndexError, TypeError, SyntaxError) as exc:
            raise PlaybackError('无法解析播放器的 D-Bus 响应') from exc

    def _properties(self, player, interface):
        values = self._dbus(player, 'org.freedesktop.DBus.Properties.GetAll', interface)
        return values if isinstance(values, dict) else {}

    def _names(self):
        names = self._dbus('org.freedesktop.DBus', 'org.freedesktop.DBus.ListNames',
                           path='/org/freedesktop/DBus')
        if not isinstance(names, list):
            return []
        names = [name for name in names if isinstance(name, str) and _DBUS_NAME.fullmatch(name)
                 and name != PLAYER_PREFIX + 'playerctld']
        preferred = self.preferred_player
        if preferred and not preferred.startswith(PLAYER_PREFIX):
            preferred = PLAYER_PREFIX + preferred
        # A pin still comes from the live bus list; no arbitrary D-Bus destinations.
        if preferred:
            return [name for name in names if name == preferred]
        return sorted(names, key=lambda name: not bool(_NATIVE.match(name[len(PLAYER_PREFIX):])))[:16]

    def snapshot(self):
        try:
            candidates = []
            for name in self._names():
                try:
                    properties = self._properties(name, PLAYER_INTERFACE)
                    metadata = properties.get('Metadata') or {}
                    if not isinstance(metadata, dict):
                        continue
                    if not (self.preferred_player or _NATIVE.match(name[len(PLAYER_PREFIX):])
                            or _apple_url(metadata.get('xesam:url', ''))):
                        continue
                    candidates.append((properties.get('PlaybackStatus') == 'Playing', name, properties, metadata))
                except PlaybackError:
                    continue
            if not candidates:
                self._last = _empty('等待 Apple Music 开始播放（网页版需启用浏览器接入）')
                return self._last
            _, player, props, metadata = max(candidates, key=lambda item: item[0])
            try:
                identity = str(self._properties(player, 'org.mpris.MediaPlayer2').get('Identity', ''))
            except PlaybackError:
                identity = player[len(PLAYER_PREFIX):]
            try:
                pid = self._dbus('org.freedesktop.DBus', 'org.freedesktop.DBus.GetConnectionUnixProcessID',
                                 player, path='/org/freedesktop/DBus')
            except PlaybackError:
                pid = None
            artist = metadata.get('xesam:artist', [])
            artist = ', '.join(map(str, artist)) if isinstance(artist, list) else str(artist or '')
            duration = max(0.0, _number(metadata.get('mpris:length')) / 1_000_000)
            position = max(0.0, _number(props.get('Position')) / 1_000_000)
            if duration:
                position = min(duration, position)
            controllable = bool(props.get('CanControl', False))
            track_id = str(metadata.get('mpris:trackid', ''))
            valid_id = bool(re.fullmatch(r'/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]*', track_id)
                            and track_id != '/org/mpris/MediaPlayer2/TrackList/NoTrack')
            state = {'available': True, 'backend': 'mpris', 'player': player, 'identity': identity,
                     'process_id': pid,
                     'track': {'id': track_id, 'title': str(metadata.get('xesam:title', '')),
                               'artist': artist, 'album': str(metadata.get('xesam:album', '')),
                               'duration': duration, 'url': str(metadata.get('xesam:url', '')),
                               'art_url': str(metadata.get('mpris:artUrl', ''))},
                     'position': position, 'playing': props.get('PlaybackStatus') == 'Playing',
                     'volume': max(0.0, min(1.0, _number(props['Volume']))) if 'Volume' in props else None,
                     'capabilities': {'play_pause': controllable and bool(props.get('CanPause') if props.get('PlaybackStatus') == 'Playing' else props.get('CanPlay')),
                                      'next': controllable and bool(props.get('CanGoNext')),
                                      'previous': controllable and bool(props.get('CanGoPrevious')),
                                      'seek': controllable and bool(props.get('CanSeek')) and valid_id,
                                      'volume': controllable and 'Volume' in props,
                                      'device': False}, 'devices': []}
            try:
                devices, streams = self._audio_state(process_id=pid, player_hint=identity or player,
                                                    track_title=state['track']['title'])
                state['devices'] = devices
                state['capabilities']['device'] = bool(devices and streams)
            except PlaybackError:
                pass
            self._last = state
            return state
        except PlaybackError as exc:
            self._last = _empty(str(exc))
            return self._last

    def command(self, action, value=None):
        if action not in ('play_pause', 'next', 'previous', 'seek', 'volume', 'device'):
            return {'ok': False, 'error': '不支持的音乐操作'}
        state = self.snapshot()
        if not state['available']:
            return {'ok': False, 'error': state['error'], 'state': state}
        if not state['capabilities'].get(action):
            return {'ok': False, 'error': '当前播放器不支持此操作', 'state': state}
        try:
            if action == 'device':
                result = self.select_device(value, process_id=state.get('process_id'),
                                            player_hint=state['identity'] or state['player'],
                                            track_title=state['track']['title'])
                if not result['ok']:
                    return result
            elif action in ('play_pause', 'next', 'previous'):
                method = {'play_pause': 'PlayPause', 'next': 'Next', 'previous': 'Previous'}[action]
                self._dbus(state['player'], PLAYER_INTERFACE + '.' + method)
            elif action == 'seek':
                seconds = self._validated_number(value)
                duration = state['track']['duration']
                if seconds < 0 or (duration and seconds > duration):
                    raise PlaybackError('播放位置超出歌曲范围')
                self._dbus(state['player'], PLAYER_INTERFACE + '.SetPosition',
                           state['track']['id'], str(round(seconds * 1_000_000)))
            elif action == 'volume':
                volume = self._validated_number(value)
                if not 0 <= volume <= 1:
                    raise PlaybackError('音量必须在 0 到 1 之间')
                self._dbus(state['player'], 'org.freedesktop.DBus.Properties.Set',
                           PLAYER_INTERFACE, 'Volume', f'<double {volume}>')
            return {'ok': True, 'state': self.snapshot()}
        except PlaybackError as exc:
            return {'ok': False, 'error': str(exc), 'state': state}

    @staticmethod
    def _validated_number(value):
        if isinstance(value, bool) or value is None:
            raise PlaybackError('操作参数必须是有限数值')
        number = _number(value, None)
        if number is None:
            raise PlaybackError('操作参数必须是有限数值')
        return number

    def _audio_json(self, kind):
        try:
            value = json.loads(self._run('pactl', '-f', 'json', 'list', kind))
        except (ValueError, TypeError) as exc:
            raise PlaybackError('无法读取本机音频设备') from exc
        if not isinstance(value, list):
            raise PlaybackError('无法读取本机音频设备')
        return [item for item in value if isinstance(item, dict)]

    @staticmethod
    def _matching_streams(streams, process_id=None, player_hint='', track_title=''):
        hint = str(player_hint).lower().removeprefix(PLAYER_PREFIX.lower())
        browser = bool(_BROWSER.search(hint))
        matches = []
        for stream in streams:
            props = stream.get('properties') or {}
            if not isinstance(props, dict) or not str(stream.get('index', '')).isdigit():
                continue
            application = ' '.join(str(props.get(key, '')) for key in
                                   ('application.name', 'application.id', 'application.process.binary')).lower()
            media = str(props.get('media.name', '')).lower()
            pid_match = process_id is not None and str(props.get('application.process.id')) == str(process_id)
            family = ('adam' if re.match(r'^adam(?:[._-]|$)', hint) else
                      'cider' if re.match(r'^cider2?(?:[._-]|$)', hint) else
                      'apple' if _NATIVE.match(hint) else '')
            native_pattern = {'adam': r'\badam\b', 'cider': r'\bcider2?\b',
                              'apple': r'\bapple[ _-]?music\b'}.get(family)
            native_match = bool(native_pattern and re.search(native_pattern, application))
            # Browser audio processes can serve several tabs. Require the track
            # title as well, instead of moving unrelated tabs by browser PID.
            browser_match = bool(browser and _BROWSER.search(application) and len(str(track_title).strip()) >= 3
                                 and str(track_title).strip().lower() in media)
            if (pid_match and not browser) or native_match or browser_match:
                matches.append(stream)
        return matches

    def _audio_state(self, *, process_id=None, player_hint='', track_title=''):
        sinks = self._audio_json('sinks')
        streams = self._matching_streams(self._audio_json('sink-inputs'), process_id, player_hint, track_title)
        try:
            default = self._run('pactl', 'get-default-sink')
        except PlaybackError:
            default = ''
        selected = {str(stream.get('sink', '')) for stream in streams}
        devices = [{'id': str(sink['index']), 'name': str(sink.get('name', '')),
                    'description': str(sink.get('description') or sink.get('name') or sink['index']),
                    'selected': str(sink['index']) in selected,
                    'default': str(sink.get('name', '')) == default,
                    'scope': 'local'} for sink in sinks if str(sink.get('index', '')).isdigit()]
        return devices, streams

    def list_devices(self, *, process_id=None, player_hint='', track_title=''):
        try:
            return self._audio_state(process_id=process_id, player_hint=player_hint,
                                     track_title=track_title)[0]
        except PlaybackError:
            return []

    def select_device(self, value, *, process_id=None, player_hint='', track_title=''):
        try:
            devices, streams = self._audio_state(process_id=process_id, player_hint=player_hint,
                                                track_title=track_title)
            target = next((item for item in devices if str(value) in (item['id'], item['name'])), None)
            if not target:
                raise PlaybackError('播放设备已断开或不存在')
            if not streams:
                raise PlaybackError('无法识别 Apple Music 的本机音频流，请先开始播放')
            moved = []
            for stream in streams:
                self._run('pactl', 'move-sink-input', str(stream['index']), target['id'])
                moved.append(str(stream['index']))
            return {'ok': True, 'device': target['id'], 'streams': moved, 'scope': 'local'}
        except PlaybackError as exc:
            return {'ok': False, 'error': str(exc)}
