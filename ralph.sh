#!/usr/bin/env bash
# The thin generation layer, in its purest form: a Ralph Loop.
#
# Per ARO-eng.md, driving the model is commodity — a fresh `claude -p` per
# iteration, re-loading the previous result from durable memory, is enough.
# ARO's value is NOT here; it is in the judge (`python3 -m aro`), which this loop
# does not replace. This script only *generates*; pipe its patches into the
# evaluator for any of them to count.
#
# Usage: ./ralph.sh /path/to/PROMPT.md   (Ctrl-C to stop)
set -euo pipefail
PROMPT="${1:?usage: ./ralph.sh PROMPT.md}"
while :; do
  echo "=== ralph iteration $(date -u +%H:%M:%S) ==="
  claude -p "$(cat "$PROMPT")" || true
done
