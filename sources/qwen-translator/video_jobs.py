"""File/YouTube -> timed SRT -> optional synchronization and Chinese translation."""
import asyncio
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sys
import tempfile
import urllib.parse

from local_subtitles import BINARY, MODEL, VAD, asr_available
from text_translation import run_process
from subtitle_sources import episode, search_name
from video_cache import source_directory
from video_download import progress_event, progress_options
from video_subtitles import (Cue, atomic_write, chinese, compose, decode, parse_srt,
                             transform, translate_cues)

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / 'video-cache'
TEXT_CODECS = {'subrip', 'ass', 'ssa', 'webvtt', 'mov_text', 'text'}
SUBTITLE_ORIGINS = {'youtube_manual': 'YouTube 上传字幕', 'youtube_auto': 'YouTube 自动字幕',
                    'asr': '本地 ASR 识别原文', 'external': '外部字幕', 'embedded': '内嵌字幕'}


def sidecar_language(path, *, read_content=True):
    name = path.stem.lower()
    if re.search(r'(?:[._-](?:zh|chs|cht|chi|zho)(?:[._-]|$))|简体|繁体|双语', name):
        return 'zh'
    if re.search(r'(?:[._-](?:en|eng)(?:[._-]|$))', name):
        return 'en'
    if read_content and path.suffix.lower() == '.srt' and path.stat().st_size <= 16 * 1024 * 1024:
        try:
            if chinese(parse_srt(decode(path.read_bytes()))):
                return 'zh'
        except (ValueError, UnicodeError):
            pass
    return 'und'


def tool(name):
    local = ROOT / '.venv/bin' / name
    value = str(local) if local.is_file() else shutil.which(name)
    if not value:
        raise RuntimeError(f'缺少 {name}；视频依赖见 requirements-video.txt，FFmpeg/播放器通过系统包管理器安装。')
    return value


async def command(args, *, timeout=120, data=None, on_line=None):
    code, out, err = await run_process(args, timeout=timeout, data=data, on_line=on_line)
    if code:
        # Full command errors can contain signed URLs or credentials; keep them local to the process.
        raise RuntimeError(f'{Path(str(args[0])).name} 处理失败（退出码 {code}）。请检查文件、网络和依赖。')
    return out


def youtube_url(value):
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in {'https', 'http'} or parts.hostname not in {
            'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'} or parts.username or parts.password:
        raise ValueError('请输入有效的 YouTube 视频链接，或选择本地视频文件。')
    return value


def file_identity(path):
    path = Path(path).expanduser().resolve()
    stat = path.stat()
    return [str(path), stat.st_size, stat.st_mtime_ns]


def directory(source):
    if source.startswith(('https://', 'http://')):
        youtube_url(source)
    root = source_directory(CACHE, source)
    root.mkdir(parents=True, exist_ok=True)
    return root


async def probe(source):
    raw = await command([tool('ffprobe'), '-v', 'error', '-show_entries',
        'format=duration:stream=index,codec_type,codec_name:stream_tags=language,title', '-of', 'json', source], timeout=180)
    data = json.loads(raw)
    duration = float(data.get('format', {}).get('duration') or 0)
    if duration <= 0:
        raise ValueError('无法确定视频时长；当前视频模式仅支持录播文件。')
    return data, duration


