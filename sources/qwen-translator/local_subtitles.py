"""PipeWire -> in-memory speech segments -> FunASR GGUF -> text translation.

No playback process is created. Audio stays in anonymous memory-backed files.
"""
import array
import asyncio
from collections import deque
import io
import os
from pathlib import Path
import re
import signal
import time
import wave

from text_translation import run_process, safe_error, SubtitleTranslator
from subtitle_settings import subtitle_profile

APP_DIR = Path(__file__).resolve().parent
BINARY = APP_DIR / 'runtime/funasr/llama-funasr-sensevoice'
MODEL = APP_DIR / 'models/funasr/sensevoice-small-q8.gguf'
VAD = APP_DIR / 'models/funasr/fsmn-vad.gguf'
RATE = 16000
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2


def asr_available():
    return all(path.is_file() for path in (BINARY, MODEL, VAD)) and os.access(BINARY, os.X_OK)


class SpeechSegmenter:
    """Energy endpointing with pre-roll, a hard duration cap, and minimum speech.

    The neural FSMN VAD in the binary subsequently filters each candidate.
    """
    def __init__(self, threshold=220, max_seconds=8, silence_ms=650):
        self.threshold = threshold
        self.max_frames = round(max_seconds * 1000 / FRAME_MS)
        self.silence_frames = round(silence_ms / FRAME_MS)
        self.preroll = deque(maxlen=10)
        self.frames = []
        self.speech_frames = 0
        self.quiet = 0

    def feed(self, frame):
        samples = array.array('h', frame)
        if not samples:
            return None
        energy = (sum(x*x for x in samples) / len(samples)) ** 0.5
        speech = energy >= self.threshold
        if not self.frames:
            if not speech:
                self.preroll.append(frame)
                return None
            self.frames = list(self.preroll)
            self.preroll.clear()
        self.frames.append(frame)
        self.speech_frames += int(speech)
        self.quiet = 0 if speech else self.quiet + 1
        if self.quiet >= self.silence_frames or len(self.frames) >= self.max_frames:
            result = b''.join(self.frames) if self.speech_frames >= 12 else None
            self.frames = []
            self.speech_frames = 0
            self.quiet = 0
            return result
        return None


async def recognize(pcm, config):
    if not asr_available():
        raise RuntimeError('本地 ASR 未安装，请运行 scripts/setup_local_asr.py。')
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(RATE)
        stream.writeframes(pcm)
    fd = os.memfd_create('qwen-asr-audio', os.MFD_CLOEXEC)
    try:
        with os.fdopen(os.dup(fd), 'wb') as stream:
            stream.write(buffer.getvalue())
        os.lseek(fd, 0, os.SEEK_SET)
        backend = config.get('asr_backend', 'vulkan')
        if backend not in {'cpu','vulkan'}:
            raise ValueError('Unsupported ASR backend')
        code, out, err = await run_process([BINARY, '-m', MODEL, '--vad', VAD, '--backend', backend,
                                             '-a', f'/proc/self/fd/{fd}'], pass_fds=(fd,), timeout=45)
        if code:
            raise RuntimeError('本地识别失败；请检查模型，或将识别加速切换为 CPU。' +
                               ' ' + safe_error(err.decode(errors='replace')[-350:]))
        return re.sub(r'<\|[^|]*\|>', '', out.decode(errors='replace')).strip()
    finally:
        os.close(fd)


