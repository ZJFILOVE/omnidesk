"""Install Qwen lyrics in Waybar's existing media slot, preserving a backup."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import shlex
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]


def configure(text, root=ROOT, python=sys.executable):
    # Replace only the media object's text, retaining comments and every other
    # module verbatim. A small scanner handles braces inside quoted commands.
    match = re.search(r'"custom/media"\s*:\s*\{', text)
    if not match:
        raise ValueError('Waybar 配置未包含 custom/media，请先将其加入 modules-left 或 modules-right。')
    start, end = match.end() - 1, None
    depth, quoted, escaped, comment = 0, False, False, None
    i = start
    while i < len(text):
        c = text[i]
        pair = text[i:i + 2]
        if comment == 'line':
            if c == '\n':
                comment = None
        elif comment == 'block':
            if pair == '*/':
                comment = None
                i += 1
        elif quoted:
            if escaped:
                escaped = False
            elif c == '\\':
                escaped = True
            elif c == '"':
                quoted = False
        elif pair in ('//', '/*'):
            comment = 'line' if pair == '//' else 'block'
            i += 1
        elif c == '"':
            quoted = True
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
        i += 1
    if end is None:
        raise ValueError('Waybar 的 custom/media 配置括号不完整。')
    command = shlex.join([str(python), str(root / 'scripts/waybar_music.py')])
    module = {'exec': command, 'return-type': 'json', 'interval': 1,
              'format': '{text}', 'tooltip': True, 'hide-empty-text': True,
              'on-click': 'playerctl play-pause',
              'on-click-middle': 'playerctl previous',
              'on-click-right': 'playerctl next'}
    indent = re.search(r'(?m)^([ \t]*)"custom/media"\s*:', text[:start])
    prefix = indent.group(1) if indent else '    '
    body = json.dumps(module, ensure_ascii=False, indent=4).splitlines()
    replacement = body[0] + '\n' + '\n'.join(prefix + line for line in body[1:])
    return text[:start] + replacement + text[end:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path.home() / '.config/waybar/config.jsonc')
    parser.add_argument('--install', action='store_true', help='Back up and replace the media module.')
    args = parser.parse_args()
    text = args.config.read_text(encoding='utf-8')
    changed = configure(text)
    if not args.install:
        print(changed)
        return
    if text == changed:
        print('Waybar 已配置 Qwen 歌词。')
        return
    backup = args.config.with_name(args.config.name + '.bak-qwen-music-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(args.config, backup)
    args.config.write_text(changed, encoding='utf-8')
    print('已接入 Waybar 当前歌词。备份：' + str(backup))


if __name__ == '__main__':
    main()