async def inspect(source, *, cache_root=None, sidecar_contents=True):
    if source.startswith(('https://', 'http://')):
        youtube_url(source)
        return {'source': source, 'title': 'YouTube 视频', 'audio': [], 'subtitles': [], 'query': ''}
    source = str(Path(source).expanduser().resolve(strict=True))
    cached = (Path(cache_root) if cache_root is not None else directory(source)) / 'inspection.json'
    if cached.is_file():
        info = json.loads(cached.read_text())
        # Sidecar availability may change without the video changing.
        info['subtitles'] = [s for s in info['subtitles'] if 'stream' in s]
        path = Path(source)
        for other in sorted(path.parent.iterdir()):
            if other.suffix.lower() in {'.srt', '.ass', '.ssa', '.vtt'} and (other.stem == path.stem or other.stem.startswith(path.stem + '.')):
                info['subtitles'].append({'path': str(other), 'title': other.name, 'language': sidecar_language(other, read_content=sidecar_contents)})
        return info
    data, duration = await probe(source)
    audio, subtitles = [], []
    for stream in data.get('streams', []):
        tags = stream.get('tags', {})
        title = ' · '.join(str(v) for v in (stream['index'], tags.get('language', 'und'),
                                            tags.get('title', ''), stream.get('codec_name', '')) if v != '')
        if stream['codec_type'] == 'audio':
            audio.append({'index': stream['index'], 'title': title, 'language': tags.get('language', 'und')})
        elif stream['codec_type'] == 'subtitle' and stream['codec_name'] in TEXT_CODECS:
            subtitles.append({'stream': stream['index'], 'title': '内嵌 · ' + title,
                              'language': tags.get('language', 'und')})
    path = Path(source)
    for other in sorted(path.parent.iterdir()):
        if other.suffix.lower() not in {'.srt', '.ass', '.ssa', '.vtt'}:
            continue
        if other.stem == path.stem or other.stem.startswith(path.stem + '.'):
            subtitles.append({'path': str(other), 'title': other.name, 'language': sidecar_language(other, read_content=sidecar_contents)})
    info = {'source': source, 'title': path.name, 'duration': duration, 'audio': audio,
            'subtitles': subtitles, 'query': search_name(path.name)}
    atomic_write(cached, json.dumps(info, ensure_ascii=False))
    return info


async def load_subtitle(path, root):
    path = Path(path).resolve(strict=True)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('字幕文件过大，请选择单集文本字幕。')
    text = decode(path.read_bytes())
    if path.suffix.lower() == '.srt':
        return parse_srt(text)
    if path.suffix.lower() not in {'.ass', '.ssa', '.vtt'}:
        raise ValueError('请导入 SRT、ASS、SSA 或 VTT 文本字幕。')
    with tempfile.TemporaryDirectory(dir=root, prefix='convert-') as tmp:
        decoded = Path(tmp) / ('input' + path.suffix.lower())
        decoded.write_text(text, encoding='utf-8')
        out = await command([tool('ffmpeg'), '-v', 'error', '-nostdin', '-i', decoded,
                             '-f', 'srt', 'pipe:1'])
        # YouTube rolling captions contain lines consisting of a single space.
        # FFmpeg preserves them, including before the first text line, where
        # an SRT parser would mistake them for the end of the cue. Drop these
        # placeholders while preserving FFmpeg's genuinely empty separators.
        converted = '\n'.join(line for line in decode(out).splitlines()
                              if not line or line.strip())
        return parse_srt(converted)


async def extract_subtitle(source, stream, root):
    path = root / f'embedded-{int(stream)}.srt'
    if not path.is_file():
        out = await command([tool('ffmpeg'), '-v', 'error', '-nostdin', '-i', source,
                    '-map', f'0:{int(stream)}', '-c:s', 'srt', '-f', 'srt', 'pipe:1'], timeout=1200)
        cues = parse_srt(decode(out))
        atomic_write(path, compose(cues))
    return parse_srt(path.read_text()), path


