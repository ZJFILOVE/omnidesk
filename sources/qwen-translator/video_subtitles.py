"""Timed soft subtitles, deterministic transforms and resumable translation."""
from dataclasses import dataclass, replace
import hashlib
import html
import json
import math
from pathlib import Path
import re

from text_translation import SubtitleTranslator
from translation_pool import TranslationPool, concurrency, gather_cancel_on_error


@dataclass(frozen=True)
class Cue:
    start: int
    end: int
    text: str


def decode(data):
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    for encoding in ('utf-8-sig', 'gb18030', 'big5'):
        try:
            return data.decode(encoding)
        except UnicodeError:
            pass
    raise ValueError('字幕编码无法识别，请另存为 UTF-8 SRT。')


def stamp(value):
    parts = re.split('[:,.]', value)
    h, m, s, ms = map(int, parts)
    if m >= 60 or s >= 60:
        raise ValueError('字幕时间戳无效。')
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def parse_srt(text):
    text = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    result = []
    for block in re.split(r'\n\s*\n', text.strip()):
        lines = block.strip().splitlines()
        if lines and lines[0].strip().isdigit():
            lines.pop(0)
        if not lines:
            continue
        match = re.fullmatch(r'\s*(\d{1,3}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*'
                             r'(\d{1,3}:\d{2}:\d{2}[,.]\d{3})(?:\s+.*)?', lines[0])
        if not match:
            raise ValueError('不是有效的 SRT 字幕，或存在损坏的字幕段。')
        start, end = map(stamp, match.groups())
        body = html.unescape(re.sub(r'<[^>]*>', '', '\n'.join(lines[1:]))).strip()
        body = re.sub(r'\{\\[^}]*\}', '', body).strip()
        if end <= start or not body:
            continue
        result.append(Cue(start, end, body))
    if not result:
        raise ValueError('字幕没有有效文本。')
    return sorted(result, key=lambda cue: cue.start)


def timestamp(ms):
    s, ms = divmod(max(0, round(ms)), 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f'{h:02}:{m:02}:{s:02},{ms:03}'


def compose(cues):
    return ''.join(f'{i}\n{timestamp(c.start)} --> {timestamp(c.end)}\n{c.text}\n\n'
                   for i, c in enumerate(cues, 1))


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.partial')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(path)


def chinese(cues):
    # Content heuristic, exposed as an overridable choice in the UI.
    body = ''.join(c.text for c in cues)
    han = len(re.findall(r'[\u3400-\u9fff]', body))
    return han >= max(2, len(re.findall(r'\w', body)) * .08)


def transform(cues, scale=1., offset_ms=0):
    if not math.isfinite(scale) or not .5 <= scale <= 2 or not math.isfinite(offset_ms):
        raise ValueError('时间轴倍率或偏移无效。')
    result = []
    for c in cues:
        start, end = round(c.start * scale + offset_ms), round(c.end * scale + offset_ms)
        if end > 0:
            result.append(replace(c, start=max(0, start), end=max(1, end)))
    if not result:
        raise ValueError('调整后所有字幕都在视频开始之前。')
    return result


def two_anchors(sub1, video1, sub2, video2):
    if sub2 <= sub1 or video2 <= video1:
        raise ValueError('第二个校准点必须晚于第一个。')
    scale = (video2 - video1) / (sub2 - sub1)
    if not .5 <= scale <= 2:
        raise ValueError('两个校准点差异过大，请检查输入。')
    return scale, video1 - sub1 * scale


def batch_result(text, ids):
    clean = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip())
    result = json.loads(clean)
    if not isinstance(result, list) or len(result) != len(ids):
        raise ValueError('批量翻译条数不匹配。')
    mapping = {}
    for item in result:
        if (not isinstance(item, dict) or type(item.get('id')) is not int
                or item['id'] not in ids or item['id'] in mapping
                or not isinstance(item.get('text'), str) or not item['text'].strip()):
            raise ValueError('批量翻译编号或文本无效。')
        # Blank lines delimit SRT cues, and must not leak out of the model response.
        mapping[item['id']] = '\n'.join(x.strip() for x in item['text'].splitlines() if x.strip())
    return mapping


def translation_batches(cues, missing):
    """Group by ten-minute media windows, with a text-size guard for dense captions."""
    batch, size, window = [], 0, None
    for i in missing:
        cue_window = cues[i].start // 600_000
        length = len(cues[i].text)
        if batch and (cue_window != window or size + length > 24_000):
            yield batch
            batch, size = [], 0
        batch.append(i)
        size += length
        window = cue_window
    if batch:
        yield batch


async def translate_cues(cues, config, directory, emit, factory=SubtitleTranslator):
    if config.get('text_provider') == 'none':
        raise ValueError('没有中文字幕；请在偏好设置选择翻译提供方，或关闭生成中文。')
    identity = {k: config.get(k) for k in ('text_provider', 'text_endpoint', 'text_model',
                'api_format', 'codex_model', 'codex_reasoning_effort', 'antigravity_model')}
    key = hashlib.sha256(json.dumps([identity, 'video-batch-v1', [c.text for c in cues]],
                                   ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path = Path(directory) / f'translation-{key}.json'
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    translated = {i: '\n'.join(line.strip() for line in saved[str(i)].splitlines() if line.strip())
                  for i in range(len(cues))
                  if isinstance(saved.get(str(i)), str) and saved[str(i)].strip()}
    translator = TranslationPool(lambda: factory({**config, 'subtitle_language': 'en-zh', 'video_batch': True}),
                                 concurrency(config))
    async def translate_batch(ids, retry_single=True):
        payload = json.dumps([{'id': i, 'text': cues[i].text} for i in ids], ensure_ascii=False)
        try:
            result = await translator.translate(payload)
            values = batch_result(result['text'], ids)
        except (ValueError, KeyError):
            if len(ids) == 1:
                if not retry_single:
                    raise
                await translate_batch(ids, retry_single=False)
            else:
                await emit({'stage': '翻译', 'message': '返回的字幕编号或条数不完整，缩小分段重试…'})
                middle = len(ids) // 2
                await translate_batch(ids[:middle])
                await translate_batch(ids[middle:])
            return
        translated.update(values)
        atomic_write(path, json.dumps(translated, ensure_ascii=False))
        await emit({'stage': '翻译', 'message': f'中文翻译 {len(translated)} / {len(cues)}',
                    'progress': len(translated) / len(cues)})

    try:
        missing = [i for i in range(len(cues)) if i not in translated]
        batches = list(translation_batches(cues, missing))
        # Shorter videos can also benefit, while small batches retain useful context.
        while batches and len(batches) < concurrency(config):
            index = max(range(len(batches)), key=lambda i: len(batches[i]))
            batch = batches[index]
            if len(batch) < 32:
                break
            middle = len(batch) // 2
            batches[index:index + 1] = [batch[:middle], batch[middle:]]
        async def run_batch(number, ids):
            await emit({'stage': '翻译', 'message':
                        f'翻译分段 {number} / {len(batches)} · {len(ids)} 条 · '
                        f'{timestamp(cues[ids[0]].start)} 至 {timestamp(cues[ids[-1]].end)}',
                        'progress': len(translated) / len(cues)})
            await translate_batch(ids)
        await gather_cancel_on_error(run_batch(number, ids) for number, ids in enumerate(batches, 1))
    finally:
        await translator.close()
    return [replace(c, text=translated[i]) for i, c in enumerate(cues)]
