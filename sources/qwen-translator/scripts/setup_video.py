"""Compatibility entry point for installing core and video dependencies."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    subprocess.run([sys.executable, str(ROOT / 'scripts/setup.py'), '--video', *sys.argv[1:]], check=True)


if __name__ == '__main__':
    main()
