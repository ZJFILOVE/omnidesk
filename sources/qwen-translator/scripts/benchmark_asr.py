"""Measure local ASR on a supplied fixture; never captures live audio."""
import asyncio
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_subtitles import recognize
from text_translation import run_process


async def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: benchmark_asr.py fixture.wav-or-mp3')
    code, pcm, error = await run_process(['ffmpeg', '-v', 'error', '-i', sys.argv[1],
                                         '-f', 's16le', '-ar', '16000', '-ac', '1', 'pipe:1'])
    if code:
        raise SystemExit('Cannot decode fixture')
    for backend in ('vulkan', 'cpu'):
        for seconds in (2, 4, 8):
            audio = pcm[:seconds*32000]
            started = time.monotonic()
            try:
                text = await recognize(audio, {'asr_backend': backend})
                print(json.dumps({'backend': backend, 'audio_seconds': round(len(audio)/32000, 2),
                                  'wall_seconds': round(time.monotonic()-started, 3), 'text': text},
                                 ensure_ascii=False), flush=True)
            except Exception as error:
                print(json.dumps({'backend': backend, 'error': str(error)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