async def youtube_media(source, root, config, emit):
    await emit({'stage': '获取信息', 'message': '正在查询 YouTube 字幕和音轨…'})
    base = [tool('yt-dlp'), '--ignore-config', '--no-playlist', '--no-progress',
            '--no-cache-dir', '--js-runtimes', 'node']
    info = json.loads(await command([*base, '--skip-download', '--dump-single-json', source], timeout=180))
    if info.get('is_live') or info.get('live_status') in {'is_live', 'is_upcoming'}:
        raise ValueError('视频字幕模式暂不支持正在直播或尚未开播的视频。')
    subtitle = None
    subtitle_origin = None
    for kind in (() if config.get('force_asr') else ('subtitles', 'automatic_captions')):
        tracks = info.get(kind) or {}
        languages = ('zh-Hans', 'zh-CN', 'zh-Hant', 'zh-TW', 'zh', 'en', 'en-US', 'en-orig')
        if kind == 'automatic_captions':
            # Do not accidentally choose a platform auto-translation over original speech captions.
            language = info.get('language')
            languages = tuple(lang for lang in tracks if lang.endswith('-orig'))
            languages += ((language,) if language else ()) + ('en-orig', 'en')
        else:
            languages += tuple(lang for lang in tracks if lang != 'live_chat')
        selected = next((lang for lang in languages if lang in tracks), None)
        if selected:
            await emit({'stage': '获取字幕', 'message': f'下载 YouTube 字幕 · {selected}'})
            await command([*base, '--skip-download', '--write-subs' if kind == 'subtitles' else '--write-auto-subs',
                '--sub-langs', selected, '--sub-format', 'vtt', '-o', str(root / 'youtube.%(ext)s'), source], timeout=180)
            subtitle = next(iter(root.glob(f'youtube.{selected}.vtt')), None)
            if subtitle:
                subtitle_origin = 'youtube_manual' if kind == 'subtitles' else 'youtube_auto'
                break
            raise RuntimeError('YouTube 字幕下载未生成完整文件，请重试。')
    if subtitle is None:
        await emit({'stage': '获取字幕', 'message': '已选择本地识别，将获取音轨。' if config.get('force_asr')
                    else '未找到可用平台字幕，将获取音轨并进行本地识别。'})
    offline = config.get('download_video', True)
    media = None
    record = root / ('video-download.json' if offline else 'audio-download.json')
    if record.is_file():
        cached = Path(json.loads(record.read_text()).get('path', ''))
        if cached.is_file():
            media = str(cached)
    if media is None and (offline or subtitle is None):
        await emit({'stage': '下载', 'message': '正在下载视频…' if offline else '正在下载识别所需的音轨…',
                    'progress': None})
        async def on_line(line):
            if event := progress_event(line):
                await emit(event)
        template = root / ('video.%(ext)s' if offline else 'audio.%(ext)s')
        raw = await command([*base, '-f', 'bv*[height<=1080]+ba/b[height<=1080]' if offline else 'ba/b',
            *progress_options(), '--print', 'after_move:filepath', '-o', str(template), source],
            timeout=7200, on_line=on_line)
        media = raw.decode().strip().splitlines()[-1]
        if not Path(media).is_file() or Path(media).resolve().parent != root.resolve():
            raise RuntimeError('下载未产生完整媒体文件。')
        atomic_write(record, json.dumps({'path': media}))
        await emit({'stage': '下载完成', 'message': '媒体下载完成，正在准备字幕…', 'progress': None})
    return media, subtitle, str(info.get('title', 'YouTube 视频')), media if offline else source, subtitle_origin


