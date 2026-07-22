#!/usr/bin/env bash
set -euo pipefail
ARO=/nvme2/mega-engineer/workspace/aro
ALG=/nvme2/mega-engineer/workspace/algebra
SALT=/nvme2/mega-engineer/workspace/salt
BASE_SHA=01b20e377460e7af9da069b0c96f2d1158a7b974
CAND_SHA=03ee25353a9ed5655af0a5f8ba4e82982de1189e
SALT_SHA=19419f4d13e6c615b7a94cf3d2bf53d1052f723c
BASE_ALG=$ARO/.aro-worktrees/a6ee3a9b-wall-base-algebra-20260722
CAND_ALG=$ARO/.aro-worktrees/a6ee3a9b-backport-certify-20260721
BASE_SALT=/nvme2/mega-engineer/workspace/aro-worktrees/a6ee3a9b-wall-base-salt-20260722
CAND_SALT=/nvme2/mega-engineer/workspace/aro-worktrees/a6ee3a9b-wall-candidate-salt-20260722
RUN=$ARO/.aro-runs/a6ee3a9b-pr-ready-20260722/performance/wall
BIN=$RUN/bin
export PATH="$HOME/.cargo/bin:$PATH"
export RUSTUP_TOOLCHAIN=nightly-2026-03-20
export RAYON_NUM_THREADS=16
export RUSTFLAGS='--check-cfg=cfg(coverage_nightly)'
mkdir -p "$RUN/logs" "$BIN"

cleanup() {
  git -C "$ALG" worktree remove --force "$BASE_ALG" >/dev/null 2>&1 || true
  git -C "$SALT" worktree remove --force "$BASE_SALT" >/dev/null 2>&1 || true
  git -C "$SALT" worktree remove --force "$CAND_SALT" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM
cleanup

git -C "$ALG" worktree prune
git -C "$SALT" worktree prune
git -C "$ALG" worktree add --detach "$BASE_ALG" "$BASE_SHA"
git -C "$SALT" worktree add --detach "$BASE_SALT" "$SALT_SHA"
git -C "$SALT" worktree add --detach "$CAND_SALT" "$SALT_SHA"

install_probes() {
  local wt=$1
  mkdir -p "$wt/salt/examples" "$wt/banderwagon/examples"
  cp "$ARO/probes/salt_state_update.rs" "$wt/salt/examples/"
  cp "$ARO/probes/salt_witness.rs" "$wt/salt/examples/"
  cp "$ARO/probes/salt_msm.rs" "$wt/banderwagon/examples/"
}
install_probes "$BASE_SALT"
install_probes "$CAND_SALT"

patch_args() {
  local root=$1
  printf '%s\n' \
    --config "patch.\"https://github.com/megaeth-labs/algebra.git\".ark-ff.path=\"$root/ff\"" \
    --config "patch.\"https://github.com/megaeth-labs/algebra.git\".ark-ec.path=\"$root/ec\"" \
    --config "patch.\"https://github.com/megaeth-labs/algebra.git\".ark-serialize.path=\"$root/serialize\"" \
    --config "patch.\"https://github.com/megaeth-labs/algebra.git\".ark-poly.path=\"$root/poly\"" \
    --config "patch.\"https://github.com/megaeth-labs/algebra.git\".ark-ed-on-bls12-381-bandersnatch.path=\"$root/curves/ed_on_bls12_381_bandersnatch\"" \
    --config "patch.crates-io.ark-ff.path=\"$root/ff\"" \
    --config "patch.crates-io.ark-ec.path=\"$root/ec\"" \
    --config "patch.crates-io.ark-serialize.path=\"$root/serialize\"" \
    --config "patch.crates-io.ark-poly.path=\"$root/poly\""
}

build_mode() {
  local mode=$1 salt_wt=$2 producer=$3
  local target=/nvme2/mega-engineer/workspace/aro-targets/a6ee3a9b-wall-$mode-20260722
  mapfile -t patch < <(patch_args "$producer")
  CARGO_TARGET_DIR="$target" cargo "${patch[@]}" metadata --format-version 1 --manifest-path "$salt_wt/Cargo.toml" > "$RUN/metadata-$mode.json"
  python3 - "$RUN/metadata-$mode.json" "$producer" "$RUN/path-patch-$mode.txt" <<'PY'
import json,sys
from pathlib import Path
d=json.loads(Path(sys.argv[1]).read_text()); root=Path(sys.argv[2]).resolve(); rows=[]
req={"ark-ff":root/"ff/Cargo.toml","ark-ec":root/"ec/Cargo.toml","ark-serialize":root/"serialize/Cargo.toml","ark-poly":root/"poly/Cargo.toml","ark-ed-on-bls12-381-bandersnatch":root/"curves/ed_on_bls12_381_bandersnatch/Cargo.toml"}
for name,expected in req.items():
 m=[p for p in d['packages'] if p['name']==name and Path(p['manifest_path']).resolve()==expected]
 if not m or any(p['source'] is not None for p in m): raise SystemExit(f'path patch inactive: {name}: {m}')
 rows.append(f'{name}|{expected}|source=null')
Path(sys.argv[3]).write_text('\n'.join(rows)+'\n')
PY
  CARGO_TARGET_DIR="$target" cargo "${patch[@]}" build --manifest-path "$salt_wt/Cargo.toml" --release -p salt --example salt_state_update --features test-bucket-resize > "$RUN/logs/build-state-update-$mode.log" 2>&1
  cp "$target/release/examples/salt_state_update" "$BIN/state-update-$mode"
  CARGO_TARGET_DIR="$target" cargo "${patch[@]}" build --manifest-path "$salt_wt/Cargo.toml" --release -p salt --example salt_witness > "$RUN/logs/build-witness-$mode.log" 2>&1
  cp "$target/release/examples/salt_witness" "$BIN/witness-$mode"
  CARGO_TARGET_DIR="$target" cargo "${patch[@]}" build --manifest-path "$salt_wt/Cargo.toml" --release -p banderwagon --example salt_msm > "$RUN/logs/build-msm-$mode.log" 2>&1
  cp "$target/release/examples/salt_msm" "$BIN/msm-$mode"
}

cat > "$RUN/environment.txt" <<EOF
started_at=$(date -u +%FT%TZ)
base_sha=$BASE_SHA
candidate_sha=$CAND_SHA
salt_sha=$SALT_SHA
rustup_toolchain=$RUSTUP_TOOLCHAIN
rayon_num_threads=$RAYON_NUM_THREADS
rustflags=$RUSTFLAGS
cpu_affinity=0-15
order=strict ABAB (baseline then candidate for every adjacent sample)
rounds_per_mode=5
samples_per_mode_round=3
scale=1
surfaces=state-update,witness,salt-msm
profile=Salt checked-in [profile.release]
EOF
build_mode baseline "$BASE_SALT" "$BASE_ALG"
build_mode candidate "$CAND_SALT" "$CAND_ALG"
sha256sum "$BIN"/* > "$RUN/binaries.sha256"
python3 "$ARO/.aro-runs/a6ee3a9b-pr-ready-20260722/run-wall.py" "$RUN" > "$RUN/summary.stdout.json"
echo "finished_at=$(date -u +%FT%TZ)" >> "$RUN/environment.txt"
echo WALL_CAMPAIGN_PASS
