#!/usr/bin/env bash
set -euo pipefail
ARO=/nvme2/mega-engineer/workspace/aro
ALG=/nvme2/mega-engineer/workspace/algebra
WT=/nvme2/mega-engineer/workspace/aro-worktrees/a6ee3a9b-livelock-candidate-20260721
RUN="$ARO/.aro-runs/a6ee3a9b-livelock-investigation-20260721"
TARGET=/nvme2/mega-engineer/workspace/aro-targets/a6ee3a9b-livelock-baseline
OUT="$RUN/baseline-short-window"
mkdir -p "$TARGET" "$OUT"
export PATH="$HOME/.cargo/bin:$PATH"
export RUSTUP_TOOLCHAIN=nightly-2026-03-20
PATCH=(
  --config "patch.\"https://github.com/megaeth-labs/algebra.git\".ark-ff.path=\"$ALG/ff\""
  --config "patch.\"https://github.com/megaeth-labs/algebra.git\".ark-ec.path=\"$ALG/ec\""
  --config "patch.\"https://github.com/megaeth-labs/algebra.git\".ark-serialize.path=\"$ALG/serialize\""
  --config "patch.\"https://github.com/megaeth-labs/algebra.git\".ark-poly.path=\"$ALG/poly\""
  --config "patch.\"https://github.com/megaeth-labs/algebra.git\".ark-ed-on-bls12-381-bandersnatch.path=\"$ALG/curves/ed_on_bls12_381_bandersnatch\""
  --config "patch.crates-io.ark-ff.path=\"$ALG/ff\""
  --config "patch.crates-io.ark-ec.path=\"$ALG/ec\""
  --config "patch.crates-io.ark-serialize.path=\"$ALG/serialize\""
  --config "patch.crates-io.ark-poly.path=\"$ALG/poly\""
)
cd "$ALG"
CARGO_TARGET_DIR="$TARGET" cargo "${PATCH[@]}" test --manifest-path "$WT/Cargo.toml" \
  --features test-bucket-resize --no-run --message-format=json \
  > "$OUT/cargo-no-run.jsonl" 2> "$OUT/cargo-no-run.stderr.log"
python3 - "$OUT/cargo-no-run.jsonl" "$OUT/test-binary.txt" <<'PY'
import json,sys
from pathlib import Path
xs=[]
for line in Path(sys.argv[1]).read_text().splitlines():
 try:o=json.loads(line)
 except:continue
 if o.get('reason')=='compiler-artifact' and o.get('profile',{}).get('test') and o.get('target',{}).get('name')=='salt' and 'lib' in o.get('target',{}).get('kind',[]) and o.get('executable'):
  xs.append(o['executable'])
if len(xs)!=1: raise SystemExit(f'expected 1 salt lib binary, got {xs}')
Path(sys.argv[2]).write_text(xs[0]+'\n')
PY
BIN=$(cat "$OUT/test-binary.txt")
printf 'attempt\tstarted_utc\texit_code\telapsed_seconds\n' > "$OUT/results.tsv"
for n in 1 2 3 4 5; do
 start=$(date -u +%FT%TZ); before=$(date +%s)
 set +e
 NUM_DATA_BUCKETS=2 BUCKET_RESIZE_LOAD_FACTOR_PCT=1 timeout --signal=TERM --kill-after=10s 20s \
   "$BIN" --nocapture > "$OUT/attempt-$n.log" 2>&1
 rc=$?
 set -e
 elapsed=$(($(date +%s)-before))
 printf '%s\t%s\t%s\t%s\n' "$n" "$start" "$rc" "$elapsed" >> "$OUT/results.tsv"
 echo "attempt=$n rc=$rc elapsed=${elapsed}s"
 if [[ "$rc" == 124 ]]; then
   echo "$n" > "$OUT/hung-attempt.txt"
   exit 0
 fi
done
echo none > "$OUT/hung-attempt.txt"
