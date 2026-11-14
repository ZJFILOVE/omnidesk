"""Durable, subtitle-only mobile queue. One event loop owns one locked database."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
from uuid import UUID, uuid4

from video_subtitles import compose, parse_srt, translate_cues

MODEL = 'gpt-6-luna'
MAX_SUBTITLE_BYTES = 2 * 1024 * 1024
MAX_CUES = 20_000
CONFIG = {'text_provider': 'codex', 'codex_model': MODEL,
          'codex_reasoning_effort': 'low', 'translation_concurrency': 4}
PUBLIC_FIELDS = ('id', 'state', 'progress', 'message', 'model', 'created_at', 'updated_at')


class JobError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def validate_srt(text):
    """Reject corrupt cues instead of parse_srt's intentional best-effort skipping."""
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_SUBTITLE_BYTES:
        raise ValueError('SRT must be UTF-8 text of at most 2 MiB.')
    normalized = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not normalized or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', normalized):
        raise ValueError('SRT is empty or contains control characters.')
    blocks = re.split(r'\n[ \t]*\n', normalized)
    if len(blocks) > MAX_CUES:
        raise ValueError('SRT contains more than 20000 cues.')
    previous = -1
    for number, block in enumerate(blocks, 1):
        lines = block.splitlines()
        if len(lines) < 3 or lines[0].strip() != str(number):
            raise ValueError('SRT cues must have sequential numbers starting at 1.')
        if not re.fullmatch(r'\d{1,3}:\d{2}:\d{2},\d{3}\s+-->\s+\d{1,3}:\d{2}:\d{2},\d{3}', lines[1].strip()):
            raise ValueError('Invalid SRT timestamp line.')
        if len('\n'.join(lines[2:])) > 8000:
            raise ValueError('An SRT cue exceeds 8000 characters.')
        cue = parse_srt(block)[0]  # Reject empty text and non-positive durations.
        if cue.start < previous:
            raise ValueError('SRT cues must be ordered by start time.')
        previous = cue.start
    cues = parse_srt(normalized)
    if len(cues) != len(blocks):
        raise ValueError('SRT contains an invalid cue.')
    return cues


