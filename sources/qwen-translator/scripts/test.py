"""Run each test under its owning interpreter; never contact a real model."""
import argparse
import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime_environment import WORKER_PYTHON, probe_python, qt_python


def uses_qt(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    qt_modules = {'PyQt6', 'native_app', 'desktop_ui', 'video_ui', 'video_player',
                  'caption_window', 'ui_controls', 'worker_shutdown', 'nas_ui', 'pdf_ui', 'music_ui'}
    for node in ast.walk(tree):
        names = [node.module or ''] if isinstance(node, ast.ImportFrom) else (
            [item.name for item in node.names] if isinstance(node, ast.Import) else [])
        if any(name.split('.')[0] in qt_modules for name in names):
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=['all', 'core', 'qt', 'extension', 'firefox'], default='all')
    parser.add_argument('--with-firefox', action='store_true', help='Include the optional real Firefox DOM tests.')
    args = parser.parse_args()
    groups = {'core': [], 'qt': [], 'firefox': []}
    for path in sorted((ROOT / 'tests').glob('test_*.py')):
        group = 'firefox' if path.stem.startswith('test_firefox_') else ('qt' if uses_qt(path) else 'core')
        groups[group].append(path.stem)
    selected = ['core', 'qt', 'extension'] if args.suite == 'all' else [args.suite]
    if args.with_firefox and 'firefox' not in selected:
        selected.append('firefox')
    env = {**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
           'PLAYWRIGHT_BROWSERS_PATH': str(ROOT / 'runtime/browsers'),
           'QWEN_FIREFOX_TESTS': '1' if 'firefox' in selected else '0'}
    runner = ('import sys,unittest; sys.path.insert(0,"tests"); '
              'suite=unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:]); '
              'result=unittest.TextTestRunner(verbosity=2).run(suite); '
              'sys.exit(not result.wasSuccessful())')
    failed = []
    for group in selected:
        print(f'\nRunning {group} tests', flush=True)
        try:
            if group == 'extension':
                node = shutil.which('node')
                if not node:
                    raise RuntimeError('未找到 Node.js，无法运行扩展后台测试。')
                command = [node, '--test', *map(str, sorted((ROOT / 'tests').glob('test_*.cjs')))]
            else:
                if group == 'qt':
                    python = qt_python()
                else:
                    python = str(WORKER_PYTHON)
                    modules = ['fastapi', 'uvicorn', 'websockets'] + (['playwright'] if group == 'firefox' else [])
                    if not probe_python(python, modules):
                        raise RuntimeError('项目 .venv 缺少依赖；请运行 python3 scripts/setup.py（Firefox 测试另加 --firefox-tests）。')
                command = [python, '-c', runner, *groups[group]]
            if subprocess.run(command, cwd=ROOT, env=env).returncode:
                failed.append(group)
        except (OSError, RuntimeError) as error:
            print(str(error), file=sys.stderr)
            failed.append(group)
    if args.suite == 'all' and not args.with_firefox:
        print('\nFirefox DOM tests not selected; use --with-firefox to include them.')
    print('\nFailed: ' + ', '.join(failed) if failed else '\nAll selected test groups passed.')
    return bool(failed)


if __name__ == '__main__':
    raise SystemExit(main())
