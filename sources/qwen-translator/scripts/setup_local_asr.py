"""Install pinned official FunASR llama.cpp runtime and GGUFs, verifying SHA-256."""
import hashlib
import io
from pathlib import Path
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    ('https://github.com/modelscope/FunASR/releases/download/runtime-llamacpp-v0.2.6/funasr-llamacpp-linux-x64-vulkan.tar.gz',
     'runtime/funasr-runtime.tar.gz', 'f02d41e98e9d4041f0896661007193810f025484d2175958f7c1313d5c90ec46'),
    ('https://huggingface.co/FunAudioLLM/SenseVoiceSmall-GGUF/resolve/main/sensevoice-small-q8.gguf',
     'models/funasr/sensevoice-small-q8.gguf', '4ae45c94422de949b387e2e0fb10d7e14e4c42c69db30c3444ecc7d4b844b7c5'),
    ('https://huggingface.co/FunAudioLLM/fsmn-vad-GGUF/resolve/main/fsmn-vad.gguf',
     'models/funasr/fsmn-vad.gguf', '1270f2559c495f4e7b6e739541151027d360761a3fda43fc147034f5719f5479'),
]


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    for url, relative, expected in FILES:
        path = ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or checksum(path) != expected:
            print('Downloading', path.name, flush=True)
            partial = path.with_suffix(path.suffix + '.part')
            with urllib.request.urlopen(url, timeout=60) as response, partial.open('wb') as target:
                while chunk := response.read(1024 * 1024):
                    target.write(chunk)
            if checksum(partial) != expected:
                raise RuntimeError('Checksum mismatch: ' + path.name)
            partial.replace(path)
        print('Verified', path.name, flush=True)
    destination = ROOT / 'runtime/funasr'
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(ROOT / FILES[0][1]) as archive:
        archive.extractall(destination, filter='data')
    print('Local ASR installed. Choose Only subtitles in the desktop app.')


if __name__ == '__main__':
    main()
