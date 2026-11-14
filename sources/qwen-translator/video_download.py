"""Structured yt-dlp progress; never forward raw URLs or diagnostic output to Qt."""
import json
import math


PREFIX = 'QWEN_DOWNLOAD:'
DOWNLOAD_TEMPLATE = PREFIX + '{' + ','.join(
    f'"{key}":%({field}|null)j' for key, field in (
        ('status', 'progress.status'), ('downloaded', 'progress.downloaded_bytes'),
        ('total', 'progress.total_bytes'), ('estimate', 'progress.total_bytes_estimate'),
        ('speed', 'progress.speed'), ('eta', 'progress.eta'), ('vcodec', 'info.vcodec'),
    )) + '}'
POSTPROCESS_TEMPLATE = PREFIX + '{"postprocessor":%(progress.postprocessor|null)j,"status":%(progress.status|null)j}'


def progress_options():
    return ['--progress', '--newline', '--no-colors', '--progress-delta', '0.5',
            '--progress-template', 'download:' + DOWNLOAD_TEMPLATE,
            '--progress-template', 'postprocess:' + POSTPROCESS_TEMPLATE]


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def size(value):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}'
        value /= 1024


def progress_event(line):
    text = line.decode('utf-8', errors='replace').strip()
    if not text.startswith(PREFIX):
        return None
    try:
        data = json.loads(text[len(PREFIX):])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if 'postprocessor' in data:
        if data.get('status') != 'started':
            return None
        message = '正在合并音视频…' if data['postprocessor'] == 'Merger' else '正在整理媒体文件…'
        return {'stage': '合并', 'message': message, 'progress': None, 'progress_label': '处理媒体',
                'transient': True}
    if data.get('status') not in {'downloading', 'finished'}:
        return None
    label = '下载音轨' if data.get('vcodec') == 'none' else '下载视频'
    downloaded = number(data.get('downloaded')) or 0
    total = number(data.get('total')) or number(data.get('estimate'))
    progress = min(downloaded / total, 1) if total else None
    parts = [label, '已下载 ' + size(downloaded)]
    if total:
        parts[-1] += ' / ' + ('' if number(data.get('total')) else '约 ') + size(total)
    if data['status'] == 'finished':
        progress = 1
        parts.append('下载完成')
    else:
        if (speed := number(data.get('speed'))) is not None:
            parts.append(size(speed) + '/s')
        if (eta := number(data.get('eta'))) is not None:
            minutes, seconds = divmod(round(eta), 60)
            parts.append(f'剩余 {minutes:02}:{seconds:02}')
    return {'stage': label, 'message': ' · '.join(parts), 'progress': progress,
            'progress_label': label + ' %p%', 'transient': True}
