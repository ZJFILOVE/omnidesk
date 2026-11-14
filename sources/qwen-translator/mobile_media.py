"""Server-owned Arr library, confined file access, and authenticated media routes.

Only identifiers returned by Radarr/Sonarr select files. API keys and filesystem
paths never form part of the mobile protocol. File reads are confined to mapped
libraries; explicit management requests are delegated to the owning Arr service.
"""
import asyncio
import hashlib
import json
import mimetypes
import math
from datetime import datetime, timezone
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import time
from urllib.parse import quote, urlencode
import urllib.error
import urllib.request

from fastapi import Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID

from mobile_jobs import JobError
from nas_api import ArrClient, base_url, digest

KINDS = {'radarr', 'sonarr'}
TEXT_CODECS = {'subrip', 'ass', 'ssa', 'webvtt', 'mov_text', 'text'}


def read_config(path=None):
    path = path or os.environ.get('MOBILE_MEDIA_CONFIG')
    if not path:
        return None
    try:
        value = json.loads(Path(path).expanduser().read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError):
        raise ValueError('无法读取 MOBILE_MEDIA_CONFIG；请检查服务端 JSON 配置。') from None


class MediaArrClient(ArrClient):
    def post(self, resource, payload):
        return self._write('POST', resource, payload)

    def put(self, resource, payload):
        return self._write('PUT', resource, payload)

    def delete(self, resource, **query):
        return self._write('DELETE', resource, **query)

    def _write(self, method, resource, payload=None, **query):
        url = self.base + '/api/v3/' + resource
        if query:
            url += '?' + urlencode({k: str(v).lower() if isinstance(v, bool) else v for k, v in query.items()})
        request = urllib.request.Request(url,
            data=json.dumps(payload).encode() if payload is not None else None, method=method,
            headers={'X-Api-Key': self.key, 'Accept': 'application/json', 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=30) as response:
                if not 200 <= response.status < 300:
                    raise ValueError('媒体服务未确认操作成功。')
                body = response.read(4 * 1024 * 1024 + 1)
                if len(body) > 4 * 1024 * 1024:
                    raise ValueError('媒体服务响应过大。')
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'媒体服务 HTTP {error.code}；请检查服务端配置。') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            # A timed-out write may already have taken effect. Never retry it.
            raise RuntimeError('操作未获确认，请刷新片库或下载队列核对结果，避免重复提交。') from None


