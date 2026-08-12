"""PyInstaller entry point: the double-clicked binary IS `apps.helper app`.

Extra CLI args pass through, so `./DrishtiHelper --port 9000` works —
double-clicking supplies none and gets the defaults.
"""
import sys

from apps.helper.__main__ import main

if __name__ == "__main__":
    sys.exit(main(["app", *sys.argv[1:]]))