async def asr_file(source, duration, audio_stream, root, emit, backend='vulkan', *, reset_cache=False):
    if backend not in {'vulkan', 'cpu'}:
        raise ValueError('ASR 后端无效。')
    if not asr_available():
        raise RuntimeError('没有可用字幕且本地 ASR 尚未安装。')
    model_key = [file_identity(MODEL), file_identity(VAD), file_identity(BINARY), audio_stream, 'srt-120-v1']
    key = hashlib.sha256(json.dumps(model_key).encode()).hexdigest()[:16]
    checkpoint = root / f'asr-{key}.json'
    state = {'next': 0, 'cues': []}
    if not reset_cache and checkpoint.is_file():
        try:
            saved = json.loads(checkpoint.read_text())
            at = saved['next']
            if (type(at) not in (int, float) or not math.isfinite(at)
                    or not 0 <= at <= duration or not isinstance(saved['cues'], list)):
                raise ValueError('Invalid ASR checkpoint')
            for item in saved['cues']:
                cue = Cue(**item)
                if (type(cue.start) is not int or type(cue.end) is not int
                        or not 0 <= cue.start < cue.end <= round(at * 1000)
                        or not isinstance(cue.text, str) or not cue.text.strip()):
                    raise ValueError('Invalid ASR checkpoint cue')
            state = saved
        except (OSError, ValueError, TypeError, KeyError):
            await emit({'stage': '识别', 'message': '识别续做记录损坏，将从头识别；已有字幕成品已保留。'})
    if reset_cache:
        # Save the reset before starting work so an interrupted fresh run cannot
        # accidentally resume the previous recognition on the next attempt.
        atomic_write(checkpoint, json.dumps(state))
    cues = [Cue(**item) for item in state['cues']]
    at = state['next']
    while at < duration:
        seconds = min(120, duration - at)
        await emit({'stage': '识别', 'message': f'本地识别 {at / 60:.1f} / {duration / 60:.1f} 分钟',
                    'progress': at / duration})
        with tempfile.TemporaryDirectory(dir=root, prefix='asr-') as tmp:
            wav = Path(tmp) / 'audio.wav'
            mapping = ['-map', f'0:{int(audio_stream)}'] if audio_stream is not None else ['-map', '0:a:0']
            await command([tool('ffmpeg'), '-v', 'error', '-nostdin', '-ss', str(at), '-i', source,
                           '-t', str(seconds), *mapping, '-ar', '16000', '-ac', '1', '-y', wav], timeout=300)
            output = await command([BINARY, '-m', MODEL, '--vad', VAD, '--vad-maxseg', '10000',
                                    '--backend', backend, '--srt', '-a', wav], timeout=300)
            text = re.sub(r'<\|[^|]*\|>', '', output.decode(errors='replace')).strip()
            if text:
                chunk = parse_srt(text)
                for cue in chunk:
                    start = cue.start + round(at * 1000)
                    end = min(cue.end + round(at * 1000), round(duration * 1000))
                    if end > start:
                        cues.append(replace(cue, start=start, end=end))
        at += seconds
        atomic_write(checkpoint, json.dumps({'next': at, 'cues': [c.__dict__ for c in cues]}, ensure_ascii=False))
    if not cues:
        raise ValueError('音轨中没有识别出有效语音。')
    return cues


async def align(cues, reference, root, config, emit):
    await emit({'stage': '对齐', 'message': '正在对齐语音活动与字幕时间轴…'})
    source = root / 'alignment-input.srt'
    target = root / 'alignment-output.srt'
    atomic_write(source, compose(cues))
    payload = {'reference': str(reference), 'subtitle': str(source), 'output': str(target),
               'split': config.get('sync_mode') == 'split'}
    if Path(reference).suffix.lower() != '.srt':
        payload['audio_stream'] = config.get('audio_stream')
    raw = await command([sys.executable, ROOT / 'video_sync.py'],
                        data=json.dumps(payload).encode(), timeout=1800)
    result = json.loads(raw)
    if not result.get('success'):
        raise RuntimeError('自动对齐置信度不足，原字幕已保留。请换匹配片源的字幕、指定参考 SRT，或选择手动校准。')
    aligned = parse_srt(target.read_text())
    if len(aligned) != len(cues):
        raise RuntimeError('对齐改变了字幕条数，已停止以保留完整字幕。')
    await emit({'stage': '对齐', 'message': f"已生成对齐候选：偏移 {result.get('offset_seconds')} 秒，倍率 {result.get('scale')}；请预览核对。"})
    return aligned, result


