"""Report local dependencies without recording audio or opening a model session."""
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime_environment import WORKER_PYTHON, probe_python, qt_python


def main():
    missing = False
    try:
        print('Desktop Python: ' + qt_python())
    except RuntimeError as error:
        print('Desktop Python: ' + str(error))
        missing = True
    worker = probe_python(WORKER_PYTHON, ['fastapi', 'uvicorn', 'websockets'])
    print('Worker Python: ' + (worker['python'] + ' (' + worker['version'] + ')' if worker else 'missing; run python3 scripts/setup.py'))
    missing |= worker is None
    for name in ('pactl', 'parec', 'pacat', 'secret-tool', 'ffmpeg', 'ffprobe', 'node', 'mpv', 'codex'):
        executable = shutil.which(name)
        print(f'{name}: {executable or "not on PATH (needed only for its corresponding feature)"}')
    for name, relative in [('FunASR', 'runtime/funasr/llama-funasr-sensevoice'),
                           ('ASR model', 'models/funasr/sensevoice-small-q8.gguf'),
                           ('VAD model', 'models/funasr/fsmn-vad.gguf')]:
        print(f'{name}: {"present" if (ROOT / relative).is_file() else "not installed"}')
    print('Video Python tools: ' + ('available' if probe_python(WORKER_PYTHON, ['yt_dlp', 'ffsubsync', 'playwright']) else 'run python3 scripts/setup.py --video'))
    print('PDF Python engine: ' + ('available' if probe_python(ROOT / 'runtime/pdf-venv/bin/python',
          ['babeldoc', 'pymupdf']) else 'run python3 scripts/setup_pdf.py --warmup'))
    print('This checks installation only; audio devices, GPU, network and account access are not exercised.')
    return missing


if __name__ == '__main__':
    raise SystemExit(main())
