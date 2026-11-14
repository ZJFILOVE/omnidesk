import os
import sys
from runtime_environment import ROOT, qt_python


def main():
    try:
        python = qt_python()
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    os.execv(python, [python, str(ROOT / 'native_app.py'), *sys.argv[1:]])


if __name__ == '__main__':
    main()