class MobileJobs:
    def __init__(self, directory, *, translator=translate_cues, max_pending=100):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (self.directory / '.service.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError('Mobile subtitle data directory is already in use; run one service worker.') from None
        try:
            self.db = sqlite3.connect(self.directory / 'jobs.sqlite3')
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA secure_delete=ON')
            self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, payload_hash TEXT NOT NULL,
                title TEXT NOT NULL, source_url TEXT NOT NULL, original TEXT NOT NULL,
                state TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0, message TEXT NOT NULL,
                model TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                zh TEXT, bilingual TEXT)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS deleted_requests (
                request_id TEXT PRIMARY KEY, job_id TEXT, deleted_at TEXT NOT NULL,
                cleanup_pending INTEGER NOT NULL DEFAULT 0)''')
            self.db.commit()
            # A crash may happen after fencing the request but before erasing its data.
            # Finish these operations before allowing any translation to restart.
            for row in self.db.execute('SELECT request_id,job_id FROM deleted_requests WHERE cleanup_pending=1').fetchall():
                self._purge_request(row['request_id'], row['job_id'])
            with self.db:
                self.db.execute("UPDATE jobs SET state='queued', message=?, updated_at=? WHERE state='running'",
                                ('服务已重启，将从已完成的字幕分段继续。', now()))
        except BaseException:
            if hasattr(self, 'db'):
                self.db.close()
            self.lock.close()
            raise
        self.translator = translator
        self.max_pending = max_pending
        self.wake = asyncio.Event()
        self.worker = None
        self.active = None
        self.active_id = None
        self.stopping = False
        self.deletions = {}

    def record(self, jid):
        row = self.db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        if row is None:
            raise JobError(404, '任务不存在。')
        if row['state'] == 'deleting':
            raise JobError(410, '任务已删除，不能继续或重试。')
        return dict(row)

    def get(self, jid):
        row = self.record(jid)
        return {name: row[name] for name in PUBLIC_FIELDS}

    def check_capacity(self):
        count = self.db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
        if count >= self.max_pending:
            raise JobError(429, '字幕任务队列已满，请稍后再试。')

    def submit(self, request_id, title, source_url, subtitle_srt):
        if self.db.execute('SELECT 1 FROM deleted_requests WHERE request_id=?', (request_id,)).fetchone():
            raise JobError(410, '任务已删除；如需重新翻译，请创建新任务。')
        cues = validate_srt(subtitle_srt)
        # Compare the actual request, including metadata, before normalization.
        digest = hashlib.sha256(json.dumps([title, source_url, subtitle_srt],
                               ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        old = self.db.execute('SELECT id, payload_hash FROM jobs WHERE request_id=?', (request_id,)).fetchone()
        if old:
            if old['payload_hash'] != digest:
                raise JobError(409, 'client_request_id 已用于不同内容。')
            return self.get(old['id'])
        self.check_capacity()
        jid, timestamp = str(uuid4()), now()
        with self.db:
            self.db.execute('''INSERT INTO jobs
                (id,request_id,payload_hash,title,source_url,original,state,progress,message,model,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'queued',0,?,?,?,?)''',
                (jid, request_id, digest, title, source_url, compose(cues), '等待翻译。', MODEL, timestamp, timestamp))
        self.wake.set()
        return self.get(jid)

    def update(self, jid, *, state=None, progress=None, message=None, only_state=None):
        values = {k: v for k, v in {'state': state, 'progress': progress, 'message': message}.items() if v is not None}
        values['updated_at'] = now()
        query = 'UPDATE jobs SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?'
        args = [*values.values(), jid]
        if only_state:
            query += ' AND state=?'
            args.append(only_state)
        with self.db:
            self.db.execute(query, args)

    async def cancel(self, jid):
        if self.get(jid)['state'] in {'queued', 'running'}:
            self.update(jid, state='cancelled', message='已取消；已完成的翻译分段保留，可重试。')
            task = self.active if self.active_id == jid else None
            if task and not task.done():
                if not task.cancelling():
                    task.cancel()
                # A disconnected HTTP client must not interrupt translator/process cleanup.
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if not task.cancelled():
                        raise
        return self.get(jid)

    def _remove_checkpoints(self, jid):
        # Only server-generated UUIDs can select a directory. Directory-relative
        # operations do not follow a replaced root or a checkpoint symlink.
        if str(UUID(jid)) != jid:
            raise ValueError('Invalid stored job UUID')
        try:
            root = os.open(self.directory / 'checkpoints', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        try:
            try:
                entry = os.stat(jid, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                return
            if stat.S_ISDIR(entry.st_mode):
                shutil.rmtree(jid, dir_fd=root)
            else:
                os.unlink(jid, dir_fd=root)
            os.fsync(root)
        finally:
            os.close(root)

    def _checkpoint_database(self):
        if self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
            raise sqlite3.OperationalError('Database checkpoint is busy')

    def _purge_request(self, request_id, jid):
        if jid:
            self._remove_checkpoints(jid)
        with self.db:
            self.db.execute('DELETE FROM jobs WHERE request_id=?', (request_id,))
        # Reclaim the application's database pages and its WAL after removing all
        # subtitle variants and metadata. This is ordinary deletion, not a promise
        # of forensic erasure from filesystem snapshots, backups or storage media.
        self.db.execute('VACUUM')
        self._checkpoint_database()
        with self.db:
            self.db.execute('''UPDATE deleted_requests SET job_id=NULL, cleanup_pending=0
                               WHERE request_id=?''', (request_id,))
        self._checkpoint_database()

    async def delete_request(self, request_id):
        try:
            request_id = str(UUID(request_id))
        except ValueError:
            raise JobError(422, 'client_request_id 必须是 UUID。') from None
        # The tombstone and state change happen without yielding. Submissions,
        # retry/cancel and the worker cannot revive this request after this point.
        with self.db:
            row = self.db.execute('SELECT id FROM jobs WHERE request_id=?', (request_id,)).fetchone()
            self.db.execute('''INSERT OR IGNORE INTO deleted_requests
                (request_id,job_id,deleted_at,cleanup_pending) VALUES (?,?,?,?)''',
                (request_id, row['id'] if row else None, now(), int(row is not None)))
            self.db.execute("UPDATE jobs SET state='deleting' WHERE request_id=?", (request_id,))
        pending = self.db.execute('SELECT job_id,cleanup_pending FROM deleted_requests WHERE request_id=?',
                                  (request_id,)).fetchone()
        if not pending['cleanup_pending']:
            return {'status': 'deleted'}
        task = self.deletions.get(request_id)
        if task is None or task.done():
            task = asyncio.create_task(self._finish_delete(request_id, pending['job_id']), name='delete-mobile-request')
            self.deletions[request_id] = task
            def finished(done):
                if self.deletions.get(request_id) is done:
                    self.deletions.pop(request_id, None)
                if not done.cancelled():
                    done.exception()  # Observe errors even after the HTTP client disconnects.
            task.add_done_callback(finished)
        await asyncio.shield(task)
        return {'status': 'deleted'}

    async def _finish_delete(self, request_id, jid):
        active = self.active if self.active_id == jid else None
        if active and not active.done():
            if not active.cancelling():
                active.cancel()
            # Translator cleanup can still write a last checkpoint. Join it before
            # removing files; cancelling a disconnected request cannot interrupt us.
            await asyncio.gather(active, return_exceptions=True)
        try:
            self._purge_request(request_id, jid)
        except (OSError, sqlite3.Error, ValueError):
            raise JobError(503, '字幕清理尚未完成，请稍后重试；任务不会重新开始翻译。') from None

    def retry(self, jid):
        state = self.get(jid)['state']
        if state == 'ready':
            raise JobError(409, '字幕已准备好，无需重试。')
        if state in {'failed', 'cancelled'}:
            self.check_capacity()
            self.update(jid, state='queued', message='将从已完成的字幕分段继续。')
            self.wake.set()
        return self.get(jid)

    def subtitle(self, jid, kind):
        if kind not in {'original', 'zh', 'bilingual'}:
            raise JobError(404, '字幕类型不存在。')
        row = self.record(jid)
        if kind != 'original' and row['state'] != 'ready':
            raise JobError(409, '字幕尚未准备好。')
        return row[kind]

    def start(self):
        if self.worker is None:
            self.stopping = False
            self.worker = asyncio.create_task(self.run(), name='mobile-subtitles')
            self.wake.set()

    async def run(self):
        while not self.stopping:
            self.wake.clear()
            row = self.db.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if not row:
                await self.wake.wait()
                continue
            self.active_id = row['id']
            self.update(self.active_id, state='running', message='正在翻译字幕。')
            self.active = asyncio.create_task(self.translate(self.active_id))
            try:
                await self.active
            except asyncio.CancelledError:
                # Cancellation before the coroutine's first execution is also valid.
                if self.stopping:
                    self.update(self.active_id, state='queued', message='服务停止，重启后继续。', only_state='running')
            finally:
                self.active = None
                self.active_id = None

    async def translate(self, jid):
        async def emit(event):
            progress = event.get('progress')
            if not isinstance(progress, (int, float)) or not math.isfinite(progress):
                progress = None
            else:
                progress = max(0., min(.99, progress))
            self.update(jid, progress=progress, message=str(event.get('message') or '正在翻译字幕。')[:500],
                        only_state='running')

        try:
            cues = parse_srt(self.record(jid)['original'])
            cache = self.directory / 'checkpoints' / jid
            cache.mkdir(parents=True, exist_ok=True, mode=0o700)
            translated = await self.translator(cues, dict(CONFIG), cache, emit)
            if len(translated) != len(cues) or any(
                    (a.start, a.end) != (b.start, b.end) or not b.text.strip() for a, b in zip(cues, translated)):
                raise ValueError('Invalid translated cue mapping')
            chinese = compose(translated)
            bilingual = compose([replace(a, text=b.text + '\n' + a.text) for a, b in zip(cues, translated)])
            # Model replies cannot create extra SRT blocks or corrupt timing.
            for body in (chinese, bilingual):
                parsed = parse_srt(body)
                if len(parsed) != len(cues):
                    raise ValueError('Invalid translated SRT')
            with self.db:
                self.db.execute("""UPDATE jobs SET state='ready', progress=1, message=?,
                    updated_at=?, zh=?, bilingual=? WHERE id=? AND state='running'""",
                    ('字幕已准备好。', now(), chinese, bilingual, jid))
        except asyncio.CancelledError:
            if self.stopping:
                self.update(jid, state='queued', message='服务停止，重启后继续。', only_state='running')
            else:
                self.update(jid, state='cancelled', message='已取消，可从已完成的分段重试。', only_state='running')
        except Exception:
            # Provider exceptions may contain credentials or transcript; never return raw text.
            self.update(jid, state='failed', message='翻译失败，请检查飞牛上的 Codex 登录、模型权限、额度和网络后重试。',
                        only_state='running')

    async def close(self):
        self.stopping = True
        self.wake.set()
        if self.active and not self.active.done() and not self.active.cancelling():
            self.active.cancel()
        if self.worker:
            await self.worker
        if self.deletions:
            await asyncio.gather(*self.deletions.values(), return_exceptions=True)
        self.db.close()
        self.lock.close()
