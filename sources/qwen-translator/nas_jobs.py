"""NAS subtitle selection; existing video preparation owns translation and ASR."""
from dataclasses import replace
import json
from pathlib import Path
import re

from nas_api import digest
from nas_queue import processing_config
from nas_io import (Waiting, file_hash, fingerprint, managed_stale, remember_source,
                    resolve_media, valid_srt)
from subtitle_sources import download, search, search_name
import video_jobs
from video_subtitles import atomic_write, chinese, compose, decode, parse_srt


class ManualChoice(Exception):
    def __init__(self, message, **choices):
        super().__init__(message)
        self.choices = choices


async def read_text_subtitle(path, root):
    path = Path(path)
    if path.suffix.lower() == '.srt':
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('字幕文件过大。')
        body = decode(path.read_bytes())
        blocks = re.split(r'\n\s*\n', body.replace('\r\n', '\n').replace('\r', '\n').strip())
        ids = [b.splitlines()[0].strip() for b in blocks]
        cues = parse_srt(body)
        if (len(cues) != len(blocks) or not all(i.isdecimal() for i in ids)
                or len(ids) != len(set(ids))):
            raise ValueError('原字幕包含无效条目、重复编号或空文本，需人工检查。')
        return cues
    return await video_jobs.load_subtitle(path, root)


def release_names(media):
    names = [Path(media['path']).stem]
    if media.get('release'):
        names.append(media['release'])
    if media.get('original'):
        names.append(Path(media['original']).stem)
    # Require release/quality identifiers, not only a film title or season/episode.
    return [name for name in names if re.search(r'(?i)\b(?:2160p|1080p|720p|bluray|web[ ._-]?dl|webrip)\b', name)]


def exact_release(title, media):
    tokens = re.findall(r'[^\W_]+', title.casefold())
    padded = '|' + '|'.join(tokens) + '|'
    return any('|' + '|'.join(re.findall(r'[^\W_]+', name.casefold())) + '|' in padded
               for name in release_names(media))


def looks_english(cues):
    words = re.findall(r'[a-z]+', ' '.join(c.text for c in cues).lower())
    common = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'you', 'your', 'i', 'we', 'they',
              'he', 'she', 'it', 'and', 'of', 'to', 'in', 'that', 'this', 'what', 'hello',
              'world', 'yes', 'no', 'not', 'have', 'do', 'for', 'with', 'on', 'my', 'me'}
    return bool(words) and sum(w in common for w in words) >= max(1, len(words) * .15)


async def website_subtitle(job, root, token, emit, *, english_only=False):
    override = job['override'] or {}
    provider = job['config'].get('search_provider', 'none')
    if provider == 'none' or override.get('skip_search'):
        return None
    await emit({'stage': '搜索字幕', 'message': '优先查找匹配发布版本的网络字幕…'})
    candidates = []
    try:
        if override.get('candidate'):
            selected = override['candidate']
        else:
            candidates = await search(provider, search_name(Path(job['data']['path']).name), token)
            if not candidates:
                return None  # A successful, empty search is different from failed I/O.
            matched = [c for c in candidates if exact_release(c['title'], job['data'])]
            if len(matched) != 1:
                raise ManualChoice('网站有字幕，但无法唯一确认发布版本；请选择，或明确继续本地兜底。', candidates=candidates)
            selected = matched[0]
        await emit({'stage': '下载字幕', 'message': '下载匹配字幕，保存在本机缓存…'})
        folder = root / 'downloads' / digest([selected['provider'], selected['id']])[:20]
        files = await download(selected, folder, token)
        matched_files = [f for f in files if exact_release(f['title'], job['data'])]
        if english_only:
            english_files = [f for f in matched_files if re.search(r'(?i)(?:[._ -](?:en|eng|english)(?:[._ -]|$))|英文', f['title'])]
            if english_files:
                matched_files = english_files
        if len(matched_files) == 1:
            return matched_files[0]['path']
        # Manual selection of a release is not permission to pick a random episode from its ZIP.
        raise ManualChoice('下载包中需要核对具体字幕文件；请选择对应版本 / 季集。', files=files, candidates=[selected])
    except ManualChoice:
        raise
    except (RuntimeError, ValueError, OSError):
        raise ManualChoice('字幕网站读取或下载失败，可能需要验证；打开现有视频字幕页处理，或选择继续本地兜底。',
                           candidates=candidates) from None


