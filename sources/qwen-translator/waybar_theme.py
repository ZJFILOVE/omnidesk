"""Use the desktop app font only for Waybar's top-left lyric label.

The generated standalone CSS is also copied into a managed block at the end of
style.css. This lets it override existing rules without placing an @import
after GTK style declarations. All CSS outside the block remains untouched.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile


ROOT = Path(__file__).resolve().parent
START = '/* qwen-theme:start */'
END = '/* qwen-theme:end */'
BACKUP_NAME = 'style.css.before-qwen-theme.bak'
_MANAGED = re.compile(re.escape(START) + r'.*?' + re.escape(END), re.DOTALL)


def build_css(name, appearance=None):
    """Style the lyric label's font and an explicitly selected lyric color only.

    Caption sizes scale from the app's 22 px default to the bar's 13 px default,
    bounded to 10–20 px. An empty foreground preserves the existing bar color.
    """
    if name not in ('light', 'dark'):
        raise ValueError('Waybar theme must be light or dark')
    tokens = json.loads((ROOT / 'design/material-theme.json').read_text(encoding='utf-8'))
    families = tokens.get('fontFamilies') or ['Noto Sans CJK SC', 'Noto Sans', 'sans-serif']
    if not isinstance(families, list) or any(not isinstance(item, str) or not re.fullmatch(r'[A-Za-z0-9 _-]+', item) for item in families):
        raise ValueError('Invalid app font family')
    options = appearance if isinstance(appearance, dict) else {}
    custom_family = options.get('font_family')
    if isinstance(custom_family, str):
        custom_family = custom_family.strip()
        if (0 < len(custom_family) <= 100 and
                all(char.isalnum() or char in ' _-.' for char in custom_family)):
            families = [custom_family] + [item for item in families if item != custom_family]
    fonts = ', '.join(item if item in ('sans-serif', 'serif', 'monospace') else
                      json.dumps(item, ensure_ascii=False) for item in families)
    try:
        requested_size = float(options.get('size', 22))
        if isinstance(options.get('size'), bool) or not math.isfinite(requested_size):
            raise ValueError('Invalid lyric font size')
        size = max(10, min(20, round(requested_size * 13 / 22)))
    except (ValueError, TypeError, OverflowError):
        size = 13
    foreground = options.get('foreground')
    color = (f'\n    color: {foreground};' if isinstance(foreground, str) and
             re.fullmatch(r'#[0-9A-Fa-f]{6}', foreground) else '')
    return f'''/* Qwen lyric typography: {name}. Only custom/media is styled. */
window#waybar #custom-media {{
    background: transparent;
    border: none;
    border-radius: 0;
    box-shadow: none;
    min-width: 0;
    min-height: 0;
    margin: 0 0 0 12px;
    padding: 0 4px;
    color: inherit;
    font-family: {fonts};
    font-size: {size}px;
    font-weight: 500;{color}
}}
'''


def _atomic_write(path, content, *, exclusive=False, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive and path.exists():
        return False
    if path.is_file() and path.read_bytes() == content:
        return False
    descriptor, name = tempfile.mkstemp(prefix='.qwen-theme-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            try:
                os.link(temporary, path)
            except FileExistsError:
                return False
        else:
            os.replace(temporary, path)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def sync_theme(name, config_dir=None, appearance=None):
    """Install/update lyric typography, preserving bar styles and their first backup.

    Return the generated qwen-theme.css path. No file is rewritten when its
    bytes are unchanged. This function does not restart or signal Waybar.
    """
    css = build_css(name, appearance)
    directory = (Path(config_dir) if config_dir is not None else
                 Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'waybar')
    style = directory / 'style.css'
    original = style.read_bytes() if style.exists() else b''
    text = original.decode('utf-8')
    matches = list(_MANAGED.finditer(text))
    if text.count(START) != len(matches) or text.count(END) != len(matches):
        raise ValueError('Waybar 的 Qwen 字体标记不完整，请修复后重试。')
    block = START + '\n' + css.rstrip() + '\n' + END
    if matches:
        # Preserve every character outside managed sections, even when cleaning
        # up duplicate sections left by an older/manual installation.
        chunks, previous = [], 0
        for index, match in enumerate(matches):
            chunks.extend((text[previous:match.start()], block if index == 0 else ''))
            previous = match.end()
        chunks.append(text[previous:])
        updated = ''.join(chunks)
    else:
        updated = text + ('\n' if text and not text.endswith('\n') else '') + '\n' + block + '\n'
    mode = stat.S_IMODE(style.stat().st_mode) if style.exists() else 0o644
    if style.exists():
        _atomic_write(directory / BACKUP_NAME, original, exclusive=True, mode=mode)
    generated = directory / 'qwen-theme.css'
    _atomic_write(generated, css.encode('utf-8'))
    _atomic_write(style, updated.encode('utf-8'), mode=mode)
    return generated


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--theme', required=True, choices=('light', 'dark'))
    parser.add_argument('--config-dir', type=Path)
    args = parser.parse_args(argv)
    try:
        print(sync_theme(args.theme, args.config_dir))
        return 0
    except (OSError, ValueError) as exc:
        parser.exit(1, f'Waybar 歌词字体同步失败：{exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
