"""JSON-lines worker; cancellation also closes the independent translation sessions."""
import asyncio
import json
import logging
import signal
import sys

from pdf_translation import initialize_engine, translate_pdf
from text_translation import safe_error


def output(event):
    print(json.dumps(event, ensure_ascii=False), file=PROTOCOL, flush=True)


async def main():
    reader = asyncio.StreamReader()
    loop = asyncio.get_running_loop()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    config = json.loads(await reader.readline())
    async def emit(event):
        output(event)
    task = asyncio.create_task(translate_pdf(config, emit))
    stop = asyncio.create_task(reader.readline())
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.cancel)
    try:
        done, _ = await asyncio.wait([task, stop], return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        else:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            output({'cancelled': True, 'message': '已取消 PDF 翻译。成功的段落缓存可在重试时复用。'})
    except Exception as error:
        if isinstance(error, (ImportError, ModuleNotFoundError)):
            message = 'PDF 引擎依赖缺失，请运行 python3 scripts/setup_pdf.py。'
        else:
            message = safe_error(error, config.get('text_api_key', ''))
        output({'error': message})
        return 1
    finally:
        stop.cancel()
        await asyncio.gather(stop, return_exceptions=True)


if __name__ == '__main__':
    PROTOCOL = sys.stdout
    # Third-party progress, prompts and debug messages never enter our event stream.
    sys.stdout = sys.stderr
    logging.disable(logging.CRITICAL)
    if '--warmup' in sys.argv:
        initialize_engine()
        from babeldoc.assets.assets import warmup
        warmup()
        output({'message': '本地模型与字体已就绪。'})
    else:
        raise SystemExit(asyncio.run(main()))
