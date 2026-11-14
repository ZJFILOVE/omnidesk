"""Manage generated video cache without following links or download metadata."""
import hashlib
import json
from pathlib import Path
import re
import shutil


def source_directory(cache, source):
    if source.startswith(('https://', 'http://')):
        identity = source
    else:
        path = Path(source).expanduser().resolve()
        stat = path.stat()
        identity = [str(path), stat.st_size, stat.st_mtime_ns]
    key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]
    return Path(cache) / key


def _read_result(root):
    path = root / 'result.json'
    if root.is_symlink() or path.is_symlink():
        return None
    try:
        result = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(result, dict) or not all(
            isinstance(result.get(key), str) and result[key].strip()
            for key in ('source', 'title', 'media', 'subtitle')):
        return None
    return result


def cached_videos(cache):
    """List completed results by recency, reading only local cache metadata.

    Do not stat source/media paths here: they may live on an unavailable NAS.
    Older caches already contain titles, so no migration or network is needed.
    """
    cache = Path(cache)
    if cache.is_symlink():
        return []
    try:
        roots = list(cache.iterdir())
    except OSError:
        return []
    entries = []
    for root in roots:
        if not re.fullmatch(r'[0-9a-f]{24}', root.name):
            continue
        result = _read_result(root)
        if result is None:
            continue
        try:
            updated = (root / 'result.json').stat().st_mtime_ns
        except OSError:
            continue
        entries.append({'title': result['title'], 'source': result['source'], 'updated': updated})
    return sorted(entries, key=lambda entry: (-entry['updated'], entry['title'], entry['source']))


def cached_result(root, source):
    """Restore a result only when its media and advertised subtitles still exist."""
    result = _read_result(Path(root))
    if result is None or result['source'] != source:
        return None
    required = ['subtitle']
    if not result.get('loaded') and not result.get('original_only'):
        required += ['original', 'timed_source']
    if result.get('bilingual'):
        required.append('bilingual')
    try:
        for key in required:
            value = result.get(key)
            if not isinstance(value, str) or not value or not Path(value).is_file():
                return None
        media = result['media']
        if not media.startswith(('https://', 'http://')) and not Path(media).is_file():
            return None
    except (OSError, ValueError):
        return None
    return result


def delete_cache(cache, kind, source=None):
    """Delete one category within managed cache entries; return entry count."""
    if kind not in {'subtitles', 'video', 'audio'}:
        raise ValueError('未知缓存类型。')
    cache = Path(cache)
    if cache.is_symlink():
        raise ValueError('缓存目录是符号链接，无法清理。')
    if not cache.exists():
        return 0
    roots = [source_directory(cache, source)] if source else list(cache.iterdir())
    count = 0
    for root in roots:
        if not re.fullmatch(r'[0-9a-f]{24}', root.name) or root.is_symlink() or not root.is_dir():
            continue
        for path in root.iterdir():
            category = ('video' if path.name.startswith('video.') or path.name == 'video-download.json'
                        else 'audio' if path.name.startswith('audio.') or path.name == 'audio-download.json'
                        else 'subtitles')
            if category != kind:
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            count += 1
    return count
