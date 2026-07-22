#!/usr/bin/env bash
set -u
RUN=/nvme2/mega-engineer/workspace/aro/.aro-runs/a6ee3a9b-livelock-investigation-20260721
BIN=$(cat "$RUN/test-binary.txt")
OUT="$RUN/thread-experiments"
mkdir -p "$OUT"
: > "$OUT/results.tsv"
printf 'threads\tstarted_utc\tfinished_utc\texit_code\telapsed_seconds\n' >> "$OUT/results.tsv"
for n in 4 16; do
  start=$(date -u +%FT%TZ)
  before=$(date +%s)
  set +e
  NUM_DATA_BUCKETS=2 BUCKET_RESIZE_LOAD_FACTOR_PCT=1 \
    timeout --signal=TERM --kill-after=10s 300s \
    "$BIN" --test-threads="$n" --nocapture \
    > "$OUT/test-threads-$n.log" 2>&1
  rc=$?
  set -e
  after=$(date +%s)
  finish=$(date -u +%FT%TZ)
  elapsed=$((after-before))
  printf '%s\t%s\t%s\t%s\t%s\n' "$n" "$start" "$finish" "$rc" "$elapsed" >> "$OUT/results.tsv"
  printf 'threads=%s rc=%s elapsed=%ss\n' "$n" "$rc" "$elapsed"
done