def split_chinese(cues):
    zh, bilingual = [], []
    both = 0
    for cue in cues:
        lines = cue.text.splitlines()
        upper = [line for line in lines if re.search(r'[\u3400-\u9fff]', line)]
        lower = [line for line in lines if not re.search(r'[\u3400-\u9fff]', line) and re.search('[A-Za-z]', line)]
        zh.append(replace(cue, text='\n'.join(upper) if upper else cue.text))
        bilingual.append(replace(cue, text=' '.join(upper) + '\n' + ' '.join(lower)) if upper and lower else cue)
        both += bool(upper and lower)
    return zh, bilingual if both == len(cues) else None


def pair_existing(zh, english):
    result = []
    count = 0
    start = 0
    for c in zh:
        while start < len(english) and english[start].end <= c.start:
            start += 1
        words = []
        for e in english[start:]:
            if e.start >= c.end:
                break
            overlap = min(c.end, e.end) - max(c.start, e.start)
            if overlap >= min(c.end - c.start, e.end - e.start) * .5:
                words.append(' '.join(e.text.split()))
        count += bool(words)
        result.append(replace(c, text=c.text + ('\n' + ' '.join(words) if words else '')))
    return result if count == len(zh) else None


async def supplement_english(job, source, info, english, root, secrets, queue, video, emit):
    """Existing Chinese avoids translation, but still needs real English for the final pair."""
    if english:
        track = english[0]
        if track.get('path'):
            remember_source(queue, track['path'], video)
            return track['cues']
        cues, _ = await video_jobs.extract_subtitle(str(source), track['stream'], root)
        return cues
    downloaded = await website_subtitle(job, root, secrets.get('assrt_token', ''), emit, english_only=True)
    if downloaded:
        cues = await read_text_subtitle(downloaded, root)
        if chinese(cues):
            cues = [replace(c, text='\n'.join(line for line in c.text.splitlines()
                       if not re.search(r'[\u3400-\u9fff]', line))) for c in cues]
            cues = [c for c in cues if re.search('[A-Za-z]', c.text)]
        if cues and looks_english(cues):
            return cues
    audio = next((a for a in info['audio'] if a.get('language') in {'en', 'eng'}), None)
    if audio is None:
        audio = next((a for a in info['audio'] if a.get('language', 'und') == 'und'), None)
    if audio is None:
        raise ManualChoice('中文字幕已保留在本机，但没有可用英文字幕 / 英文音轨。请补充匹配英文或双语字幕；尚未生成最终双语 SRT。')
    await emit({'stage': '补充英文', 'message': '已有中文，无英文文本；仅识别英文原文用于中英配对，不重复翻译中文。'})
    cues = await video_jobs.asr_file(str(source), info['duration'], audio['index'], root, emit,
                                     job['config'].get('asr_backend', 'vulkan'))
    if chinese(cues) or not looks_english(cues):
        raise ManualChoice('识别结果无法确认是英文原文，请人工选择英文 / 双语字幕。')
    return cues