async def load_existing(config, emit):
    """Load a selected local subtitle without recognition, translation or alignment."""
    source = config['source']
    if source.startswith(('https://', 'http://')):
        raise ValueError('直接加载字幕请先选择本地或已挂载的视频；在线链接请使用准备字幕。')
    source = str(Path(source).expanduser().resolve(strict=True))
    if not Path(source).is_file():
        raise ValueError('请选择一个本地视频文件。')
    cache_root = directory(source)
    if config.get('subtitle_path'):
        cues = await load_subtitle(config['subtitle_path'], cache_root)
    elif config.get('subtitle_stream') is not None:
        cues, _ = await extract_subtitle(source, config['subtitle_stream'], cache_root)
    else:
        raise ValueError('请先选择已发现或已导入的字幕文件。')
    body = compose(cues)
    key = hashlib.sha256(body.encode()).hexdigest()[:20]
    root = cache_root / 'loaded' / key
    path = root / 'original.srt'
    atomic_write(path, body)
    result = {'source': source, 'media': source, 'title': Path(source).name,
              'subtitle': str(path), 'original': str(path), 'timed_source': str(path),
              'bilingual': None, 'directory': str(root), 'cues': len(cues),
              'alignment': None, 'loaded': True, 'subtitle_suffix': 'original',
              'origin': 'external' if config.get('subtitle_path') else 'embedded'}
    result['origin_label'] = SUBTITLE_ORIGINS[result['origin']]
    atomic_write(root / 'result.json', json.dumps(result, ensure_ascii=False, indent=2))
    atomic_write(cache_root / 'result.json', json.dumps(result, ensure_ascii=False, indent=2))
    await emit({'stage': '完成', 'message': f'已加载 {len(cues)} 条原字幕，可直接播放或保存；需要中文时点击“准备 / 翻译中文字幕”。',
                'progress': 1, 'result': result})
    return result


async def obtain_original(config, emit):
    """Acquire source captions/ASR without translating or changing their timeline."""
    return await prepare({**config, 'make_chinese': False, 'sync_mode': 'none',
                          'reference_path': '', 'offset_ms': 0, 'scale': 1, 'original_only': True}, emit)


