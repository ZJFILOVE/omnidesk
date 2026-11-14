"""Application-lifetime scheduler. SQLite is local; all NAS I/O is isolated."""
import asyncio
import json
from pathlib import Path
import signal
import sys
import time

from credentials import nas_credential, text_credential
from nas_api import fetch, service_id
from nas_queue import Queue, processing_config
from worker_shutdown import WorkerTree

ROOT = Path(__file__).resolve().parent


def output(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


async def video_action(config, emit=None, timeout=28800):
    """Retain stdin so video_worker can cancel/reap its FFmpeg/model helpers."""
    process = await asyncio.create_subprocess_exec(sys.executable, str(ROOT / 'video_worker.py'),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True, limit=8 * 1024 * 1024)
    response = None
    async def read():
        nonlocal response
        while line := await process.stdout.readline():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if 'nas_result' in event or 'nas_error' in event:
                response = event
            elif event.get('error'):
                response = {'nas_error': {'state': 'failed', 'message': '视频工作进程失败，请检查依赖和翻译设置。'}}
            elif emit:
                await emit(event)
    async def drain():
        while await process.stderr.read(8192):
            pass
    readers = [asyncio.create_task(read()), asyncio.create_task(drain())]
    try:
        process.stdin.write((json.dumps(config, ensure_ascii=False) + '\n').encode())
        await process.stdin.drain()
        await asyncio.wait_for(process.wait(), timeout)
        await asyncio.gather(*readers)
        return response or {'nas_error': {'state': 'waiting', 'message': '工作进程中断，等待继续检查点。'}}
    finally:
        # Capture descendants before asking the leader to exit (nested private process groups).
        tree = WorkerTree(process.pid) if process.returncode is None else None
        try:
            if process.returncode is None:
                try:
                    process.stdin.write(b'stop\n')
                    await process.stdin.drain()
                    await asyncio.wait_for(process.wait(), 4)
                except (TimeoutError, BrokenPipeError, ConnectionResetError):
                    tree.send(signal.SIGTERM)
                    try:
                        await asyncio.wait_for(process.wait(), 1)
                    except TimeoutError:
                        tree.send(signal.SIGKILL)
                        await process.wait()
        finally:
            if tree:
                tree.send(signal.SIGKILL)
                tree.close()
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)


