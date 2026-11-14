"""Durable media subtitle jobs using the mobile queue's cancellation/deletion fence."""
import asyncio
from dataclasses import replace
import json
import math
import re
from uuid import uuid4

from mobile_jobs import CONFIG, MODEL, JobError, MobileJobs, now, validate_srt
from nas_api import digest
from video_subtitles import chinese, compose, parse_srt, translate_cues


class MediaJobs(MobileJobs):
    def __init__(self, directory, library, *, translator=translate_cues, max_pending=100):
        super().__init__(directory, translator=translator, max_pending=max_pending)
        self.library = library
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}
        with self.db:
            for name in ('media_id', 'media_options', 'media_identity', 'subtitle_source'):
                if name not in columns:
                    self.db.execute(f'ALTER TABLE jobs ADD COLUMN {name} TEXT')

    def get(self, jid):
        result = super().get(jid)
        row = self.record(jid)
        result.update(media_id=row.get('media_id'), subtitle_source=row.get('subtitle_source'),
                      client_request_id=row['request_id'])
        if row['state'] == 'ready':
            result['subtitles'] = {kind: f'/v1/media/jobs/{jid}/subtitles/{kind}'
                                   for kind in ('original', 'zh', 'bilingual')}
        return result

    async def submit_media(self, request_id, media_id, subtitle_id=None, force_asr=False, audio_id=None):
        if force_asr or audio_id is not None:
            raise JobError(422, 'NAS 字幕不执行语音识别；无文本字幕时直接跳过字幕处理，视频仍可观看。')
        if self.db.execute('SELECT 1 FROM deleted_requests WHERE request_id=?', (request_id,)).fetchone():
            raise JobError(410, '任务已删除；如需重新生成字幕，请创建新任务。')
        options = {'subtitle_id': subtitle_id, 'force_asr': force_asr, 'audio_id': audio_id}
        payload_hash = digest([media_id, options])
        # Check idempotency before NAS I/O: a lost response remains recoverable
        # even while the library or source file is temporarily unavailable.
        old = self.db.execute('SELECT id,payload_hash FROM jobs WHERE request_id=?', (request_id,)).fetchone()
        if old:
            if old['payload_hash'] != payload_hash:
                raise JobError(409, 'client_request_id 已用于不同内容。')
            return self.get(old['id'])
        self.check_capacity()
        record = await self.library.file_record(media_id)
        info = await self.library.inspection(record)
        if not info['subtitles'] and not (subtitle_id and subtitle_id.startswith('generated-')):
            raise JobError(422, '此视频无文本字幕，已跳过字幕处理；可以直接观看或下载视频。')
        if subtitle_id and not subtitle_id.startswith('generated-') and not any(
                self.library.track_id(t) == subtitle_id for t in info['subtitles']):
            raise JobError(422, '所选字幕已不存在，请刷新字幕选项；不会执行语音识别。')
        # NAS I/O yielded; another request may have cancelled/deleted/submitted.
        if self.db.execute('SELECT 1 FROM deleted_requests WHERE request_id=?', (request_id,)).fetchone():
            raise JobError(410, '任务已删除，迟到的请求已停止。')
        old = self.db.execute('SELECT id,payload_hash FROM jobs WHERE request_id=?', (request_id,)).fetchone()
        if old:
            if old['payload_hash'] != payload_hash:
                raise JobError(409, 'client_request_id 已用于不同内容。')
            return self.get(old['id'])
        self.check_capacity()
        jid, timestamp = str(uuid4()), now()
        with self.db:
            self.db.execute('''INSERT INTO jobs
                (id,request_id,payload_hash,title,source_url,original,state,progress,message,model,created_at,updated_at,
                 media_id,media_options,media_identity)
                VALUES (?,?,?,?,?,'','queued',0,?,?,?,?,?,?,?)''',
                (jid, request_id, payload_hash, record['title'], '', '等待准备媒体字幕。', MODEL,
                 timestamp, timestamp, media_id, json.dumps(options), record['identity']))
        self.wake.set()
        return self.get(jid)

    def available_tracks(self, media_id, identity):
        row = self.db.execute("SELECT id FROM jobs WHERE media_id=? AND media_identity=? AND state='ready' ORDER BY updated_at DESC LIMIT 1",
                              (media_id, identity)).fetchone()
        if not row:
            return []
        labels = {'original': ('原文字幕 · 飞牛生成', 'und'), 'zh': ('中文字幕 · 飞牛生成', 'zh'),
                  'bilingual': ('双语字幕 · 飞牛生成', 'zh-en')}
        return [{'id': f"generated-{row['id']}-{kind}", 'title': title, 'language': language,
                 'source': 'generated', 'download_path': f"/v1/media/jobs/{row['id']}/subtitles/{kind}"}
                for kind, (title, language) in labels.items()]

    def latest(self, media_id, identity):
        row = self.db.execute("SELECT id FROM jobs WHERE media_id=? AND media_identity=? AND state!='deleting' ORDER BY created_at DESC,id DESC LIMIT 1",
                              (media_id, identity)).fetchone()
        return self.get(row['id']) if row else None

    def subtitle(self, jid, kind):
        value = super().subtitle(jid, kind)
        if not value:
            raise JobError(409, '原文字幕尚在提取或识别中，请等待字幕任务完成。')
        return value

    async def translate(self, jid):
        async def emit(event):
            progress = event.get('progress')
            progress = max(0., min(.99, float(progress))) if isinstance(progress, (int, float)) and math.isfinite(progress) else None
            # Stage text is controlled by the existing processors. Never forward
            # exceptions, ffmpeg output, source paths or model replies to mobile.
            self.update(jid, progress=progress, message=str(event.get('message') or '正在准备字幕。')[:500], only_state='running')

        try:
            row = self.record(jid)
            options = json.loads(row['media_options'])
            if options.get('force_asr') or options.get('audio_id') is not None:
                raise JobError(422, 'NAS 字幕不执行语音识别，请选择已有文本字幕。')
            record = await self.library.file_record(row['media_id'])
            if record['identity'] != row['media_identity']:
                raise JobError(409, '媒体版本已改变，请刷新影片并创建新的字幕任务。')
            root = self.directory / 'checkpoints' / jid
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if row['original']:
                cues = validate_srt(row['original'])
                origin = row.get('subtitle_source') or '已保留原文'
            else:
                await emit({'message': '正在检查外挂和内嵌字幕。'})
                info = await self.library.inspection(record)
                selected = options['subtitle_id']
                tracks = info['subtitles']
                if selected and selected.startswith('generated-'):
                    # Generated tracks are selected by a server job UUID, never by
                    # a caller-provided filesystem path.
                    prefix, sep, kind = selected.rpartition('-')
                    source_job = prefix.removeprefix('generated-')
                    old = self.record(source_job)
                    if old['media_id'] != row['media_id'] or old['media_identity'] != record['identity']:
                        raise JobError(409, '所选生成字幕属于其他媒体版本。')
                    cues = validate_srt(self.subtitle(source_job, kind))
                    origin = '已有生成字幕'
                elif selected or tracks:
                    if not selected:
                        # Prefer English source, then Chinese, then a sole known
                        # text track. Multiple unidentified tracks need selection.
                        english = [t for t in tracks if t.get('language') in {'en', 'eng'}]
                        known_zh = [t for t in tracks if t.get('language') in {'zh', 'zho', 'chi', 'chs', 'cht'}]
                        candidates = english or known_zh or tracks
                        if len(candidates) != 1:
                            raise JobError(422, '发现多个字幕，请选择对应语言的字幕后准备。')
                        selected = self.library.track_id(candidates[0])
                    cues = await self.library.subtitle_cues(record, selected, info=info, cache_root=root)
                    origin = '外挂字幕' if selected.startswith('sidecar-') else '内嵌字幕'
                else:
                    raise JobError(422, '此视频无文本字幕，已跳过字幕处理；可以直接观看或下载视频。')
                original = compose(cues)
                validate_srt(original)
                with self.db:
                    self.db.execute("UPDATE jobs SET original=?,subtitle_source=?,updated_at=? WHERE id=? AND state='running'",
                                    (original, origin, now(), jid))
            await emit({'message': '原文已准备好，正在生成中文与双语字幕。'})
            if chinese(cues):
                # Existing Chinese subtitles need no second translation. If the
                # source already contains English lines preserve that pair.
                if not all(any(re.search(r'[A-Za-z]{2,}', line) and
                               not any('\u3400' <= ch <= '\u9fff' for ch in line)
                               for line in c.text.splitlines()) for c in cues):
                    raise JobError(422, '所选字幕只有中文，已保留原文供直接观看；如需双语字幕，请选择英文或已有双语字幕。')
                translated = [replace(c, text='\n'.join(line for line in c.text.splitlines()
                              if any('\u3400' <= ch <= '\u9fff' for ch in line)) or c.text) for c in cues]
                bilingual = compose(cues)
            else:
                translated = await self.translator(cues, dict(CONFIG), root, emit)
                if len(translated) != len(cues) or any((a.start, a.end) != (b.start, b.end) or not b.text.strip()
                                                      for a, b in zip(cues, translated)):
                    raise ValueError('Invalid cue mapping')
                bilingual = compose([replace(a, text=b.text + '\n' + a.text) for a, b in zip(cues, translated)])
            zh = compose(translated)
            validate_srt(zh)
            validate_srt(bilingual)
            current = await self.library.file_record(row['media_id'])
            if current['identity'] != record['identity']:
                raise JobError(409, '媒体版本已改变，字幕结果未发布；请刷新并创建新的任务。')
            with self.db:
                self.db.execute("UPDATE jobs SET state='ready',progress=1,message=?,updated_at=?,zh=?,bilingual=? WHERE id=? AND state='running'",
                                ('字幕已准备好，可播放或下载到手机。', now(), zh, bilingual, jid))
        except asyncio.CancelledError:
            self.update(jid, state='queued' if self.stopping else 'cancelled',
                        message='服务停止，重启后继续。' if self.stopping else '已取消，原文与翻译检查点保留，可重试。', only_state='running')
        except JobError as error:
            self.update(jid, state='failed', message=str(error), only_state='running')
        except Exception:
            self.update(jid, state='failed', message='字幕准备失败，请检查飞牛上的文件、FFmpeg 及 Codex 登录 / 模型权限后重试。', only_state='running')
