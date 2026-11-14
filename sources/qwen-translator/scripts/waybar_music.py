#!/usr/bin/env python3
"""Waybar JSON output and controls for Qwen's local Apple Music integration.

Example custom module (adjust the script's absolute path for your checkout):
  "return-type": "json", "interval": 1,
  "exec": "python3 /path/to/scripts/waybar_music.py",
  "on-click": "playerctl play-pause",
  "on-click-middle": "playerctl previous",
  "on-click-right": "playerctl next"

The service must already be running; this script never launches a player or
downloads media. Keep the user's existing click and scroll bindings when
installing this reader. Optional CLI controls are available without binding them.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import shutil
import subprocess
import sys
import unicodedata
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


SERVICE_URL = 'http://127.0.0.1:18765'
MAX_RESPONSE = 2 * 1024 * 1024
MIN_DISPLAY_WIDTH, MAX_DISPLAY_WIDTH = 24, 240


def request(path, payload=None, *, opener=None):
    """Keep requests loopback-only and independent of proxy environment settings."""
    data = json.dumps(payload, allow_nan=False).encode('utf-8') if payload is not None else None
    req = Request(SERVICE_URL + path, data=data,
                  headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                  method='POST' if payload is not None else 'GET')
    client = opener or build_opener(ProxyHandler({}))
    with client.open(req, timeout=2.5) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError('音乐服务响应过大')
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError('音乐服务响应格式错误')
    return result


def _state(response):
    state = response.get('state')
    return state if isinstance(state, dict) else response


def _text(value, limit=500):
    clean = ''.join(' ' if unicodedata.category(char) in {'Cc', 'Cs'} else char
                    for char in str(value or ''))
    return ' '.join(clean.split())[:limit]


def _seconds(value):
    try:
        number = float(value or 0) if not isinstance(value, bool) else 0
        return max(0, number) if math.isfinite(number) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _time(value):
    seconds = int(min(359999, _seconds(value)))
    return f'{seconds // 60}:{seconds % 60:02d}'


def _width(text):
    """A conservative display-cell budget, including CJK and combining marks."""
    return sum(0 if unicodedata.category(char) in {'Mn', 'Me', 'Cf'} else
               2 if unicodedata.east_asian_width(char) in {'W', 'F'} else 1 for char in text)


def _clip(text, width):
    if _width(text) <= width:
        return text
    if width <= 0:
        return ''
    result = []
    occupied = 0
    for char in text:
        occupied += _width(char)
        if occupied > width - 1:
            break
        result.append(char)
    return ''.join(result).rstrip() + '…'


def _label(title, lyric, width):
    if not title:
        return _clip(lyric, width)
    if not lyric:
        return _clip(title, width)
    # Reserve most space for lyrics. Truncate the title independently so a long
    # song name cannot consume the current line or the progress timer.
    title_width = min(22, max(4, width // 3))
    if width - min(_width(title), title_width) - 3 < 2:
        return _clip(lyric, width)
    short_title = _clip(title, title_width)
    return short_title + ' · ' + _clip(lyric, width - _width(short_title) - 3)


def _lyric(state):
    lyrics = state.get('lyrics') or {}
    if isinstance(lyrics, str):
        return _text(lyrics, 8000), ''
    if not isinstance(lyrics, dict):
        return '', ''
    current = lyrics.get('current') or lyrics.get('current_line') or lyrics
    if isinstance(current, str):
        return _text(current, 8000), _text(lyrics.get('translation') or lyrics.get('translated'), 8000)
    if not isinstance(current, dict):
        return '', ''
    return (_text(current.get('text') or current.get('original') or current.get('source'), 8000),
            _text(current.get('translation') or current.get('translated') or current.get('zh'), 8000))


def render(response, *, max_length=70):
    """Produce one JSON object; only explicitly selected bilingual mode has a line break.

    Waybar custom JSON and escaping: https://github.com/Alexays/Waybar/blob/master/man/waybar-custom.5.scd
    Pango span markup: https://docs.gtk.org/Pango/pango_markup.html
    """
    state = _state(response)
    track = state.get('track') or {}
    if not isinstance(track, dict):
        track = {}
    title, artist = _text(track.get('title')), _text(track.get('artist'))
    available = state.get('available', bool(title))
    if not available or not title:
        return {'text': '♫', 'tooltip': html.escape(_text(state.get('error') or '等待 Apple Music 播放')),
                'class': 'idle', 'alt': 'idle', 'percentage': 0}
    original, translated = _lyric(state)
    icon = '♫' if state.get('playing') else '♫ Ⅱ'
    options = state.get('waybar')
    options = options if isinstance(options, dict) else {}
    display = options.get('display', 'translation')
    display = display if isinstance(display, str) and display in {'original', 'translation', 'bilingual'} else 'translation'
    show_title = options.get('show_title', True) is not False
    show_progress = options.get('show_progress', True) is not False
    try:
        width = max(MIN_DISPLAY_WIDTH, min(MAX_DISPLAY_WIDTH, int(max_length)))
    except (TypeError, ValueError, OverflowError):
        width = 70
    position, duration = _seconds(state.get('position')), _seconds(track.get('duration'))
    if duration:
        position = min(position, duration)
    timer = f' [{_time(position)}/{_time(duration)}]' if show_progress else ''
    lyric = (original or translated) if display == 'original' else (translated or original)
    label = _label(title if show_title else '', lyric, width - _width(icon + ' ' + timer))
    main_line = html.escape((icon + ' ' + label).rstrip() + timer)
    text = main_line
    if display == 'bilingual' and original and translated and original != translated:
        text = '<span size="small">' + html.escape(_clip(original, width)) + '</span>\n' + main_line
    lines = [title + (f' — {artist}' if artist else ''),
             f"{_time(position)} / {_time(duration)}"]
    if original:
        lines.append(original)
    if translated and translated != original:
        lines.append(translated)
    volume = state.get('volume')
    if isinstance(volume, (int, float)) and math.isfinite(volume):
        lines.append(f'音量 {round(max(0.0, min(1.0, volume)) * 100)}%')
    devices = state.get('devices') or []
    selected = [_text(item.get('description') or item.get('name')) for item in devices
                if isinstance(item, dict) and item.get('selected')]
    if selected:
        lines.append('播放设备：' + '、'.join(selected))
    lines.append('左键播放/暂停 · 中键上一首 · 右键下一首')
    percentage = round(position / duration * 100) if duration else 0
    return {'text': text,
            'tooltip': html.escape('\n'.join(lines)), 'class': 'playing' if state.get('playing') else 'paused',
            'alt': 'playing' if state.get('playing') else 'paused', 'percentage': percentage}


def control(action, value=None, *, requester=request):
    if action in ('volume_up', 'volume_down'):
        state = _state(requester('/music/state'))
        volume = state.get('volume')
        if not isinstance(volume, (int, float)) or isinstance(volume, bool) or not math.isfinite(volume):
            return {'ok': False, 'error': '当前播放器未提供音量控制'}
        value = round(max(0.0, min(1.0, volume + (0.05 if action == 'volume_up' else -0.05))), 4)
        action = 'volume'
    return requester('/music/control', {'action': action, 'value': value})


def choose_device(state, *, finder=shutil.which, runner=subprocess.run):
    devices = [item for item in state.get('devices', []) if isinstance(item, dict) and item.get('id') is not None]
    if not devices:
        return None
    labels = [f"{'●' if item.get('selected') else '○'} {_text(item.get('description') or item.get('name'))} [{item['id']}]"
              for item in devices]
    rofi, wofi = finder('rofi'), finder('wofi')
    if rofi:
        command = [rofi, '-dmenu', '-i', '-p', 'Apple Music 播放设备']
    elif wofi:
        command = [wofi, '--dmenu', '--prompt', 'Apple Music 播放设备']
    else:
        raise ValueError('播放设备选择器需要 rofi 或 wofi；也可使用 --list-devices 和 --action device --value ID')
    result = runner(command, input='\n'.join(labels), capture_output=True, text=True,
                    timeout=60, check=False)
    if result.returncode:
        return None
    choice = result.stdout.strip()
    if choice not in labels:
        return None
    return devices[labels.index(choice)]['id']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--action', choices=('play_pause', 'next', 'previous', 'seek', 'volume',
                                           'volume_up', 'volume_down', 'device'))
    group.add_argument('--devices', action='store_true', help='选择本机播放设备')
    group.add_argument('--list-devices', action='store_true', help='打印播放设备 JSON')
    parser.add_argument('--value')
    parser.add_argument('--max-length', type=int, default=70)
    args = parser.parse_args(argv)
    try:
        if args.action:
            value = args.value
            if args.action in ('seek', 'volume'):
                value = float(value) if value is not None else None
            result = control(args.action, value)
            if result.get('ok') is False:
                print(_text(result.get('error')), file=sys.stderr)
                return 1
            return 0
        response = request('/music/state')
        if args.list_devices:
            print(json.dumps(_state(response).get('devices', []), ensure_ascii=False))
        elif args.devices:
            selected = choose_device(_state(response))
            if selected is not None:
                result = control('device', selected)
                if result.get('ok') is False:
                    print(_text(result.get('error')), file=sys.stderr)
                    return 1
        else:
            print(json.dumps(render(response, max_length=max(8, args.max_length)), ensure_ascii=False))
        return 0
    except (OSError, URLError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        if args.action or args.devices or args.list_devices:
            print(f'Qwen 音乐服务：{_text(exc)}', file=sys.stderr)
            return 1
        print(json.dumps({'text': '♫', 'tooltip': 'Qwen 音乐服务未启动', 'class': 'offline', 'alt': 'offline', 'percentage': 0}, ensure_ascii=False))
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
