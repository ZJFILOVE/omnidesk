"""Install worker dependencies; Qt and PipeWire stay managed by the operating system."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video', action='store_true', help='Install video tools and SubHD Chromium.')
    parser.add_argument('--firefox-tests', action='store_true', help='Install Playwright and its Firefox test browser.')
    args = parser.parse_args()
    uv = shutil.which('uv')
    if not uv:
        raise SystemExit('请先安装 uv，再运行本脚本。')
    python = ROOT / '.venv/bin/python'
    if not python.is_file():
        subprocess.run([uv, 'venv', '--python', '3.13', str(ROOT / '.venv')], check=True)
    requirements = [uv, 'pip', 'install', '--python', str(python), '-r', str(ROOT / 'requirements.txt'),
                    '-c', str(ROOT / 'constraints.txt')]
    if args.video or args.firefox_tests:
        requirements += ['-r', str(ROOT / 'requirements-video.txt')]
    subprocess.run(requirements, check=True)
    browsers = (['chromium'] if args.video else []) + (['firefox'] if args.firefox_tests else [])
    if browsers:
        subprocess.run([str(python), '-m', 'playwright', 'install', *browsers], check=True,
                       env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(ROOT / 'runtime/browsers')})
    print('依赖已安装。运行 python3 scripts/check_environment.py 检查系统组件。')


if __name__ == '__main__':
    main()
