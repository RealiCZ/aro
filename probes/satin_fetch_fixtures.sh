#!/usr/bin/env bash
# satin_fetch_fixtures.sh DEST [MEGA_EVM_CHECKOUT]
#
# Fetches the execution-spec fixtures the Satin differential (satin_outcome_digest.py) runs, into
# DEST/main (state_tests, blockchain_tests) and DEST/devnet (state_tests), and stamps
# DEST/FIXTURES.txt. The releases are the ones mega-evm's CI gate (.github/workflows/
# exec-spec-satin.yml) downloads at the target's baseline: the CI reads them from the revm fork's
# scripts/run-tests.sh at the revision Cargo.lock pins (fd230a34 at 2e482f22), which names
#   main   = execution-spec-tests v5.4.0, fixtures_develop.tar.gz
#   devnet = execution-specs tests-glamsterdam-devnet@v8.1.0, fixtures_glamsterdam-devnet.tar.gz
# Each archive is checked against its sha256 before anything is extracted.
#
# With MEGA_EVM_CHECKOUT, the script also re-derives the versions as the CI does, from the fork's
# run-tests.sh in cargo's git checkout of the locked revision (present once the checkout has been
# built), and refuses when they moved: the pins below then need updating, with new sha256s.
#
# Exit status: 0 fetched (or already present and stamped), 1 a check failed, 2 usage.
set -euo pipefail

MAIN_VERSION="v5.4.0"
DEVNET_VERSION="tests-glamsterdam-devnet%40v8.1.0"
MAIN_URL="https://github.com/ethereum/execution-spec-tests/releases/download/$MAIN_VERSION/fixtures_develop.tar.gz"
DEVNET_URL="https://github.com/ethereum/execution-specs/releases/download/$DEVNET_VERSION/fixtures_glamsterdam-devnet.tar.gz"
MAIN_SHA256="3e2b02d49fe903eda4fd8caca5cbf0d139c470e97e1de9a85299b1b034f97099"
DEVNET_SHA256="3bdb3783c988d8a2dbc0dd6bb2dc0986b4559829513229a48bcc2840557c2e22"
STAMP="eest-main-$MAIN_VERSION-develop+glamsterdam-devnet-v8.1.0 main-sha256=$MAIN_SHA256 devnet-sha256=$DEVNET_SHA256"

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 DEST [MEGA_EVM_CHECKOUT]" >&2
  exit 2
fi
dest=$1
checkout=${2:-}

sha256() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

if [ -n "$checkout" ]; then
  # The CI's derivation: the one revm fork revision in Cargo.lock, then its run-tests.sh.
  sources=$(grep -oE 'git\+https://github.com/megaeth-labs/revm\?(tag|rev)=[^"#]*#[0-9a-f]*' "$checkout/Cargo.lock" | sort -u)
  if [ "$(printf '%s\n' "$sources" | grep -c .)" -ne 1 ]; then
    echo "expected exactly one revm fork revision in $checkout/Cargo.lock, found: $sources" >&2
    exit 1
  fi
  commit=${sources##*#}
  script=$(ls -d "${CARGO_HOME:-$HOME/.cargo}"/git/checkouts/revm-*/"${commit:0:7}"/scripts/run-tests.sh 2>/dev/null | head -1 || true)
  if [ -z "$script" ]; then
    echo "note: no cargo checkout of revm ${commit:0:7} yet (build the checkout once); versions not re-derived" >&2
  else
    read_var() { sed -n "s/^$1=\"\(.*\)\"$/\1/p" "$script"; }
    if [ "$(read_var MAIN_VERSION)" != "$MAIN_VERSION" ] || [ "$(read_var DEVNET_VERSION)" != "$DEVNET_VERSION" ] ||
       ! grep -q '^    MAIN_TAR="fixtures_develop.tar.gz"$' "$script"; then
      echo "the revm fork ${commit:0:7} names other fixtures than this script pins:" >&2
      grep -nE '^(MAIN_VERSION|DEVNET_VERSION)=|MAIN_TAR=' "$script" >&2
      exit 1
    fi
    echo "pins match the revm fork ${commit:0:7}'s run-tests.sh"
  fi
fi

if [ -f "$dest/FIXTURES.txt" ] && [ "$(head -1 "$dest/FIXTURES.txt")" = "$STAMP" ] &&
   [ -d "$dest/main/state_tests" ] && [ -d "$dest/main/blockchain_tests" ] && [ -d "$dest/devnet/state_tests" ]; then
  echo "fixtures already present and stamped in $dest"
  exit 0
fi

work="$dest.partial"
rm -rf "$work"
mkdir -p "$work/main" "$work/devnet"
# fetch NAME URL SHA256 DIRS...: the directories, out of the archive at URL, under $work/NAME. The
# archive's top-level `fixtures/` directory is stripped, as the CI and the fork's runner strip it.
fetch() {
  local name=$1 url=$2 want=$3 got
  shift 3
  local members=()
  for dir in "$@"; do members+=("fixtures/$dir"); done
  echo "downloading $url"
  curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors "$url" -o "$work/$name.tar.gz"
  got=$(sha256 "$work/$name.tar.gz")
  if [ "$got" != "$want" ]; then
    echo "$name: sha256 $got, expected $want: refusing to extract" >&2
    exit 1
  fi
  tar -xzf "$work/$name.tar.gz" --strip-components=1 -C "$work/$name" "${members[@]}"
  rm "$work/$name.tar.gz"
}
fetch main "$MAIN_URL" "$MAIN_SHA256" state_tests blockchain_tests
fetch devnet "$DEVNET_URL" "$DEVNET_SHA256" state_tests
printf '%s\n' "$STAMP" >"$work/FIXTURES.txt"
rm -rf "$dest"
mv "$work" "$dest"
echo "fixtures in $dest: $(find "$dest/main/state_tests" -name '*.json' | wc -l | tr -d ' ') main state tests," \
  "$(find "$dest/main/blockchain_tests" -name '*.json' | wc -l | tr -d ' ') main blockchain tests," \
  "$(find "$dest/devnet/state_tests" -name '*.json' | wc -l | tr -d ' ') devnet state tests"
