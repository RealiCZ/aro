"""Thin shim — the logic lives in `aro/cli.py` (`aro hotpath <spec>`).

    python3 find_hotpath.py targets/<name>.json
"""
import sys

from aro.cli import main

if __name__ == "__main__":
    main(["hotpath"] + sys.argv[1:])