class Scheduler:
    def __init__(self, config):
        self.config = config
        self.queue = Queue(config.get('database', ROOT / 'nas-cache' / 'queue.sqlite3'))
        self.queue.recover()
        self.current = None
        self.running = None
        self.sync_task = None
        self.helpers = set()
        self.last_sync = 0
        self.closed = False
        self.suspended = False
        self.errors = {}
        self.ready_services = set()

    def snapshot(self):
        output({'snapshot': self.queue.snapshot(), 'paused': bool(self.queue.setting('paused')),
                'services': self.errors})

    async def sync(self):
        # Capture a consistent snapshot of configuration for this entire cycle.
        config = self.config
        for service in config.get('services', []):
            if not service.get('enabled'):
                continue
            sid = service_id(service)
            key = ''
            try:
                key = await asyncio.to_thread(nas_credential, 'lookup', service)
                items = await fetch(service, key)
                result = self.queue.sync(service, items, processing_config(config.get('processing', {})),
                                         auto=config.get('auto', False))
                self.ready_services.add(sid)
                self.errors[sid] = ('首次连接只建立清单' if result['first'] else '同步成功') + f" · {result['count']} 个文件"
                if self.current and self.queue.job(self.current)['state'] in {'superseded', 'missing'}:
                    await self.stop_current()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                from text_translation import safe_error
                message = '同步失败：' + safe_error(error, key) + '；上次清单已保留。'
                self.errors[sid] = message
                self.queue.sync_error(service, message)
            self.snapshot()
        self.last_sync = time.monotonic()

    async def run_job(self, job):
        jid = job['id']
        latest = self.queue.job(jid)
        if not latest or latest['state'] not in {'queued', 'waiting', 'generated', 'waiting_write'}:
            return
        job = latest
        self.current = jid
        writing = bool(job['result'])
        self.queue.update(jid, state='writing' if writing else 'running', error='')
        self.snapshot()
        async def progress(event):
            values = {k: event[k] for k in ('stage', 'progress', 'origin') if k in event}
            if values:
                self.queue.update(jid, **values)
                output({'progress': {'id': jid, **values, 'message': event.get('message', '')}})
        try:
            secrets = {}
            if not writing:
                cfg = job['config']
                if cfg.get('text_provider') == 'api':
                    secrets['text_api_key'] = self.config.get('text_api_key', '')
                    # Use the queued endpoint's key if preferences have since changed.
                    if (not secrets['text_api_key'] or cfg.get('text_endpoint') !=
                            self.config.get('processing', {}).get('text_endpoint')):
                        secrets['text_api_key'] = await asyncio.to_thread(text_credential, 'lookup', cfg.get('text_endpoint', ''))
                if cfg.get('search_provider') == 'assrt':
                    secrets['assrt_token'] = await asyncio.to_thread(text_credential, 'lookup', 'subtitle-service:assrt')
            response = await video_action({'action': 'nas_publish' if writing else 'nas_generate',
                'database': str(self.queue.path), 'job_id': jid, 'mappings': self.config.get('mappings', []),
                'secrets': secrets}, progress, timeout=1800 if writing else 28800)
            if error := response.get('nas_error'):
                state = error['state']
                values = {'state': state, 'error': error['message'], 'next_try': time.time() + 60}
                if writing and state in {'waiting', 'failed'}:
                    values['state'] = 'waiting_write'
                if state == 'changed':
                    values.update(state='waiting', result=None)
                if error.get('choices'):
                    values['override'] = {**(job['override'] or {}), 'choices': error['choices']}
                self.queue.update(jid, **values)
            elif writing:
                result = {**job['result'], 'publication': response['nas_result']}
                self.queue.update(jid, state='written', result=result, stage='已写回 NAS · 飞牛加载待验证', progress=1)
            else:
                result = response['nas_result']
                self.queue.update(jid, state='generated', result=result, stage='字幕已生成 · 等待写回',
                                  origin=result['origin_label'], progress=1, next_try=0)
        except asyncio.CancelledError:
            state = self.queue.job(jid)['state']
            if state in {'running', 'writing'}:
                self.queue.update(jid, state='waiting_write' if writing else 'queued')
            raise
        except Exception:
            self.queue.update(jid, state='waiting_write' if writing else 'failed', next_try=time.time() + 60,
                              error='任务未完成；检查系统密钥库、翻译设置和连接后重试。')
        finally:
            self.current = None
            self.snapshot()

    async def stop_current(self):
        if self.running and not self.running.done():
            self.running.cancel()
            await asyncio.gather(self.running, return_exceptions=True)
        self.running = None

    async def command(self, command):
        name = command.get('command')
        if name == 'configure':
            self.config = {**self.config, **command['config']}
        elif name == 'suspend':
            self.suspended = bool(command['value'])
        elif name == 'sync':
            self.last_sync = 0
        elif name == 'enqueue':
            self.queue.enqueue(command['ids'], self.config.get('processing', {}))
        elif name == 'pause_all':
            self.queue.setting('paused', command['value'])
            if command['value']:
                await self.stop_current()
        elif name in {'pause', 'cancel', 'retry', 'fallback', 'choose'}:
            for jid in command['ids']:
                if name in {'pause', 'cancel'}:
                    self.queue.update(jid, state='paused' if name == 'pause' else 'cancelled')
                    if jid == self.current:
                        await self.stop_current()
                elif name == 'retry':
                    if 'update_managed' in command:
                        override = {**(self.queue.job(jid)['override'] or {}), 'update_managed': bool(command['update_managed'])}
                        self.queue.update(jid, override=override)
                    self.queue.retry(jid)
                else:
                    override = {'skip_search': True} if name == 'fallback' else command['override']
                    self.queue.retry(jid, override)
        elif name == 'stop':
            self.closed = True
        self.snapshot()

    async def control(self, reader):
        while line := await reader.readline():
            try:
                value = json.loads(line)
            except ValueError:
                if line.strip() == b'stop':
                    self.closed = True
                    return
                continue
            try:
                await self.command(value)
            except (ValueError, KeyError):
                output({'error': '配置或队列操作无效，请检查输入。'})
            if self.closed:
                return
        self.closed = True  # Parent app has closed; never become a daemon.

    async def run(self, reader):
        control = asyncio.create_task(self.control(reader))
        self.snapshot()
        try:
            while not self.closed:
                if (self.sync_task is None or self.sync_task.done()) and (
                        not self.last_sync or time.monotonic() - self.last_sync >= max(30, int(self.config.get('interval', 300)))):
                    self.sync_task = asyncio.create_task(self.sync())
                if ((self.running is None or self.running.done()) and not self.queue.setting('paused')
                        and not self.suspended):
                    job = self.queue.next_job([service_id(s) for s in self.config.get('services', [])
                        if s.get('enabled') and service_id(s) in self.ready_services])
                    if job:
                        self.running = asyncio.create_task(self.run_job(job))
                await asyncio.sleep(.25)
        finally:
            if self.sync_task:
                self.sync_task.cancel()
                await asyncio.gather(self.sync_task, return_exceptions=True)
            await self.stop_current()
            control.cancel()
            await asyncio.gather(control, return_exceptions=True)
            self.queue.close()


async def main():
    reader = asyncio.StreamReader(limit=8 * 1024 * 1024)
    await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    config = json.loads(await reader.readline())
    await Scheduler(config).run(reader)


if __name__ == '__main__':
    asyncio.run(main())