async def generate(queue, job, mappings, secrets, emit):
    media = job['data']
    source = resolve_media(media['path'], mappings, job['service'].split(':', 1)[0])
    await emit({'stage': '读取校验', 'message': '完整读取视频并计算内容指纹；原视频只读。', 'progress': 0})
    identity = fingerprint(source)
    if media.get('size') and identity['size'] != media['size']:
        raise Waiting('本机文件大小与入库记录不同，等待挂载刷新或导入完成。')
    video = digest([job['service'], media['file_id'], media['version'], identity])
    root = queue.path.parent / 'videos' / video
    root.mkdir(parents=True, exist_ok=True)
    try:
        info = await video_jobs.inspect(str(source), cache_root=root, sidecar_contents=False)
    except (RuntimeError, OSError, ValueError):
        raise Waiting('视频 / 字幕读取或探测失败；不会当作无字幕转入 ASR。') from None
    override = job['override'] or {}
    english, chinese_tracks = [], []
    # Every matching sidecar is read and parsed. A failure is never treated as absence.
    for track in info['subtitles']:
        if override.get('subtitle_path'):
            continue
        if track.get('path'):
            if managed_stale(queue, track['path'], video):
                continue
            try:
                cues = await read_text_subtitle(track['path'], root)
            except OSError:
                raise Waiting('外挂字幕暂不可读，等待挂载恢复；不会转入 ASR。') from None
            except (ValueError, RuntimeError):
                raise ManualChoice('已有外挂字幕损坏或转换失败；请在视频页核对后指定可用字幕。') from None
            track = {**track, 'cues': cues}
            if chinese(cues):
                chinese_tracks.append(track)
            elif track.get('language') in {'en', 'eng'} or looks_english(cues):
                english.append(track)
        elif track.get('language') in {'zh', 'zho', 'chi'}:
            chinese_tracks.append(track)
        elif track.get('language') in {'en', 'eng'}:
            english.append(track)
    # Keep the specified Chinese -> downloaded -> English sidecar -> embedded -> ASR order.
    english.sort(key=lambda t: not bool(t.get('path')))
    chinese_tracks.sort(key=lambda t: not bool(t.get('path')))
    chosen, origin = None, ''
    if override.get('subtitle_path'):
        chosen, origin = {'path': override['subtitle_path']}, '人工选择字幕'
    elif chinese_tracks:
        chosen = chinese_tracks[0]
        origin = '复用已有中文（外挂）' if chosen.get('path') else '复用已有中文（内嵌）'
    else:
        downloaded = await website_subtitle(job, root, secrets.get('assrt_token', ''), emit)
        if downloaded:
            chosen, origin = {'path': downloaded}, '下载字幕 · ' + job['config']['search_provider'].upper()
        elif english:
            chosen = english[0]
            origin = '英文外挂字幕' if chosen.get('path') else '内嵌英文文本字幕'
        else:
            origin = '本地 ASR（已确认无可用文本字幕）'
    config = {**processing_config(job['config']), **secrets, 'source': str(source), 'make_chinese': True,
              'subtitle_content': 'auto'}
    if chosen:
        if chosen.get('path'):
            try:
                selected_cues = chosen.get('cues') or await read_text_subtitle(chosen['path'], root)
            except (ValueError, RuntimeError):
                raise ManualChoice('所选文本字幕解析失败，请检查文件和编号；不会转入 ASR。') from None
            # Translation reads a stable local snapshot, not a changing NAS sidecar.
            text = compose(selected_cues)
            staged = root / ('selected-' + digest(text)[:24] + '.srt')
            atomic_write(staged, text)
            config['subtitle_path'] = str(staged)
        else:
            config['subtitle_stream'] = chosen['stream']
    else:
        config['force_asr'] = True
    await emit({'origin': origin, 'stage': '准备字幕', 'message': origin})
    result = await video_jobs.prepare(config, emit, cache_root=root, inspection=info)
    output_root = Path(result['directory']) / 'nas'
    output_root.mkdir(exist_ok=True)
    original = valid_srt(result['timed_source'])
    chinese_cues = valid_srt(result['subtitle'])
    bilingual = valid_srt(result['bilingual']) if result.get('bilingual') else None
    if chinese(original):
        chinese_cues, bilingual = split_chinese(chinese_cues)
        if bilingual is None:
            en = await supplement_english(job, source, info, english, root, secrets, queue, video, emit)
            bilingual = pair_existing(chinese_cues, en)
    if bilingual is None or any(len(c.text.splitlines()) < 2 for c in bilingual):
        raise ManualChoice('中文与英文无法逐条可靠配对；字幕保留在本机。请选匹配双语字幕或校准时间轴后重试，不写出单语成品。')
    artifacts = {}
    for suffix, cues in (('zh', chinese_cues), ('bilingual', bilingual)):
        if suffix not in config['outputs'] or cues is None:
            continue
        path = output_root / (suffix + '.srt')
        atomic_write(path, compose(cues))
        valid_srt(path)
        artifacts[suffix] = {'path': str(path), 'hash': file_hash(path), 'cues': len(cues)}
    if not artifacts:
        raise ManualChoice('已有中文但没有可配对的英文原文；请选择中文输出或补充匹配英文字幕。')
    # This also detects a replacement during a long model/ASR job.
    await emit({'stage': '生成校验', 'message': '字幕已生成，复核视频版本后准备写回。', 'progress': 1})
    if fingerprint(source) != identity:
        raise Waiting('处理期间视频已变化；字幕保留在本机，等待新版本重新处理。')
    if chosen and chosen.get('path'):
        remember_source(queue, chosen['path'], video)
    if 'zh' in artifacts:
        result['subtitle'] = artifacts['zh']['path']
    if 'bilingual' in artifacts:
        result['bilingual'] = artifacts['bilingual']['path']
    result.update(outputs=artifacts, fingerprint=identity, video=video, source=str(source),
                  origin_label=origin, reused_path=chosen.get('path') if chosen else None,
                  reused_chinese=chinese(original),
                  missing_outputs=[s for s in config['outputs'] if s not in artifacts],
                  library_verified=False)
    atomic_write(output_root / 'result.json', json.dumps(result, ensure_ascii=False, indent=2))
    return result