async def prepare(config, emit, *, cache_root=None, inspection=None):
    # An explicit fresh recognition also bypasses embedded/platform subtitles.
    if config.get('reset_asr_cache'):
        config = {**config, 'force_asr': True}
    source = config['source']
    source_fingerprint = None
    if not source.startswith(('https://', 'http://')):
        source = str(Path(source).expanduser().resolve(strict=True))
        if config.get('nas_writeback'):
            from nas_io import fingerprint
            await emit({'stage': '读取校验', 'message': '记录 NAS 视频版本，供字幕完成后安全回传。'})
            source_fingerprint = fingerprint(source)
    root = Path(cache_root) if cache_root is not None else directory(source)
    root.mkdir(parents=True, exist_ok=True)
    online = source.startswith(('https://', 'http://'))
    imported = config.get('subtitle_path')
    selected_stream = config.get('subtitle_stream')
    reference = config.get('reference_path')
    title, playable, media = Path(source).name, source, source
    platform_origin = None
    if online:
        media, platform_subtitle, title, playable, platform_origin = await youtube_media(source, root, config, emit)
        imported = imported or platform_subtitle
        tracks = []
        audio_stream = None
    else:
        await emit({'stage': '检查', 'message': '检查视频、音轨和现有字幕…'})
        info = inspection if inspection is not None else await inspect(source)
        title, tracks = info['title'], info['subtitles']
        audio_stream = config.get('audio_stream')
        if audio_stream is None and info['audio']:
            audio_stream = next((a['index'] for a in info['audio'] if a['language'] in {'en', 'eng'}), info['audio'][0]['index'])
        config = {**config, 'audio_stream': audio_stream}
    if not imported and selected_stream is None and not config.get('force_asr'):
        ranked = sorted(tracks, key=lambda t: t.get('language') in {'zh', 'zho', 'chi'}, reverse=True)
        # Sidecar subtitles are preferred over non-Chinese embedded tracks.
        ranked.sort(key=lambda t: (t.get('language') in {'zh', 'zho', 'chi'}, bool(t.get('path')),
                                    t.get('language') in {'en', 'eng'}), reverse=True)
        if ranked:
            imported = ranked[0].get('path')
            selected_stream = ranked[0].get('stream')
    origin = 'asr'
    if imported and not config.get('force_asr'):
        cues = await load_subtitle(imported, root)
        origin = platform_origin if online and not config.get('subtitle_path') else 'external'
    elif selected_stream is not None and not config.get('force_asr'):
        await emit({'stage': '提取', 'message': '正在提取内嵌文本字幕，网络文件可能需要等待…'})
        cues, _ = await extract_subtitle(source, selected_stream, root)
        origin = 'embedded'
    else:
        if not media:
            raise ValueError('没有可用于识别的音轨。')
        _, duration = await probe(media)
        cues = await asr_file(media, duration, audio_stream, root, emit, config.get('asr_backend', 'vulkan'),
                             reset_cache=config.get('reset_asr_cache', False))
    cache_root = root
    # Each distinct preparation has its own artifacts; failed jobs cannot corrupt
    # the last successful result. Translation checkpoints remain shared by text.
    signature = {k: config.get(k) for k in ('sync_mode', 'offset_ms', 'scale', 'subtitle_content',
        'make_chinese', 'audio_stream', 'text_provider', 'text_endpoint', 'text_model',
        'codex_model', 'codex_reasoning_effort', 'antigravity_model', 'api_format')}
    if reference:
        signature['reference'] = file_identity(reference)
    preparation = hashlib.sha256(json.dumps([compose(cues), signature], sort_keys=True).encode()).hexdigest()[:20]
    root = cache_root / 'prepared' / preparation
    root.mkdir(parents=True, exist_ok=True)
    atomic_write(root / 'original.srt', compose(cues))
    alignment = None
    mode = config.get('sync_mode', 'auto')
    if mode in {'auto', 'split'} and origin == 'external':
        if reference:
            ref_cues = await load_subtitle(reference, root)
            ref_path = root / 'reference.srt'
            atomic_write(ref_path, compose(ref_cues))
            reference = ref_path
        elif not online:
            ref_track = next((t for t in tracks if 'stream' in t and t.get('language') in {'en', 'eng'}), None)
            if ref_track:
                await emit({'stage': '提取', 'message': '提取视频内英文字幕作为时间轴参考…'})
                _, reference = await extract_subtitle(source, ref_track['stream'], cache_root)
        if reference or media:
            cues, alignment = await align(cues, reference or media, root, config, emit)
    if config.get('offset_ms') or config.get('scale', 1) != 1:
        cues = transform(cues, float(config.get('scale', 1)), float(config.get('offset_ms', 0)))
    atomic_write(root / 'source.srt', compose(cues))
    language = config.get('subtitle_content', 'auto')
    is_chinese = language == 'zh' or (language == 'auto' and chinese(cues))
    output = root / 'source.srt'
    translated = None
    if config.get('make_chinese', True):
        if is_chinese:
            atomic_write(root / 'zh.srt', compose(cues))
        else:
            translated = await translate_cues(cues, config, cache_root, emit)
            atomic_write(root / 'zh.srt', compose(translated))
            bilingual = [replace(a, text=' '.join(b.text.split()) + '\n' + ' '.join(a.text.split()))
                         for a, b in zip(cues, translated)]
            atomic_write(root / 'bilingual.srt', compose(bilingual))
        output = root / 'zh.srt'
    result = {'source': source, 'media': playable, 'title': title, 'subtitle': str(output),
              'origin': origin, 'origin_label': SUBTITLE_ORIGINS[origin],
              'original_only': bool(config.get('original_only')),
              'subtitle_suffix': 'zh' if config.get('make_chinese', True) else 'source',
              'original': str(root / 'original.srt'), 'timed_source': str(root / 'source.srt'),
              'bilingual': str(root / 'bilingual.srt') if translated else None,
              'directory': str(root), 'cues': len(cues), 'alignment': alignment}
    if source_fingerprint is not None:
        if fingerprint(source) != source_fingerprint:
            raise ValueError('处理期间视频内容已变化，请重新准备字幕；本次结果不会自动回传。')
        result['source_fingerprint'] = source_fingerprint
    atomic_write(root / 'result.json', json.dumps(result, ensure_ascii=False, indent=2))
    atomic_write(cache_root / 'result.json', json.dumps(result, ensure_ascii=False, indent=2))
    await emit({'stage': '完成', 'message': f'已准备 {len(cues)} 条外挂 SRT 字幕', 'progress': 1, 'result': result})
    return result
