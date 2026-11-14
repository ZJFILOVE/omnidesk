"""Adopt a completed manual preparation into the protected NAS publication queue."""
import json
from pathlib import Path, PurePosixPath
import uuid

from nas_api import digest
from nas_io import VersionChanged, Waiting, file_hash, fingerprint, resolve_media, valid_srt
from nas_queue import processing_config
from video_cache import source_directory


def adopt_prepared(queue, prepared, mappings, processing, *, job_id=None, cache=None):
    if (prepared.get('loaded') or prepared.get('original_only') or not prepared.get('bilingual')
            or prepared.get('source', '').startswith(('https://', 'http://'))):
        raise ValueError('请先准备完整的中文与原文对照字幕，再回传 NAS。')
    source = Path(prepared['source']).resolve(strict=True)
    # Resolve only the configured, tested mappings; never publish beside an arbitrary file.
    matches = []
    for row in queue.db.execute('SELECT id,service,data FROM media WHERE present=1'):
        data = json.loads(row['data'])
        kind = row['service'].split(':', 1)[0]
        remote = PurePosixPath(data['path'])
        if not any(m.get('kind') == kind and m.get('local') and m.get('remote') and
                   remote.is_relative_to(PurePosixPath(m['remote'])) and
                   Path(m['local']).expanduser() / str(remote.relative_to(PurePosixPath(m['remote']))) == source
                   for m in mappings):
            continue
        if resolve_media(data['path'], mappings, kind) == source:
            matches.append(row['id'])
    if len(matches) != 1:
        raise ValueError('未找到唯一对应的 NAS 入库影片，请先在 NAS 自动字幕页同步清单并测试路径映射。')
    mid = matches[0]
    media = queue.db.execute('SELECT service,version,data FROM media WHERE id=?', (mid,)).fetchone()
    version = media['version']
    if job_id:
        job = queue.job(job_id)
        if not job or job['media_id'] != mid or job['version'] != version:
            raise VersionChanged('原 NAS 任务的影片版本已变化，请重新从清单打开影片。')
    else:
        job = None
        # Reconnect a task awaiting manual help rather than leave it stuck forever.
        row = queue.db.execute("SELECT id FROM jobs WHERE media_id=? AND version=? "
                               "AND state='manual' ORDER BY updated DESC LIMIT 1", (mid, version)).fetchone()
        if row:
            job = queue.job(row['id'])
    if job and job['state'] in {'running', 'writing', 'paused', 'cancelled', 'superseded', 'missing'}:
        raise ValueError('对应 NAS 任务正在运行、已暂停或已取消；请先在 NAS 页处理任务状态再回传。')

    cache = Path(cache) if cache is not None else Path(__file__).resolve().parent / 'video-cache'
    expected = source_directory(cache, str(source)).resolve()
    directory = Path(prepared['directory']).resolve(strict=True)
    if not directory.is_relative_to(expected):
        raise VersionChanged('视频已变化或字幕不属于当前视频缓存，请重新准备字幕。')
    saved = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
    if any(saved.get(k) != prepared.get(k) for k in
           ('source', 'subtitle', 'bilingual', 'timed_source', 'source_fingerprint')):
        raise ValueError('字幕缓存记录已变化，请重新打开缓存后回传。')
    identity = fingerprint(source)
    if prepared.get('source_fingerprint') and identity != prepared['source_fingerprint']:
        raise VersionChanged('视频内容已变化，禁止回传旧字幕。')
    if source_directory(cache, str(source)).resolve() != expected:
        raise VersionChanged('读取期间视频已变化，请重新准备字幕。')
    if json.loads(media['data']).get('size') not in (None, 0, identity['size']):
        raise Waiting('视频大小与 NAS 入库记录不同，请先同步清单。')

    artifacts = {}
    timelines = []
    for suffix, key in (('zh', 'subtitle'), ('bilingual', 'bilingual')):
        path = Path(prepared[key]).resolve(strict=True)
        if not path.is_relative_to(directory):
            raise ValueError('字幕文件不属于此视频的准备结果。')
        cues = valid_srt(path)
        if suffix == 'bilingual' and any(len(c.text.splitlines()) < 2 for c in cues):
            raise ValueError('对照字幕存在未配对条目，请重新准备后回传。')
        timelines.append([(c.start, c.end) for c in cues])
        artifacts[suffix] = {'path': str(path), 'hash': file_hash(path), 'cues': len(cues)}
    if timelines[0] != timelines[1]:
        raise ValueError('中文与对照字幕的条数或时间轴不一致，请重新准备。')
    outputs = (job['config'] if job else processing_config(processing))['outputs']
    artifacts = {suffix: artifact for suffix, artifact in artifacts.items() if suffix in outputs}
    video = digest([media['service'], json.loads(media['data'])['file_id'], version, identity])
    root = queue.path.parent / 'videos' / video / ('manual-' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    # Stable local snapshots survive manual cache deletion and later preparations.
    for suffix, artifact in artifacts.items():
        path = root / (suffix + '.srt')
        path.write_bytes(Path(artifact['path']).read_bytes())
        if file_hash(path) != artifact['hash']:
            raise Waiting('复制字幕时缓存发生变化，请重新回传。')
        artifact['path'] = str(path)
    result = {**prepared, 'directory': str(root), 'source': str(source), 'outputs': artifacts,
              'subtitle': artifacts.get('zh', artifacts['bilingual'])['path'],
              'bilingual': artifacts['bilingual']['path'],
              'fingerprint': identity, 'video': video, 'origin_label': '视频页已完成字幕',
              'reused_chinese': False, 'library_verified': False}
    (root / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    # Check task/media state again under a write lock after slow NAS reads.
    queue.db.execute('BEGIN IMMEDIATE')
    try:
        current = queue.db.execute('SELECT version,present FROM media WHERE id=?', (mid,)).fetchone()
        if not current['present'] or current['version'] != version:
            raise VersionChanged('准备回传时入库版本已变化，已保留本机结果。')
        if job is None:
            jid = queue._enqueue(mid, version, processing)
        else:
            jid = job['id']
        latest = queue.job(jid)
        if latest['state'] in {'running', 'writing', 'paused', 'cancelled', 'superseded', 'missing'}:
            raise ValueError('NAS 任务状态已变化，请稍后重试回传。')
        queue.update(jid, state='generated', result=result, next_try=0, error='', progress=1,
                     stage='视频页字幕已接回 · 等待写回', origin=result['origin_label'])
        queue.db.commit()
    except BaseException:
        queue.db.rollback()
        raise
    return {'job_id': jid, 'message': '字幕已接回 NAS 队列，等待受保护写回。'}
