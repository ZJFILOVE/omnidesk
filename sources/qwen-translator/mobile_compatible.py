"""Bounded, durable AAC playback copies. Original library files are read-only."""
import asyncio
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import time

from mobile_jobs import JobError, now
from nas_api import digest
from text_translation import run_process
from video_subtitles import atomic_write

GIB = 1024 ** 3
DEFAULT_FILE_BYTES = 4 * GIB
DEFAULT_CACHE_BYTES = 10 * GIB


class CompatibleMedia:
    def __init__(self, directory, library, *, runner=run_process):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.library = library
        self.runner = runner
        self.max_file_bytes = library.config.get('compatible_max_file_bytes', DEFAULT_FILE_BYTES)
        self.max_cache_bytes = library.config.get('compatible_cache_bytes', DEFAULT_CACHE_BYTES)
        if any(type(n) is not int or not 64 * 1024 * 1024 <= n <= 128 * GIB
               for n in (self.max_file_bytes, self.max_cache_bytes)) or self.max_file_bytes > self.max_cache_bytes:
            raise ValueError('兼容播放缓存需要64MiB–128GiB整数限制，单片上限不能超过总缓存。')
        self.records = {}
        self.readers = {}
        self.worker = self.active = None
        self.stopping = False
        self.wake = asyncio.Event()
        for path in self.directory.glob('*.json'):
            if not re.fullmatch(r'[a-f0-9]{64}\.json', path.name) or path.is_symlink():
                continue
            try:
                value = json.loads(path.read_text())
                key = path.stem
                if self.key(value['identity']) != key or value['state'] not in {'queued', 'running', 'ready', 'failed'}:
                    continue
                self.library.identifier(value['media_id'], {'file'})
                value.setdefault('last_access', 0.)
                value.setdefault('progress', 0.)
                if value['state'] == 'running':
                    value.update(state='queued', progress=0., message='服务已重启，将重新准备兼容音轨。')
                if value['state'] == 'ready' and not self.valid_output(key, value.get('size')):
                    value.update(state='failed', progress=0., message='兼容播放缓存已丢失，请重新准备。', size=None)
                self.records[key] = value
                self.save(key)
            except (OSError, ValueError, TypeError, KeyError, JobError):
                continue
        # Partially written files never become playable and need no resumption.
        for path in self.directory.glob('*.partial'):
            if re.fullmatch(r'[a-f0-9]{64}\.mkv\.partial', path.name):
                path.unlink(missing_ok=True)
        for path in self.directory.glob('*.mkv'):
            if (re.fullmatch(r'[a-f0-9]{64}\.mkv', path.name)
                    and (path.stem not in self.records or self.records[path.stem]['state'] != 'ready')):
                path.unlink(missing_ok=True)

    @staticmethod
    def key(identity):
        return digest([identity, 'mkv-video-copy-all-audio-aac-stereo-160k-v1'])

    def output(self, key):
        return self.directory / (key + '.mkv')

    def valid_output(self, key, size):
        try:
            info = self.output(key).lstat()
            return stat.S_ISREG(info.st_mode) and 0 < info.st_size == size <= self.max_file_bytes
        except (OSError, TypeError):
            return False

    def save(self, key):
        value = self.records[key]
        value['updated_at'] = now()
        atomic_write(self.directory / (key + '.json'), json.dumps(value))

    def public(self, key, media_id):
        value = self.records.get(key)
        if value is None:
            return {'state': 'idle', 'progress': 0., 'message': '需要时可准备 AAC 兼容音轨，保留原画质。',
                    'stream_path': None, 'size': None}
        if value['state'] == 'ready' and not self.valid_output(key, value.get('size')):
            value.update(state='failed', progress=0., message='兼容播放缓存已清理，请重新准备。', size=None)
            self.save(key)
        return {name: value.get(name) for name in ('state', 'progress', 'message', 'size')} | {
            'stream_path': f'/v1/media/files/{media_id}/compatible/stream' if value['state'] == 'ready' else None}

    async def status(self, media_id):
        record = await self.library.file_record(media_id)
        return self.public(self.key(record['identity']), media_id)

    async def submit(self, media_id):
        record = await self.library.file_record(media_id)
        key = self.key(record['identity'])
        old = self.records.get(key)
        if old and old['state'] == 'ready':
            self.public(key, media_id)  # Recheck externally removed cache files.
        if old and old['state'] in {'queued', 'running', 'ready'}:
            return self.public(key, media_id)
        info = await self.library.inspection(record)
        if not info.get('audio'):
            raise JobError(422, '此视频没有可转换的音轨，可以直接观看原文件。')
        if record['size'] > self.max_file_bytes:
            raise JobError(422, '此影片超过服务端兼容缓存单片限制，请调整缓存上限或播放原文件。')
        if sum(v['state'] in {'queued', 'running'} for v in self.records.values()) >= 10:
            raise JobError(429, '兼容音轨准备队列已满，请稍后重试。')
        # inspection yielded: a concurrent button press must share this job.
        old = self.records.get(key)
        if old and old['state'] == 'ready':
            self.public(key, media_id)
        if old and old['state'] in {'queued', 'running', 'ready'}:
            return self.public(key, media_id)
        self.records[key] = {'media_id': media_id, 'identity': record['identity'], 'state': 'queued',
                             'progress': 0., 'message': '等待准备 AAC 兼容音轨。', 'size': None,
                             'created_at': now(), 'last_access': time.time()}
        self.save(key)
        self.wake.set()
        return self.public(key, media_id)

    def reserve(self, key, amount):
        usage = sum(v.get('size') or 0 for v in self.records.values() if v['state'] == 'ready')
        candidates = sorted((k for k, v in self.records.items() if k != key and v['state'] == 'ready'
                             and not self.readers.get(k)), key=lambda k: self.records[k].get('last_access') or 0.)
        for other in candidates:
            if usage + amount <= self.max_cache_bytes:
                break
            value = self.records[other]
            usage -= value.get('size') or 0
            self.output(other).unlink(missing_ok=True)
            # An evicted result is idle again; keep no unbounded history records.
            (self.directory / (other + '.json')).unlink(missing_ok=True)
            del self.records[other]
        if usage + amount > self.max_cache_bytes:
            raise JobError(503, '兼容播放缓存正在使用，请稍后重试或增加服务端缓存上限。')
        if shutil.disk_usage(self.directory).free < amount + 64 * 1024 * 1024:
            raise JobError(503, '飞牛可用空间不足，未开始准备兼容音轨。')

    def start(self):
        if self.worker is None:
            self.worker = asyncio.create_task(self.run(), name='mobile-compatible-media')
            self.wake.set()

    async def run(self):
        while not self.stopping:
            self.wake.clear()
            key = next((k for k, v in self.records.items() if v['state'] == 'queued'), None)
            if key is None:
                await self.wake.wait()
                continue
            self.active = asyncio.create_task(self.convert(key), name='aac-compatible-copy')
            try:
                await self.active
            except asyncio.CancelledError:
                pass
            finally:
                self.active = None

    async def convert(self, key):
        import video_jobs
        value = self.records[key]
        temporary = self.directory / (key + '.mkv.partial')
        source_fd = None
        last_save = 0.
        try:
            value.update(state='running', progress=0., message='正在准备 AAC 兼容音轨，视频画面直接保留。')
            self.save(key)
            record = await self.library.file_record(value['media_id'])
            if record['identity'] != value['identity']:
                raise JobError(409, '媒体版本已改变，请刷新影片后重新准备兼容音轨。')
            info = await self.library.inspection(record)
            duration = info.get('duration') or 0.
            # Reserve source size plus worst-case added AAC bytes. Video is never
            # decoded/reencoded; all existing audio tracks remain selectable.
            estimate = min(self.max_file_bytes, record['size'] + math.ceil(duration * max(1, len(info.get('audio', []))) * 20_000) + 8 * 1024 * 1024)
            self.reserve(key, estimate)
            source_fd, _ = self.library.open_file(record)

            async def progress(line):
                nonlocal last_save
                if temporary.exists() and temporary.stat().st_size > self.max_file_bytes:
                    raise JobError(422, '兼容播放文件超过单片缓存限制，已停止处理，原文件保持不变。')
                text = line.decode(errors='replace').strip()
                if text.startswith('out_time_us=') and duration > 0:
                    try:
                        value['progress'] = max(0., min(.99, int(text.split('=', 1)[1]) / 1_000_000 / duration))
                    except ValueError:
                        return
                    if time.monotonic() - last_save > 1:
                        self.save(key)
                        last_save = time.monotonic()

            code, _, _ = await self.runner([video_jobs.tool('ffmpeg'), '-hide_banner', '-v', 'error', '-nostdin',
                '-i', f'/proc/self/fd/{source_fd}', '-map', '0:v:0', '-map', '0:a', '-c:v', 'copy',
                '-c:a', 'aac', '-ac', '2', '-b:a', '160k', '-sn', '-dn', '-map_metadata', '-1',
                '-max_muxing_queue_size', '2048', '-progress', 'pipe:1', '-nostats', '-y',
                '-fs', str(self.max_file_bytes), '-f', 'matroska', temporary],
                pass_fds=(source_fd,), timeout=7200, on_line=progress)
            if code:
                raise JobError(503, '兼容音轨准备失败，请检查 FFmpeg、媒体完整性和可用空间后重试。')
            size = temporary.stat().st_size
            if not 0 < size <= self.max_file_bytes:
                raise JobError(422, '兼容播放文件为空或超过服务端单片限制。')
            output_info, output_duration = await video_jobs.probe(str(temporary))
            audios = [s for s in output_info.get('streams', []) if s.get('codec_type') == 'audio']
            if (not audios or any(s.get('codec_name') != 'aac' for s in audios)
                    or abs(output_duration - duration) > max(2., duration * .01)):
                raise JobError(503, '兼容播放文件不完整或音轨校验失败，未发布缓存；请检查缓存上限后重试。')
            current = await self.library.file_record(value['media_id'])
            if current['identity'] != value['identity']:
                raise JobError(409, '媒体版本已改变，兼容播放文件未发布，请刷新影片后重试。')
            self.reserve(key, size)
            with temporary.open('rb') as finished:
                os.fsync(finished.fileno())
            temporary.replace(self.output(key))
            value.update(state='ready', progress=1., message='AAC 兼容音轨已就绪，可播放或下载到手机。', size=size)
            self.save(key)
        except asyncio.CancelledError:
            value.update(state='queued', progress=0., message='服务停止，重启后继续准备兼容音轨。')
            self.save(key)
        except Exception as error:
            # TaskGroup may wrap a bounded-output failure. Do not expose raw
            # subprocess diagnostics, source paths or arbitrary exceptions.
            message = str(error) if isinstance(error, JobError) else '兼容音轨准备失败，请检查媒体文件和可用空间后重试。'
            value.update(state='failed', progress=0., size=None, message=message)
            self.save(key)
        finally:
            if source_fd is not None:
                os.close(source_fd)
            temporary.unlink(missing_ok=True)

    async def stream_record(self, media_id):
        source = await self.library.file_record(media_id)
        key = self.key(source['identity'])
        status = self.public(key, media_id)
        if status['state'] != 'ready':
            raise JobError(409, '兼容音轨尚未准备好，请先准备并等待完成。')
        self.records[key]['last_access'] = time.time()
        self.save(key)
        self.readers[key] = self.readers.get(key, 0) + 1
        return {'mapping': {'local': self.directory}, 'relative': Path(key + '.mkv'),
                'filename': Path(source['filename']).stem + '.compatible.mkv'}, key

    def release(self, key):
        self.readers[key] = max(0, self.readers.get(key, 1) - 1)
        if not self.readers[key]:
            del self.readers[key]

    async def close(self):
        self.stopping = True
        self.wake.set()
        if self.active and not self.active.done():
            self.active.cancel()
        if self.worker:
            await self.worker
