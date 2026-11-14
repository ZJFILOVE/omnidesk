"""Local Apple Music companion: observe/control the player, cache lyrics only."""
import asyncio
from bisect import bisect_right
from contextlib import suppress
import copy
import json
import math
import sys
import time

from music_lyrics import LyricsRepository
from music_playback import MusicPlayback

HOST, PORT = '127.0.0.1', 18765
MAX_BODY = 1024 * 1024
LYRICS_RETRY_DELAYS = (5, 15, 30, 60)


def waybar_preferences(value=None):
    value = value if isinstance(value, dict) else {}
    display = value.get('display', 'translation')
    if display not in {'translation', 'original', 'bilingual'}:
        display = 'translation'
    return {'display': display, 'show_title': value.get('show_title', True) is not False,
            'show_progress': value.get('show_progress', True) is not False}


def music_config(config=None):
    # Music always uses the user's explicitly requested local Codex account.
    return {**(config or {}), 'text_provider': 'codex', 'codex_model': 'gpt-6-luna',
            'codex_reasoning_effort': 'low'}


def track_key(track, state=None):
    source = state or {}
    return json.dumps([[source.get(key) for key in ('backend', 'player', 'identity')],
                       [track.get(key) for key in ('id', 'title', 'artist', 'album', 'duration', 'url')]],
                      ensure_ascii=False, sort_keys=True)


def current_line(lyrics, position):
    lines = lyrics.get('lines', [])
    timed = [(line['start'], index) for index, line in enumerate(lines)
             if isinstance(line.get('start'), (int, float)) and math.isfinite(line['start'])]
    timed.sort()
    index = bisect_right([stamp for stamp, _ in timed], position) - 1
    if index < 0:
        return -1, None
    original_index = timed[index][1]
    return original_index, lines[original_index]


