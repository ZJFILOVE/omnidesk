"""Locate the desktop interpreter without mixing Qt and worker site-packages."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
WORKER_PYTHON = ROOT / '.venv/bin/python'


def probe_python(executable, modules):
    code = ('import importlib,json,sys; '
            '[importlib.import_module(name) for name in ' + repr(list(modules)) + ']; '
            'print(json.dumps({"python":sys.executable,"version":sys.version.split()[0]}))')
    try:
        result = subprocess.run([str(executable), '-c', code], cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def qt_python():
    override = os.environ.get('QWEN_QT_PYTHON')
    candidates = [override] if override else [sys.executable, '/usr/bin/python3',
                                             '/usr/bin/python', shutil.which('python3')]
    checked = set()
    for candidate in candidates:
        if not candidate or candidate in checked:
            continue
        checked.add(candidate)
        if result := probe_python(candidate, ['PyQt6.QtCore', 'PyQt6.QtWidgets', 'PyQt6.QtNetwork']):
            return result['python']
    raise RuntimeError('未找到可导入 PyQt6 的 Python。请安装系统 PyQt6，或设置 QWEN_QT_PYTHON 指向已安装 PyQt6 的解释器。')
