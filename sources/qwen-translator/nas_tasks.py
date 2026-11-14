"""Short-lived, cancellable NAS I/O jobs hosted by the existing video worker."""
import asyncio

from nas_io import Conflict, VersionChanged, Waiting, probe_mapping, publish
from nas_jobs import ManualChoice, generate
from nas_queue import Queue


async def action(config, emit):
    queue = None
    try:
        name = config['action']
        if name == 'nas_probe':
            result = probe_mapping(config['mapping'])
        elif name == 'nas_adopt':
            from nas_handoff import adopt_prepared
            queue = Queue(config['database'])
            result = adopt_prepared(queue, config['prepared'], config['mappings'],
                                    config['processing'], job_id=config.get('job_id'))
        elif name in {'nas_generate', 'nas_publish'}:
            queue = Queue(config['database'])
            job = queue.job(config['job_id'])
            if job is None or job['state'] in {'superseded', 'missing', 'cancelled', 'paused'}:
                raise ValueError('任务已失效。')
            if name == 'nas_generate':
                result = await generate(queue, job, config['mappings'], config.get('secrets', {}), emit)
            else:
                result = publish(queue, job, config['mappings'])
        elif name in {'nas_key', 'nas_test'}:
            from credentials import nas_credential
            if name == 'nas_key':
                await asyncio.to_thread(nas_credential, config['operation'], config['service'], config.get('key', ''))
                result = {'message': '系统密钥库操作完成；API Key 未写入设置或队列。'}
            else:
                from nas_api import fetch
                key = config.get('key') or await asyncio.to_thread(nas_credential, 'lookup', config['service'])
                result = await fetch(config['service'], key, 'status')
                if config.get('key'):
                    await asyncio.to_thread(nas_credential, 'store', config['service'], key)
                result['message'] = '连接通过 · ' + result['appName'] + ' ' + result['version']
        else:
            raise ValueError('未知 NAS 操作。')
        await emit({'nas_result': result})
    except asyncio.CancelledError:
        raise
    except ManualChoice as error:
        await emit({'nas_error': {'state': 'manual', 'message': str(error), 'choices': error.choices}})
    except Conflict as error:
        await emit({'nas_error': {'state': 'conflict', 'message': str(error)}})
    except VersionChanged as error:
        await emit({'nas_error': {'state': 'changed', 'message': str(error)}})
    except Waiting as error:
        await emit({'nas_error': {'state': 'waiting', 'message': str(error)}})
    except (OSError, TimeoutError) as error:
        import errno
        reason = ('挂载只读或没有目录写入权限' if getattr(error, 'errno', None) in {errno.EROFS, errno.EACCES, errno.EPERM}
                  else '路径、挂载、文件读取或写入暂不可用')
        await emit({'nas_error': {'state': 'waiting', 'message':
            reason + '；结果与检查点已保留。请检查映射测试和 NAS 连接。'}})
    except Exception as error:
        from text_translation import safe_error
        message = safe_error(error)
        for secret in (*config.get('secrets', {}).values(), config.get('key', '')):
            if isinstance(secret, str) and secret:
                message = message.replace(secret, '[hidden]')
        transient = isinstance(error, RuntimeError) and any(term in message for term in ('网络', '连接', '超时', '限流', '处理失败'))
        await emit({'nas_error': {'state': 'waiting' if transient else 'failed', 'message': message}})
    finally:
        if queue:
            queue.close()
