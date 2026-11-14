"""Install the isolated PDF engine, optionally prefetch its local models and fonts."""
import argparse
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--warmup', action='store_true', help='Download layout model and fonts without calling an LLM.')
    args = parser.parse_args()
    uv = shutil.which('uv')
    if not uv:
        raise SystemExit('请先安装 uv。')
    python = ROOT / 'runtime/pdf-venv/bin/python'
    if not python.is_file():
        subprocess.run([uv, 'venv', '--python', '3.13', str(python.parent.parent)], check=True)
    subprocess.run([uv, 'pip', 'sync', '--python', str(python), str(ROOT / 'requirements-pdf.lock')], check=True)
    if args.warmup:
        subprocess.run([str(python), str(ROOT / 'pdf_worker.py'), '--warmup'], check=True)
    print('PDF 引擎已安装。打开 Qwen LiveTranslate 的 PDF 翻译页面即可使用。')


if __name__ == '__main__':
    main()