class MusicService:
    def __init__(self, config=None, playback=None, repository=None, enrich=None, emit=None):
        self.config = music_config(config)
        self.waybar = waybar_preferences(self.config.get('waybar'))
        self.playback = playback or MusicPlayback()
        self.repository = repository or LyricsRepository()
        self.enrich = enrich
        self.emit = emit or (lambda state: None)
        self.state = {'available': False, 'track': {}, 'position': 0, 'playing': False,
                      'lyrics': {}, 'capabilities': {}}
        self.lyrics = {}
        self.key = None
        self.generation = 0
        self.lyric_task = None
        self.lyric_tasks = set()
        self.retry_at = None
        self.retry_attempt = 0
        self.lock = asyncio.Lock()
        self.closed = False
        self.server = None
        self.clients = set()

    async def poll(self):
        async with self.lock:
            if self.closed:
                return
            try:
                state = await asyncio.to_thread(self.playback.snapshot)
            except Exception:
                state = {'available': False, 'track': {}, 'position': 0, 'playing': False,
                         'capabilities': {}, 'error': '暂时无法读取播放器状态，请稍后重试。'}
            await self._accept(state)

    async def _accept(self, state):
        if self.closed:
            return
        track = state.get('track') or {}
        key = track_key(track, state) if state.get('available') and track.get('title') else None
        if key != self.key:
            self.generation += 1
            self.key, self.lyrics = key, {}
            self._reset_retry()
            if self.lyric_task:
                self.lyric_task.cancel()
            if key is not None:
                self.lyrics = {'lines': [], 'translation_status': 'loading',
                               'status_text': '正在读取原生歌词与本地缓存…'}
                self._start_load(dict(track))
        elif (key is not None and self.retry_at is not None and time.monotonic() >= self.retry_at
              and (self.lyric_task is None or self.lyric_task.done())):
            self.lyrics = {**self.lyrics, 'translation_status': 'loading',
                           'status_text': '正在自动重新读取歌词…'}
            self._start_load(dict(track))
        self.state = {**state, 'updated_at': time.time(),
                      'translation_model': 'gpt-6-luna', 'lyrics': copy.deepcopy(self.lyrics)}
        self._publish()

    def _publish(self):
        self.state['waybar'] = dict(self.waybar)
        lyrics = copy.deepcopy(self.lyrics)
        index, line = current_line(lyrics, self.state.get('position') or 0)
        lyrics.update(current_index=index, current=line)
        self.state['lyrics'] = lyrics
        self.emit(copy.deepcopy(self.state))

    def _reset_retry(self):
        self.retry_at = None
        self.retry_attempt = 0

    def _start_load(self, track, *, force=False, enrich=True, translate=False, recover=True):
        self.retry_at = None
        self.lyric_task = asyncio.create_task(self._load(track, self.generation, force=force,
                                                        enrich=enrich, translate=translate, recover=recover))
        self.lyric_tasks.add(self.lyric_task)
        self.lyric_task.add_done_callback(self.lyric_tasks.discard)

    async def _load(self, track, generation, *, force=False, enrich=True, translate=False, recover=True):
        async def update(result):
            if generation == self.generation and not self.closed:
                self.lyrics = copy.deepcopy(result)
                self._publish()
        try:
            if enrich and self.enrich:
                track = await asyncio.to_thread(self.enrich, track, dict(self.state))
            request_config = {**self.config, 'music_translation_enabled': True} if translate else self.config
            result = await self.repository.get(track, request_config, force=force, on_update=update)
        except asyncio.CancelledError:
            raise
        except Exception:
            result = {'lines': [], 'translation_status': 'error',
                      'error': '歌词暂时不可用，请点击重新读取。',
                      'status_text': '歌词暂时不可用，请点击重新读取。'}
        if generation == self.generation and not self.closed:
            # Recover missing originals without repeated model calls, parallel
            # requests, or replacing an imported/visible lyric on a timer.
            if recover and not result.get('lines') and not result.get('instrumental'):
                delay = LYRICS_RETRY_DELAYS[min(self.retry_attempt, len(LYRICS_RETRY_DELAYS) - 1)]
                self.retry_attempt += 1
                self.retry_at = time.monotonic() + delay
                result = {**result, 'retry_after': delay,
                          'status_text': ('歌词暂时读取失败' if result.get('error') else '暂未找到歌词')
                                         + f'，{delay} 秒后自动重试…'}
            else:
                self._reset_retry()
            self.lyrics = result
            self._publish()

    async def command(self, data):
        if not isinstance(data, dict) or not isinstance(data.get('action'), str):
            return {'ok': False, 'error': '播放操作无效。'}
        action, value = data['action'], data.get('value')
        async with self.lock:
            if self.closed:
                return {'ok': False, 'error': '歌词服务已关闭。'}
            if action == 'update_waybar':
                if (not isinstance(value, dict) or set(value) - {'display', 'show_title', 'show_progress'}
                        or value.get('display', 'translation') not in {'translation', 'original', 'bilingual'}
                        or any(key in value and not isinstance(value[key], bool)
                               for key in ('show_title', 'show_progress'))):
                    return {'ok': False, 'error': 'Waybar 显示设置无效。'}
                self.waybar = waybar_preferences({**self.waybar, **value})
                self._publish()
                return {'ok': True}
            # Reject stale seek/next requests after the displayed song changes.
            expected = data.get('track_id')
            if expected is not None and expected != self.state.get('track', {}).get('id'):
                return {'ok': False, 'error': '歌曲已切换，请重试。'}
            if action == 'translate_lyrics':
                if self.key is None:
                    return {'ok': False, 'error': '请先在 Apple Music 播放歌曲。'}
                self.generation += 1
                self._reset_retry()
                if self.lyric_task:
                    self.lyric_task.cancel()
                # The repository has already saved every visible original. Reuse
                # it without network refresh or native enrichment; this request's
                # override never changes the user's automatic/manual preference.
                have_original = bool(self.lyrics.get('lines'))
                track = dict(self.state['track'])
                if have_original:
                    # A manual import may have replaced a player's earlier raw
                    # lyrics. The saved current original is authoritative now.
                    for field in ('lyrics', 'translated_lyrics', 'lyrics_path', 'translated_lyrics_path'):
                        track.pop(field, None)
                self.lyrics = {**self.lyrics, 'translation_status': 'translating',
                               'status_text': '正在准备中文翻译，原文可先查看…'}
                self._start_load(track, enrich=not have_original, translate=True, recover=False)
                self._publish()
                return {'ok': True}
            if action == 'retry_lyrics':
                if self.key is None:
                    return {'ok': False, 'error': '请先在 Apple Music 播放歌曲。'}
                self.generation += 1
                self._reset_retry()
                if self.lyric_task:
                    self.lyric_task.cancel()
                self.lyrics = {'lines': [], 'translation_status': 'loading', 'status_text': '正在重新读取歌词…'}
                self._start_load(dict(self.state['track']), force=True)
                self._publish()
                return {'ok': True}
            if action == 'import_lyrics':
                lyrics = data.get('lyrics')
                if self.key is None or not isinstance(lyrics, str) or not lyrics.strip() or len(lyrics.encode()) > MAX_BODY:
                    return {'ok': False, 'error': '请先播放歌曲，再选择有效的 LRC 或文本歌词。'}
                self.generation += 1
                self._reset_retry()
                if self.lyric_task:
                    self.lyric_task.cancel()
                track = {**self.state['track'], 'lyrics': lyrics}
                # Imported lyrics are authoritative; generation also fences two
                # imports of the same track when an older provider ignores cancel.
                self.lyrics = {'lines': [], 'translation_status': 'loading', 'status_text': '正在接入本地歌词…'}
                self._start_load(track, enrich=False, recover=False)
                self._publish()
                return {'ok': True}
            result = await asyncio.to_thread(self.playback.command, action, value)
            if result.get('state'):
                await self._accept(result['state'])
            return {'ok': bool(result.get('ok')), **({'error': result['error']} if result.get('error') else {})}

    async def serve(self, port=PORT):
        self.server = await asyncio.start_server(self._http, HOST, port, limit=16384)
        return self.server.sockets[0].getsockname()[1]

    async def _http(self, reader, writer):
        self.clients.add(writer)
        status, result = 400, {'error': '请求无效。'}
        try:
            async with asyncio.timeout(8):
                raw = await reader.readuntil(b'\r\n\r\n')
                rows = raw.decode('latin1').split('\r\n')
                method, path, version = rows[0].split(' ')
                headers = {}
                for row in rows[1:]:
                    if row:
                        key, value = row.split(':', 1)
                        key = key.strip().lower()
                        if key in headers:
                            raise ValueError('Duplicate header')
                        headers[key] = value.strip()
                host = headers.get('host', '')
                if (host not in {HOST, f'{HOST}:{self.server.sockets[0].getsockname()[1]}'}
                        or headers.get('origin') or headers.get('sec-fetch-site')
                        or 'transfer-encoding' in headers or version != 'HTTP/1.1'):
                    status, result = 403, {'error': '仅接受本机应用请求。'}
                elif method == 'GET' and path == '/music/state':
                    status, result = 200, self.state
                elif method == 'POST' and path == '/music/control':
                    length = int(headers.get('content-length', '0'))
                    if not 0 < length <= MAX_BODY or headers.get('content-type', '').split(';')[0] != 'application/json':
                        status, result = 413, {'error': '播放请求格式或长度无效。'}
                    else:
                        body = json.loads(await reader.readexactly(length))
                        result = await self.command(body)
                        status = 200 if result.get('ok') else 400
                else:
                    status, result = 404, {'error': '接口不存在。'}
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        except Exception:
            status, result = 500, {'error': '播放操作暂时失败。'}
        try:
            body = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
            writer.write(f'HTTP/1.1 {status} Response\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n'.encode() + body)
            await writer.drain()
        except (OSError, ValueError):
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            self.clients.discard(writer)

    async def close(self):
        self.closed = True
        self.generation += 1
        tasks = list(self.lyric_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for writer in list(self.clients):
            writer.close()


async def main():
    config = json.loads(await asyncio.to_thread(sys.stdin.readline))
    from music_adam import enrich_track
    def emit(state):
        print(json.dumps({'state': state}, ensure_ascii=False, allow_nan=False), flush=True)
    service = MusicService(config, enrich=enrich_track, emit=emit)
    try:
        await service.serve(config.get('music_port', PORT))
    except OSError:
        print(json.dumps({'error': '歌词服务端口已被占用，请关闭重复运行的 Qwen。'}, ensure_ascii=False), flush=True)
        return
    print(json.dumps({'ready': True}), flush=True)
    async def polling():
        while not service.closed:
            try:
                await service.poll()
            except Exception:
                emit({'available': False, 'track': {}, 'lyrics': {}, 'error': '暂时无法读取播放器状态。'})
            await asyncio.sleep(1)
    poll_task = asyncio.create_task(polling())
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            try:
                data = json.loads(line)
                result = await service.command(data)
                if not result.get('ok'):
                    print(json.dumps({'error': result.get('error', '播放操作失败。')}, ensure_ascii=False), flush=True)
            except (ValueError, TypeError):
                pass
    finally:
        poll_task.cancel()
        await asyncio.gather(poll_task, return_exceptions=True)
        await service.close()


if __name__ == '__main__':
    asyncio.run(main())