async def subtitles(config, reader, emit, devices_fn):
    if not asr_available():
        raise RuntimeError('本地 ASR 模型或 llama.cpp 运行时不存在，请先安装模型。')
    source = config.get('subtitle_source')
    available = await devices_fn()
    selected = next((item for item in available['sources'] if item['name'] == source), None)
    if not selected:
        raise RuntimeError('字幕音频来源已断开，请刷新设备并重新选择。')
    if selected.get('mute'):
        raise RuntimeError('所选字幕音频来源已静音，请先取消静音。')
    language = config.get('subtitle_language', 'en-zh')
    if language not in {'en-zh','zh-en'}:
        raise ValueError('Unsupported subtitle language')
    direction = 'inbound' if language == 'en-zh' else 'outbound'
    config['subtitle_language'] = language
    profile = subtitle_profile(config)
    audio_queue = asyncio.Queue(maxsize=profile['audio_queue'])
    translation_queue = asyncio.Queue(maxsize=profile['translation_queue'])
    translator = SubtitleTranslator(config)
    tasks = []
    capture = await asyncio.create_subprocess_exec(
        'parec', '--raw', '--format=s16le', '--rate=16000', '--channels=1', '--latency-msec=80',
        f'--device={source}', '--client-name=qwen-subtitles-input',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )

    async def offer(queue, item, stage):
        if queue.full():
            queue.get_nowait()
            queue.task_done()
            await emit({'direction': direction, 'warning': f'{stage}暂时跟不上，已跳过最旧的待处理片段，继续显示最新字幕。'})
        queue.put_nowait(item)

    async def collect():
        segmenter = SpeechSegmenter(threshold=max(50, min(3000, int(config.get('vad_threshold', 220)))),
                                    max_seconds=profile['max_seconds'], silence_ms=profile['silence_ms'])
        sequence = 0
        while True:
            try:
                frame = await capture.stdout.readexactly(FRAME_BYTES)
            except asyncio.IncompleteReadError:
                raise RuntimeError('音频采集已中断，请检查字幕音频来源。') from None
            if pcm := segmenter.feed(frame):
                sequence += 1
                await offer(audio_queue, (sequence, pcm, time.monotonic()), '语音识别')

    async def transcribe():
        while True:
            seq, pcm, ended = await audio_queue.get()
            try:
                started = time.monotonic()
                text = await recognize(pcm, config)
                ready = time.monotonic()
                timing = {'segment_seconds': round(len(pcm) / (RATE * 2), 2),
                          'asr_queue_seconds': round(started - ended, 2),
                          'asr_seconds': round(ready - started, 2)}
                if text:
                    await emit({'direction': direction, 'source_final': text, 'segment_id': seq,
                                'status': 'listening', **timing})
                    if config.get('text_provider', 'codex') != 'none':
                        await offer(translation_queue, (seq, text, ended, ready, timing), '文本翻译')
            finally:
                audio_queue.task_done()

    async def translate():
        while True:
            seq, text, ended, ready, timing = await translation_queue.get()
            try:
                started = time.monotonic()
                result = await translator.translate(text)
                finished = time.monotonic()
                await emit({'direction': direction, 'translation_final': result['text'], 'segment_id': seq,
                            'status': 'translating', 'latency_seconds': round(finished - ended, 2),
                            **timing, 'translation_queue_seconds': round(started - ready, 2),
                            'translation_seconds': round(finished - started, 2),
                            'usage': result.get('usage', {}), 'cache_hit': result.get('cache_hit', False)})
            except Exception as error:
                await emit({'direction': direction, 'warning': '原文已保留，文本翻译失败：' + safe_error(error, config.get('text_api_key',''))})
            finally:
                translation_queue.task_done()

    async def watch():
        while True:
            await asyncio.sleep(2)
            if source not in {item['name'] for item in (await devices_fn())['sources']}:
                raise RuntimeError('字幕音频来源已断开，会话已停止。')

    async def capture_errors():
        # Drain stderr throughout the session to avoid a full pipe blocking capture.
        tail = deque(maxlen=4)
        async for line in capture.stderr:
            tail.append(line.decode(errors='replace').strip())
        await capture.wait()
        raise RuntimeError('音频采集已停止：' + ' / '.join(tail))

    async def control():
        await reader.readline()

    try:
        await emit({'direction':direction,'status':'listening','subtitle_only':True})
        tasks = [asyncio.create_task(coro) for coro in (collect(), transcribe(), translate(), watch(), capture_errors(), control())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await translator.close()
        try:
            os.killpg(capture.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(capture.wait(), 2)
        except asyncio.TimeoutError:
            capture.kill()
            await capture.wait()
