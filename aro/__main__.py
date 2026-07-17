"""ARO CLI entry points.

    python3 -m aro <cmd> …
    aro <cmd> …   (console script once pip-installed)
"""
from __future__ import annotations

import sys


def main(argv):
    """Back-compat entry (`python3 -m aro …`) — parsing lives in aro/cli.py."""
    from .cli import main as cli_main
    cli_main(argv)


def cli_entry():
    """Console-script entry point (`aro …` once pip-installed)."""
    from .cli import main as cli_main
    cli_main(sys.argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