class MediaLibrary:
    def __init__(self, config, directory, *, client_factory=MediaArrClient):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config = config or {}
        self.clients = {}
        self.mappings = []
        self.cache = {}
        self.cache_lock = asyncio.Lock()
        self.mutation_lock = asyncio.Lock()
        self.search_tasks = {}
        self.search_messages = {}
        for service in self.config.get('services', []):
            kind = service.get('kind')
            if kind not in KINDS or kind in self.clients:
                raise ValueError('媒体配置每种类型最多一个 Radarr / Sonarr。')
            base_url(service['url'])
            key = os.environ.get(service.get('api_key_env', ''), '').strip()
            if not key and service.get('api_key_file'):
                key = Path(service['api_key_file']).expanduser().read_text().strip()
            if not key:
                raise ValueError('媒体配置需要 api_key_env 或 api_key_file。')
            self.clients[kind] = client_factory(service, key)
        for mapping in self.config.get('mappings', []):
            remote = PurePosixPath(mapping.get('remote', ''))
            local = Path(mapping.get('local', '')).expanduser()
            if mapping.get('kind') not in KINDS or not remote.is_absolute() or '..' in remote.parts or not local.is_absolute():
                raise ValueError('媒体映射须包含 kind、remote、local 绝对目录。')
            self.mappings.append({**mapping, 'remote': remote, 'local': local.resolve()})

    @property
    def configured(self):
        return bool(self.clients)

    async def call(self, kind, resource, *, fresh=False, **query):
        if kind not in self.clients:
            raise JobError(503, '此媒体服务尚未在飞牛上配置。')
        # A short shared cache prevents repeated app refreshes from hammering Arr.
        key = (kind, resource, tuple(sorted(query.items())))
        async with self.cache_lock:
            cached = self.cache.get(key)
            if not fresh and cached and time.monotonic() - cached[0] < 5:
                return cached[1]
            try:
                value = await asyncio.to_thread(self.clients[kind].get, resource, **query)
            except Exception:
                raise JobError(503, '媒体服务暂不可达，请检查飞牛上的连接和 API Key。') from None
            if len(self.cache) >= 256:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (time.monotonic(), value)
            return value

    async def write(self, kind, method, resource, payload=None, **query):
        if kind not in self.clients:
            raise JobError(503, '此媒体服务尚未在飞牛上配置。')
        async with self.cache_lock:
            try:
                operation = getattr(self.clients[kind], method)
                arguments = (resource, payload) if payload is not None else (resource,)
                return await asyncio.to_thread(operation, *arguments, **query)
            except Exception:
                raise JobError(503, '媒体服务未确认操作结果，请刷新片库或下载队列核对后再决定是否重试。') from None
            finally:
                # Even an HTTP/network failure may follow a completed mutation.
                self.cache.clear()

    @staticmethod
    def identifier(value, expected=None):
        match = re.fullmatch(r'(radarr|sonarr):(movie|series|episode|file):([1-9][0-9]{0,9})', value)
        if not match or (expected and match[2] not in expected):
            raise JobError(404, '媒体编号不存在。')
        kind, entity, number = match[1], match[2], int(match[3])
        if (kind == 'radarr' and entity in {'series', 'episode'}) or (kind == 'sonarr' and entity == 'movie'):
            raise JobError(404, '媒体编号不存在。')
        return kind, entity, number

    @staticmethod
    def title(kind, item):
        series = kind == 'sonarr'
        counts = item.get('statistics') or {}
        available = int(counts.get('episodeFileCount') or 0) if series else int(bool(item.get('hasFile')))
        total = int(counts.get('episodeCount') or counts.get('totalEpisodeCount') or 0) if series else 1
        file = item.get('movieFile') or {}
        return {'id': f"{kind}:{'series' if series else 'movie'}:{int(item['id'])}",
                'kind': 'series' if series else 'movie', 'title': str(item.get('title') or ''),
                'year': int(item.get('year') or 0), 'overview': str(item.get('overview') or ''),
                'monitored': bool(item.get('monitored')), 'available_count': available,
                'total_count': total, 'media_id': f"radarr:file:{int(file['id'])}" if not series and item.get('hasFile') and file.get('id') else None}

    async def catalog(self):
        items, services = [], []
        for kind in sorted(self.clients):
            try:
                values = ArrClient._list(await self.call(kind, 'series' if kind == 'sonarr' else 'movie'))
                items.extend(self.title(kind, item) for item in values)
                services.append({'kind': kind, 'connected': True, 'message': '已连接'})
            except (JobError, ValueError, KeyError, TypeError):
                services.append({'kind': kind, 'connected': False, 'message': '媒体服务暂不可达或清单无效，请检查飞牛配置。'})
        return {'configured': self.configured, 'items': sorted(items, key=lambda x: x['title'].casefold()),
                'capabilities': ['manage_queue', 'manage_titles'] if self.configured else [],
                'services': services, 'message': '' if self.configured else '请先在飞牛字幕服务配置 Radarr、Sonarr 和只读媒体目录映射。'}

    async def title_detail(self, identifier):
        kind, entity, number = self.identifier(identifier, {'movie', 'series'})
        resource = 'series' if entity == 'series' else 'movie'
        values = ArrClient._list(await self.call(kind, resource))
        raw = next((x for x in values if x.get('id') == number), None)
        if not raw:
            raise JobError(404, '作品已不在媒体库中。')
        result = self.title(kind, raw)
        result['search_message'] = self.search_messages.get(identifier)
        result['episodes'] = []
        if entity == 'series':
            episodes = ArrClient._list(await self.call(kind, 'episode', seriesId=number))
            for episode in episodes:
                file_id = episode.get('episodeFileId')
                result['episodes'].append({'id': f"sonarr:episode:{int(episode['id'])}",
                    'title': str(episode.get('title') or ''), 'season_number': int(episode.get('seasonNumber') or 0),
                    'episode_number': int(episode.get('episodeNumber') or 0),
                    'has_file': bool(episode.get('hasFile') and file_id), 'monitored': bool(episode.get('monitored')),
                    'media_id': f'sonarr:file:{int(file_id)}' if episode.get('hasFile') and file_id else None})
            result['episodes'].sort(key=lambda e: (e['season_number'], e['episode_number']))
            result['available_count'] = sum(e['has_file'] for e in result['episodes'])
            result['total_count'] = len(result['episodes'])
        return result

    async def file_record(self, identifier):
        kind, _, number = self.identifier(identifier, {'file'})
        if kind == 'radarr':
            movies = ArrClient._list(await self.call(kind, 'movie'))
            raw = next((m for m in movies if m.get('hasFile') and (m.get('movieFile') or {}).get('id') == number), None)
            if not raw:
                raise JobError(404, '影片文件已不在媒体库中。')
            file, parent, title = raw['movieFile'], raw['path'], raw.get('title', '')
        else:
            # Fetching the specific file keeps playback independent of library size.
            file = await self.call(kind, f'episodefile/{number}')
            if not isinstance(file, dict) or file.get('id') != number or not file.get('seriesId'):
                raise JobError(404, '剧集文件已不在媒体库中。')
            series = ArrClient._list(await self.call(kind, 'series'))
            raw = next((s for s in series if s.get('id') == file['seriesId']), None)
            if not raw:
                raise JobError(404, '剧集已不在媒体库中。')
            parent, title = raw['path'], raw.get('title', '')
        remote = file.get('path') or str(PurePosixPath(parent) / file.get('relativePath', ''))
        mapping, relative = self.map_path(kind, remote)
        path = self.checked_path(mapping, relative)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or not info.st_size:
            raise JobError(409, '文件仍未完整入库或不可读。')
        if file.get('size') and info.st_size != file['size']:
            raise JobError(409, '媒体入库记录与文件大小不同，请等待导入完成。')
        return {'id': identifier, 'kind': kind, 'path': path, 'mapping': mapping, 'relative': relative,
                'title': str(title), 'size': info.st_size, 'filename': path.name,
                'identity': digest([remote, file.get('dateAdded'), info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev])}

    def map_path(self, kind, value):
        remote = PurePosixPath(value)
        if not remote.is_absolute() or '..' in remote.parts:
            raise JobError(403, '媒体路径未获服务端授权。')
        matches = [m for m in self.mappings if m['kind'] == kind and remote.is_relative_to(m['remote'])]
        if not matches:
            raise JobError(403, '此媒体目录尚未加入服务端只读映射。')
        mapping = max(matches, key=lambda m: len(m['remote'].parts))
        return mapping, remote.relative_to(mapping['remote'])

    @staticmethod
    def checked_path(mapping, relative):
        try:
            root = mapping['local'].resolve(strict=True)
            target = (root / str(relative)).resolve(strict=True)
            if not root.is_dir() or not target.is_relative_to(root) or not target.is_file():
                raise ValueError
            return target
        except (OSError, ValueError):
            raise JobError(403, '媒体路径越出授权目录、挂载不可用或文件不可读。') from None

    @staticmethod
    def open_file(record):
        """Walk directory descriptors without following links, including the leaf."""
        flags = os.O_RDONLY | os.O_NOFOLLOW
        root = None
        try:
            root = os.open(record['mapping']['local'], flags | os.O_DIRECTORY)
            parts = record['relative'].parts
            if not parts:
                raise ValueError
            for part in parts[:-1]:
                nxt = os.open(part, flags | os.O_DIRECTORY, dir_fd=root)
                os.close(root)
                root = nxt
            fd = os.open(parts[-1], flags, dir_fd=root)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not info.st_size:
                os.close(fd)
                raise ValueError
            return fd, info
        except (OSError, ValueError):
            raise JobError(403, '媒体文件已移动、存在链接或不可读取。') from None
        finally:
            if root is not None:
                os.close(root)

    def cache_root(self, record):
        path = self.directory / record['identity']
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    async def inspection(self, record):
        import video_jobs
        try:
            info = await video_jobs.inspect(str(record['path']), cache_root=self.cache_root(record), sidecar_contents=False)
            # inspect discovers adjacent paths. Recheck each sidecar against the
            # same authorized root before any content is read or sent to ffmpeg.
            tracks = []
            for track in info['subtitles']:
                if track.get('path'):
                    p = Path(track['path']).resolve(strict=True)
                    if not p.is_relative_to(record['mapping']['local']) or p.parent != record['path'].parent:
                        continue
                    track = {**track, 'path': str(p)}
                tracks.append(track)
            return {**info, 'subtitles': tracks}
        except (OSError, ValueError, RuntimeError):
            raise JobError(503, '媒体探测失败，请检查文件、只读映射以及飞牛上的 FFmpeg / ffprobe。') from None

    @staticmethod
    def track_id(track):
        return ('sidecar-' + hashlib.sha256(Path(track['path']).name.encode()).hexdigest()[:20]
                if track.get('path') else 'embedded-' + str(int(track['stream'])))

    async def details(self, identifier, jobs=None, compatible=None):
        record = await self.file_record(identifier)
        info = await self.inspection(record)
        tracks = [{'id': self.track_id(t), 'title': str(t.get('title') or ''),
            'language': str(t.get('language') or 'und'), 'source': 'sidecar' if t.get('path') else 'embedded',
            'download_path': f"/v1/media/files/{identifier}/subtitles/{self.track_id(t)}"} for t in info['subtitles']]
        if jobs:
            tracks.extend(jobs.available_tracks(identifier, record['identity']))
        compatibility = compatible.public(compatible.key(record['identity']), identifier) if compatible else None
        return {'id': identifier, 'title': record['title'], 'filename': record['filename'],
            'size': record['size'], 'duration': info.get('duration', 0),
            'stream_path': f'/v1/media/files/{identifier}/stream', 'subtitles': tracks,
            'audio': [{'id': int(a['index']), 'title': str(a.get('title') or ''), 'language': str(a.get('language') or 'und')} for a in info['audio']],
            'asr_available': False,
            'compatible': compatibility, 'compatible_available': bool(info.get('audio')),
            'compatible_stream_path': compatibility['stream_path'] if compatibility else None,
            'job': jobs.latest(identifier, record['identity']) if jobs else None}

    async def subtitle_cues(self, record, identifier, *, info=None, cache_root=None):
        import video_jobs
        info = info or await self.inspection(record)
        track = next((t for t in info['subtitles'] if self.track_id(t) == identifier), None)
        if track is None:
            raise JobError(404, '所选字幕已不存在，请刷新字幕选项。')
        try:
            async def load(root):
                if track.get('path'):
                    return await video_jobs.load_subtitle(track['path'], root)
                cues, _ = await video_jobs.extract_subtitle(str(record['path']), track['stream'], root)
                return cues
            if cache_root is not None:
                return await load(cache_root)
            # Ad-hoc track downloads leave no persisted transcript. Durable job
            # extractions use that job's own deletable checkpoint directory.
            with tempfile.TemporaryDirectory(dir=self.directory, prefix='subtitle-') as temporary:
                return await load(Path(temporary))
        except (OSError, ValueError, RuntimeError):
            raise JobError(422, '所选字幕损坏或无法转换，请选择其他字幕；不会自动跳过损坏字幕。') from None

    async def queue_rows(self, kind, *, fresh=False):
        rows = []
        for page in range(1, 101):
            include = {'includeMovie': True} if kind == 'radarr' else {'includeSeries': True, 'includeEpisode': True}
            data = await self.call(kind, 'queue', fresh=fresh, page=page, pageSize=200, **include)
            if not isinstance(data, dict) or 'records' not in data:
                raise JobError(503, '下载队列格式无效，请刷新后重试。')
            try:
                values = ArrClient._list(data['records'])
            except ValueError:
                raise JobError(503, '下载队列格式无效，请刷新后重试。') from None
            rows.extend(values)
            total = data.get('totalRecords', len(rows))
            if type(total) is not int or total < 0:
                raise JobError(503, '下载队列格式无效，请刷新后重试。')
            if not values or page * 200 >= total:
                return rows
        raise JobError(503, '下载队列过大，未能完整核对，请在下载工具中管理。')

    async def queue(self):
        items, services = [], []
        for kind in sorted(self.clients):
            try:
                values = await self.queue_rows(kind)
                counts = {}
                for row in values:
                    if row.get('downloadId'):
                        counts[row['downloadId']] = counts.get(row['downloadId'], 0) + 1
                for row in values:
                    size, left = max(0, int(row.get('size') or 0)), max(0, int(row.get('sizeleft') or 0))
                    items.append({'id': f"{kind}:queue:{row['id']}", 'kind': kind,
                        'title': str(row.get('title') or ''), 'status': str(row.get('status') or 'unknown'),
                        'progress': max(0., min(1., (size - left) / size)) if size else 0.,
                        'size': size, 'size_left': left, 'time_left': str(row.get('timeleft') or ''),
                        'affected_count': counts.get(row.get('downloadId'), 1)})
                services.append({'kind': kind, 'connected': True, 'message': '已连接'})
            except (JobError, ValueError, TypeError, KeyError):
                services.append({'kind': kind, 'connected': False, 'message': '下载队列暂不可达。'})
        return {'items': items, 'services': services}

    async def remove_queue(self, identifier, blocklist=False):
        # Arr queue IDs are signed .NET Int32 values, including negative IDs.
        match = re.fullmatch(r'(radarr|sonarr):queue:(0|-?[1-9][0-9]{0,9})', identifier)
        if not match or not -2_147_483_648 <= int(match[2]) <= 2_147_483_647:
            raise JobError(404, '下载任务编号不存在。')
        kind, number = match[1], int(match[2])
        async with self.mutation_lock:
            rows = await self.queue_rows(kind, fresh=True)
            row = next((r for r in rows if type(r.get('id')) is int and r['id'] == number), None)
            if row is None:
                raise JobError(404, '下载任务已不在队列中，请刷新下载队列。')
            affected = sum(r.get('downloadId') == row['downloadId'] for r in rows) if row.get('downloadId') else 1
            await self.write(kind, 'delete', f'queue/{number}', removeFromClient=True,
                             blocklist=blocklist, skipRedownload=True, changeCategory=False)
        return {'status': 'removed', 'id': identifier, 'affected_count': affected,
                'message': '已移除下载任务；下载工具会删除该任务及下载目录数据。同一整季或合集下载对应的多个条目会一起移除。'}

    async def trusted_title(self, identifier):
        kind, entity, number = self.identifier(identifier, {'movie', 'series'})
        raw = await self.call(kind, f'{entity}/{number}', fresh=True)
        if not isinstance(raw, dict) or type(raw.get('id')) is not int or raw['id'] != number:
            raise JobError(404, '作品已不在媒体库中，请刷新片库。')
        return kind, entity, number, raw

    async def monitor_title(self, identifier, monitored):
        async with self.mutation_lock:
            kind, entity, number, raw = await self.trusted_title(identifier)
            # Preserve quality profiles, paths, season monitoring, tags and all
            # other server-owned settings by changing only this one field.
            await self.write(kind, 'put', f'{entity}/{number}', {**raw, 'monitored': monitored})
            return await self.title_detail(identifier)

    def authorize_title_deletion(self, kind, raw):
        value = raw.get('path')
        if not isinstance(value, str) or not value:
            raise JobError(403, '作品目录未获服务端授权，不能删除文件。')
        mapping, relative = self.map_path(kind, value)
        if not relative.parts:
            raise JobError(403, '不能删除授权媒体库根目录，请先在 Arr 中修正作品目录。')
        # A title may have no files yet, but any existing path component must be
        # a real directory inside the configured mount, never a symbolic link.
        try:
            root = mapping['local'].resolve(strict=True)
            if not root.is_dir():
                raise ValueError
            target = root
            for part in relative.parts:
                target /= part
                if target.is_symlink() or (target.exists() and not target.is_dir()):
                    raise ValueError
            if not target.resolve().is_relative_to(root):
                raise ValueError
        except (OSError, ValueError):
            raise JobError(403, '作品目录越出授权范围、包含链接或挂载不可用，不能删除文件。') from None

    async def remove_title(self, identifier, delete_files=False, add_import_exclusion=True):
        async with self.mutation_lock:
            kind, entity, number, raw = await self.trusted_title(identifier)
            if delete_files:
                self.authorize_title_deletion(kind, raw)
            exclusion = 'addImportListExclusion' if kind == 'sonarr' else 'addImportExclusion'
            await self.write(kind, 'delete', f'{entity}/{number}',
                             deleteFiles=delete_files, **{exclusion: add_import_exclusion})
        return {'status': 'removed', 'id': identifier,
                'message': '已从片库移除作品并删除媒体库文件。' if delete_files else '已从片库移除作品，媒体库文件已保留。'}

    def service_options(self, kind):
        return next((s for s in self.config.get('services', []) if s.get('kind') == kind), {})

    def seeders_first(self, kind, profile_id):
        return profile_id in self.service_options(kind).get('seeders_first_profile_ids', [])

    @staticmethod
    def release_rank(release, episode_count=1):
        # Same logarithmic seeder bands as Arr. Within a band use size first;
        # seeder count only breaks identical-size ties. Packs use bytes/episode.
        return (round(math.log10(release['seeders'])),
                -release['size'] / max(1, episode_count), release['seeders'])

    @staticmethod
    def approved_releases(releases):
        return [r for r in releases if r.get('approved') is True and not r.get('rejected')
                and not r.get('rejections') and r.get('protocol') == 'torrent'
                and type(r.get('seeders')) is int and r['seeders'] > 0
                and type(r.get('size')) is int and r['size'] > 0
                and r.get('guid') and type(r.get('indexerId')) is int]

    async def search(self, identifier):
        async with self.mutation_lock:
            kind, entity, number, raw = await self.trusted_title(identifier)
            if self.seeders_first(kind, raw.get('qualityProfileId')):
                if entity == 'series':
                    active = self.search_tasks.get(identifier)
                    if not active or active.done():
                        self.search_messages[identifier] = '正在按季搜索：最高做种档内优先较小版本，请稍后刷新详情与下载队列。'
                        self.search_tasks[identifier] = asyncio.create_task(self.search_series_by_seeders(identifier, number))
                    return {'id': '', 'status': 'queued', 'message': self.search_messages[identifier]}
                return await self.search_by_seeders(kind, number)
            return await self.search_command(kind, entity, number)

    async def search_by_seeders(self, kind, number):
        # Keep Arr's rejection checks without ranking by quality or peer count.
        queued = await self.queue_rows(kind, fresh=True)
        if any(row.get('movieId') == number for row in queued):
            return {'id': '', 'status': 'queued', 'message': '该电影已有下载任务，请在下载队列查看进度。'}
        releases = ArrClient._list(await self.call(kind, 'release', movieId=number, fresh=True))
        candidates = self.approved_releases(releases)
        if not candidates:
            return {'id': '', 'status': 'no_match', 'message': '暂未找到符合规则且有人做种的资源，作品已保留，可稍后再搜索。'}
        chosen = max(candidates, key=self.release_rank)
        await self.write(kind, 'post', 'release', {'guid': chosen['guid'], 'indexerId': chosen['indexerId'], 'movieId': number})
        return {'id': '', 'status': 'queued', 'message': f"已选最高做种档中较小的资源（源站报告 {chosen['seeders']} 人），请在下载队列查看进度。"}

    async def search_series_by_seeders(self, identifier, number):
        submitted = 0
        try:
            episodes = ArrClient._list(await self.call('sonarr', 'episode', seriesId=number, fresh=True))
            now = datetime.now(timezone.utc).isoformat()
            missing = [e for e in episodes if e.get('monitored') and not e.get('hasFile')
                       and e.get('airDateUtc') and e['airDateUtc'] <= now]
            for season in sorted({e['seasonNumber'] for e in missing}):
                self.search_messages[identifier] = f'正在搜索第 {season} 季，已提交 {submitted} 个下载任务。'
                releases = self.approved_releases(ArrClient._list(await self.call(
                    'sonarr', 'release', seriesId=number, seasonNumber=season, fresh=True)))
                all_numbers = {e['episodeNumber'] for e in episodes if e['seasonNumber'] == season}
                pending = {e['episodeNumber'] for e in missing if e['seasonNumber'] == season}
                def covered(r):
                    return all_numbers if r.get('fullSeason') else set(r.get('episodeNumbers') or [])
                releases = [r for r in releases if r.get('seasonNumber') == season and covered(r) & pending]
                releases.sort(key=lambda r: self.release_rank(r, len(covered(r))), reverse=True)
                for release in releases:
                    async with self.mutation_lock:
                        _, _, _, current = await self.trusted_title(identifier)
                        if not current.get('monitored') or not self.seeders_first('sonarr', current.get('qualityProfileId')):
                            self.search_messages[identifier] = '作品已停止追更或更换规则，本次搜索已停止。'
                            return
                        fresh = ArrClient._list(await self.call('sonarr', 'episode', seriesId=number, fresh=True))
                        queue = await self.queue_rows('sonarr', fresh=True)
                        queued_ids = {q.get('episodeId') for q in queue if q.get('seriesId') == number}
                        pending &= {e['episodeNumber'] for e in fresh if e['seasonNumber'] == season
                                    and e.get('monitored') and not e.get('hasFile') and e['id'] not in queued_ids}
                        targets = covered(release) & pending
                        if not targets:
                            continue
                        await self.write('sonarr', 'post', 'release', {'guid': release['guid'], 'indexerId': release['indexerId']})
                        submitted += 1
                        pending -= targets
            self.search_messages[identifier] = f'搜索完成，按最高做种档内较小体积提交了 {submitted} 个下载任务；未匹配的集数保留待下载。'
        except asyncio.CancelledError:
            self.search_messages[identifier] = '本次搜索已中断，请刷新下载队列后再搜索。'
            raise
        except Exception:
            self.search_messages[identifier] = f'本次搜索未完成，已提交 {submitted} 个任务；请刷新下载队列后再搜索。'

    async def close(self):
        tasks = list(self.search_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def search_command(self, kind, entity, number):
        payload = {'name': 'MoviesSearch', 'movieIds': [number]} if entity == 'movie' else {'name': 'SeriesSearch', 'seriesId': number}
        try:
            value = await asyncio.to_thread(self.clients[kind].post, 'command', payload)
        except Exception:
            raise JobError(503, '媒体服务未确认搜索请求，请先查看下载队列再决定是否重试。') from None
        self.cache.clear()
        return {'id': str(value.get('id') or ''), 'status': str(value.get('status') or 'queued'), 'message': '已提交缺集 / 影片搜索，请在下载队列查看结果。'}

    async def discover(self, kind, query):
        if kind not in KINDS:
            raise JobError(422, '请选择电影或剧集。')
        resource, external = ('series', 'tvdbId') if kind == 'sonarr' else ('movie', 'tmdbId')
        existing = {x.get(external): x for x in ArrClient._list(await self.call(kind, resource))}
        values = ArrClient._list(await self.call(kind, resource + '/lookup', term=query))
        items = []
        for row in values[:100]:
            number = row.get(external)
            if not isinstance(number, int) or number <= 0:
                continue
            old = existing.get(number)
            items.append({'external_id': number, 'kind': kind, 'title': str(row.get('title') or ''),
                          'year': int(row.get('year') or 0), 'overview': str(row.get('overview') or ''),
                          'already_added': old is not None, 'title_id': self.title(kind, old)['id'] if old else None})
        return {'items': items}

    async def raw_options(self, kind):
        if kind not in KINDS:
            raise JobError(422, '请选择电影或剧集。')
        folders = ArrClient._list(await self.call(kind, 'rootfolder'))
        profiles = ArrClient._list(await self.call(kind, 'qualityprofile'))
        # Only configured roots that lie within an explicit media allowlist can
        # be offered. Client requests never accept a caller-provided root path.
        allowed = []
        for folder in folders:
            try:
                self.map_path(kind, folder.get('path', ''))
                if folder.get('accessible') is False:
                    continue
                allowed.append(folder)
            except JobError:
                continue
        return allowed, profiles

    async def options(self, kind):
        folders, profiles = await self.raw_options(kind)
        default = self.service_options(kind).get('default_quality_profile_id')
        default = default if any(p.get('id') == default for p in profiles) else None
        return {'root_folders': [{'id': int(f['id']), 'name': PurePosixPath(f['path']).name or '媒体库'} for f in folders],
                'quality_profiles': [{'id': int(p['id']), 'name': str(p.get('name') or ''),
                                      'description': '最高做种档内优先较小版本；排除枪版，不设体积上限。'
                                      if self.seeders_first(kind, p['id']) else ''} for p in profiles],
                'default_quality_profile_id': default}

    async def add(self, kind, data):
        resource, external, term_prefix = ('series', 'tvdbId', 'tvdb') if kind == 'sonarr' else ('movie', 'tmdbId', 'tmdb')
        if kind not in KINDS:
            raise JobError(422, '请选择电影或剧集。')
        # Reconcile a lost response before issuing another write/search.
        self.cache.clear()
        values = ArrClient._list(await self.call(kind, resource))
        existing = next((r for r in values if r.get(external) == data.external_id), None)
        if existing:
            return self.title(kind, existing)
        folders, profiles = await self.raw_options(kind)
        folder = next((f for f in folders if f.get('id') == data.root_folder_id), None)
        if folder is None or not any(p.get('id') == data.quality_profile_id for p in profiles):
            raise JobError(422, '媒体目录或画质方案已失效，请重新选择服务端提供的选项。')
        looked_up = ArrClient._list(await self.call(kind, resource + '/lookup', term=f'{term_prefix}:{data.external_id}'))
        canonical = next((r for r in looked_up if r.get(external) == data.external_id), None)
        if not canonical:
            raise JobError(404, '此作品元数据已不可用，请重新搜索。')
        # Take the trusted lookup metadata, drop identity/path fields and apply
        # only validated mobile choices. This is the standard Arr add workflow.
        payload = {k: v for k, v in canonical.items() if k not in {'id', 'path', 'rootFolderPath', 'addOptions', 'movieFile', 'statistics'}}
        payload.update(rootFolderPath=folder['path'], qualityProfileId=data.quality_profile_id,
                       monitored=data.monitored)
        if kind == 'sonarr':
            payload.update(seasonFolder=True, seriesType='standard',
                           addOptions={'monitor': 'all' if data.monitored else 'none',
                                       'searchForMissingEpisodes': data.search and not self.seeders_first(kind, data.quality_profile_id),
                                       'searchForCutoffUnmetEpisodes': False})
            payload['seasons'] = [{**s, 'monitored': data.monitored and int(s.get('seasonNumber') or 0) > 0}
                                  for s in canonical.get('seasons', [])]
        else:
            payload.update(minimumAvailability='released', addOptions={
                'searchForMovie': data.search and not self.seeders_first(kind, data.quality_profile_id)})
        try:
            result = await asyncio.to_thread(self.clients[kind].post, resource, payload)
        except Exception:
            raise JobError(503, '添加请求未获确认，请先刷新媒体库；同一作品重试不会重复添加。') from None
        self.cache.clear()
        added = self.title(kind, result)
        if data.search and self.seeders_first(kind, data.quality_profile_id):
            try:
                added['search_message'] = (await self.search(added['id']))['message']
            except JobError:
                added['search_message'] = '作品已加入片库，搜索下载未获确认；请先刷新下载队列，再到作品详情重试搜索。'
        return added


def range_response(request, record, *, on_close=None):
    try:
        fd, info = MediaLibrary.open_file(record)
    except BaseException:
        if on_close:
            on_close()
        raise

    def close():
        os.close(fd)
        if on_close:
            on_close()
    size = info.st_size
    etag = '"' + hashlib.sha256(f'{info.st_dev}:{info.st_ino}:{size}:{info.st_mtime_ns}'.encode()).hexdigest()[:32] + '"'
    headers = {'Accept-Ranges': 'bytes', 'ETag': etag,
        'Content-Disposition': "inline; filename*=UTF-8''" + quote(record['filename']),
        'Content-Length': str(size)}
    start, end, status_code = 0, size - 1, 200
    value = request.headers.get('range')
    if value and request.headers.get('if-range', etag) == etag:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', value.strip())
        try:
            if not match or not any(match.groups()):
                raise ValueError
            a, b = match.groups()
            if a:
                start, end = int(a), min(size - 1, int(b)) if b else size - 1
            else:
                suffix = int(b)
                if suffix <= 0:
                    raise ValueError
                start, end = max(0, size - suffix), size - 1
            if start >= size or start > end:
                raise ValueError
        except ValueError:
            close()
            return Response(status_code=416, headers={'Content-Range': f'bytes */{size}', 'Accept-Ranges': 'bytes', 'ETag': etag})
        status_code = 206
        headers.update({'Content-Range': f'bytes {start}-{end}/{size}', 'Content-Length': str(end - start + 1)})
    media_type = mimetypes.guess_type(record['filename'])[0] or 'application/octet-stream'
    if request.method == 'HEAD':
        close()
        return Response(status_code=status_code, headers=headers, media_type=media_type)

    def chunks():
        offset, remaining = start, end - start + 1
        while remaining:
            chunk = os.pread(fd, min(1024 * 1024, remaining), offset)
            if not chunk:
                break
            remaining -= len(chunk)
            offset += len(chunk)
            yield chunk

    class ConfinedStream(StreamingResponse):
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                # Also closes when a client disconnects before iteration starts.
                close()
    return ConfinedStream(chunks(), status_code=status_code, headers=headers, media_type=media_type)


class MediaSubmission(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    client_request_id: str = Field(min_length=36, max_length=36)
    subtitle_id: str | None = Field(default=None, max_length=128)
    force_asr: bool = False
    audio_id: int | None = Field(default=None, ge=0, le=1024)


class MediaAddition(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    external_id: int = Field(gt=0, le=2_147_483_647)
    quality_profile_id: int = Field(gt=0)
    root_folder_id: int = Field(gt=0)
    monitored: bool = True
    search: bool = True


class QueueRemoval(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    blocklist: bool = False


class TitleMonitoring(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    monitored: bool


class TitleRemoval(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    delete_files: bool = False
    add_import_exclusion: bool = True


async def management_request(request, model):
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
        raise JobError(415, 'Use application/json.')
    try:
        return model.model_validate(await request.json())
    except (ValueError, UnicodeError):
        raise JobError(422, '无效管理请求；只允许指定的布尔选项，不接受媒体路径或额外字段。') from None


def install_routes(app):
    @app.get('/v1/media/discover')
    async def discover(kind: str, query: str = Query(min_length=1, max_length=150)):
        return await app.state.media.discover(kind, query.strip())

    @app.get('/v1/media/services/{kind}/options')
    async def options(kind: str):
        return await app.state.media.options(kind)

    @app.post('/v1/media/services/{kind}/add', status_code=201)
    async def add(request: Request, kind: str):
        if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            return JSONResponse({'detail': 'Use application/json.'}, status_code=415)
        try:
            data = MediaAddition.model_validate(await request.json())
        except (ValueError, UnicodeError):
            return JSONResponse({'detail': '无效添加请求；请选择作品、画质方案和媒体目录。'}, status_code=422)
        return await app.state.media.add(kind, data)

    @app.get('/v1/media/catalog')
    async def catalog():
        return await app.state.media.catalog()

    @app.get('/v1/media/queue')
    async def queue():
        return await app.state.media.queue()

    @app.post('/v1/media/queue/{identifier}/remove')
    async def remove_queue(request: Request, identifier: str):
        data = await management_request(request, QueueRemoval)
        return await app.state.media.remove_queue(identifier, data.blocklist)

    @app.get('/v1/media/titles/{identifier}')
    async def title(identifier: str):
        return await app.state.media.title_detail(identifier)

    @app.post('/v1/media/titles/{identifier}/monitor')
    async def monitor_title(request: Request, identifier: str):
        data = await management_request(request, TitleMonitoring)
        return await app.state.media.monitor_title(identifier, data.monitored)

    @app.post('/v1/media/titles/{identifier}/remove')
    async def remove_title(request: Request, identifier: str):
        data = await management_request(request, TitleRemoval)
        return await app.state.media.remove_title(identifier, data.delete_files, data.add_import_exclusion)

    @app.post('/v1/media/titles/{identifier}/search', status_code=202)
    async def search(identifier: str):
        return await app.state.media.search(identifier)

    @app.get('/v1/media/files/{identifier}')
    async def file(identifier: str):
        return await app.state.media.details(identifier, app.state.media_jobs, app.state.compatible_media)

    @app.get('/v1/media/files/{identifier}/compatible')
    async def compatible_status(identifier: str):
        return await app.state.compatible_media.status(identifier)

    @app.post('/v1/media/files/{identifier}/compatible', status_code=202)
    async def compatible_prepare(identifier: str):
        return await app.state.compatible_media.submit(identifier)

    @app.api_route('/v1/media/files/{identifier}/compatible/stream', methods=['GET', 'HEAD'])
    async def compatible_stream(request: Request, identifier: str):
        record, key = await app.state.compatible_media.stream_record(identifier)
        return range_response(request, record, on_close=lambda: app.state.compatible_media.release(key))

    @app.api_route('/v1/media/files/{identifier}/stream', methods=['GET', 'HEAD'])
    async def stream(request: Request, identifier: str):
        record = await app.state.media.file_record(identifier)
        return range_response(request, record)

    @app.get('/v1/media/files/{identifier}/subtitles/{subtitle_id}')
    async def subtitle(identifier: str, subtitle_id: str):
        from video_subtitles import compose
        record = await app.state.media.file_record(identifier)
        body = compose(await app.state.media.subtitle_cues(record, subtitle_id))
        return Response(body, media_type='application/x-subrip; charset=utf-8')

    @app.post('/v1/media/files/{identifier}/prepare', status_code=202)
    async def prepare(request: Request, identifier: str):
        if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            return JSONResponse({'detail': 'Use application/json.'}, status_code=415)
        try:
            data = MediaSubmission.model_validate(await request.json())
            request_id = str(UUID(data.client_request_id))
            if request_id != data.client_request_id.lower() or (data.force_asr and data.subtitle_id):
                raise ValueError
        except (ValueError, UnicodeError):
            return JSONResponse({'detail': '无效字幕任务：需要 UUID 和有效的文本字幕选项。'}, status_code=422)
        return await app.state.media_jobs.submit_media(request_id, identifier, data.subtitle_id, data.force_asr, data.audio_id)

    @app.get('/v1/media/jobs/{jid}')
    async def job(jid: UUID):
        return app.state.media_jobs.get(str(jid))

    @app.post('/v1/media/jobs/{jid}/cancel')
    async def cancel(jid: UUID):
        return await app.state.media_jobs.cancel(str(jid))

    @app.post('/v1/media/jobs/{jid}/retry')
    async def retry(jid: UUID):
        return app.state.media_jobs.retry(str(jid))

    @app.delete('/v1/media/requests/{request_id}')
    async def delete(request_id: UUID):
        return await app.state.media_jobs.delete_request(str(request_id))

    @app.get('/v1/media/jobs/{jid}/subtitles/{kind}')
    async def generated_subtitle(jid: UUID, kind: str):
        return Response(app.state.media_jobs.subtitle(str(jid), kind), media_type='application/x-subrip; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="{jid}.{kind}.srt"'})
