"""Independent cancellable video jobs; no meeting devices or audio routing."""
import asyncio
import json
from pathlib import Path
import sys
from contextlib import asynccontextmanager

from text_translation import safe_error


def output(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


async def action(config):
    from video_jobs import CACHE, directory, inspect, load_existing, obtain_original, prepare
    from video_cache import cached_result
    from subtitle_sources import search, download, subtitle_files
    name = config['action']
    async def emit(event):
        output(event)
    if name.startswith('nas_'):
        from nas_tasks import action as nas_action
        await nas_action(config, emit)
    elif name == 'inspect':
        info = await inspect(config['source'])
        output({'inspection': info})
        result = cached_result(directory(info['source']), info['source'])
        if result:
            output({'result': result, 'message': '已恢复上次字幕，可直接播放或重新准备。'})
    elif name == 'open_cached':
        source = config['source']
        if not source.startswith(('https://', 'http://')):
            try:
                source = str(Path(source).expanduser().resolve(strict=True))
            except OSError:
                raise ValueError('原视频暂不可用，请检查文件位置或网盘挂载后重试。') from None
        result = cached_result(directory(source), source)
        if not result:
            raise ValueError('视频或字幕缓存已缺失、变更或损坏，请重新检查视频或准备字幕。')
        output({'result': result, 'message': '已恢复「' + result['title'] + '」的字幕，可直接点击播放。',
                'progress': 1})
    elif name == 'search':
        output({'candidates': await search(config['provider'], config['query'], config.get('assrt_token', ''))})
    elif name == 'download':
        root = directory(config['source']) / 'downloads' / (config['candidate']['provider'] + '-' + config['candidate']['id'])
        output({'files': await download(config['candidate'], root, config.get('assrt_token', ''))})
    elif name == 'import_archive':
        path = Path(config['path'])
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('字幕压缩包过大。')
        root = directory(config['source']) / 'imports'
        root.mkdir(exist_ok=True)
        files = []
        for i, (title, data) in enumerate(subtitle_files(path.read_bytes(), path.name)):
            target = root / f'{i:03}-{Path(title).name}'
            target.write_bytes(data)
            files.append({'title': title, 'path': str(target)})
        output({'files': files})
    elif name == 'prepare':
        await prepare(config, emit)
    elif name == 'load_subtitle':
        await load_existing(config, emit)
    elif name == 'obtain_original':
        await obtain_original(config, emit)
    elif name in {'token_store', 'token_lookup', 'token_clear'}:
        from credentials import text_credential
        value = await asyncio.to_thread(text_credential, name.removeprefix('token_'),
                                        'subtitle-service:assrt', config.get('assrt_token', ''))
        output({'token': value, 'message': '字幕服务密钥库操作完成'})
    else:
        raise ValueError('未知视频操作。')


@asynccontextmanager
async def video_slot(config):
    """One video computation across both the manual page and NAS queue."""
    if config['action'] not in {'prepare', 'obtain_original', 'nas_generate'}:
        yield
        return
    import fcntl
    from video_jobs import CACHE
    CACHE.mkdir(parents=True, exist_ok=True)
    with (CACHE / '.processing.lock').open('a') as lock:
        reported = False
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not reported:
                    output({'stage': '等待视频任务', 'message': '另一个视频正在处理，等待释放 ASR / 翻译资源。'})
                    reported = True
                await asyncio.sleep(.25)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


async def scheduled_action(config):
    async with video_slot(config):
        await action(config)


async def main():
    reader = asyncio.StreamReader()
    await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    config = json.loads(await reader.readline())
    task = asyncio.create_task(scheduled_action(config))
    stop = asyncio.create_task(reader.readline())
    try:
        done, _ = await asyncio.wait([task, stop], return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        else:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            output({'cancelled': True, 'message': '已取消；已完成的字幕与翻译缓存可继续使用。'})
    except Exception as error:
        message = safe_error(error, config.get('text_api_key', ''))
        if config.get('assrt_token'):
            message = message.replace(config['assrt_token'], '[hidden]')
        output({'error': message})
    finally:
        stop.cancel()
        await asyncio.gather(stop, return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(main())
