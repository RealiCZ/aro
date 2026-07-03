"""Thin shim — the logic lives in `aro/verify.py` (`aro verify-patch …`).

    python3 verify_patch.py <patch> --spec <spec.json> [--ab-pairs N] [--aa-runs N]
                            [--out DIR] [--reuse-out]
"""
import sys

from aro.cli import main

if __name__ == "__main__":
    main(["verify-patch"] + sys.argv[1:])
