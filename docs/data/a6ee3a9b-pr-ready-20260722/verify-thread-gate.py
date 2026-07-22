#!/usr/bin/env python3
import json
import re
from pathlib import Path

ARO = Path('/nvme2/mega-engineer/workspace/aro')
ALG = ARO / '.aro-worktrees/a6ee3a9b-backport-certify-20260721'
errors = []
checked = 0
for path in sorted((ARO / 'targets').glob('*.json')):
    data = json.loads(path.read_text())
    if not str(data.get('target_repo', {}).get('path', '')).endswith('/salt'):
        continue
    for entry in data.get('ship_conformance', []):
        cmd = entry.get('cmd', '')
        if not re.search(r'\bcargo(?:\s+\+\S+)?\s+test\b', cmd):
            continue
        checked += 1
        harness = cmd.split(' -- ', 1)[1].split() if ' -- ' in cmd else []
        if '--test-threads=4' not in harness:
            errors.append(f'{path.name}:{entry.get("name")}: unbounded Salt test command: {cmd}')

script = (ALG / 'scripts/validate-salt-path-patch.sh').read_text()
normalized = ' '.join(script.replace('\\\n', ' ').split())
expected = [
    'run_cargo test -p banderwagon -p ipa-multipoint -- --test-threads=4',
    'run_cargo test -- --test-threads=4',
    'run_cargo test --features test-bucket-resize -- --test-threads=4',
    'run_cargo test -p salt --features test-bucket-resize test_e2e_random_stress -- --test-threads=4 --ignored --nocapture',
    'run_cargo test --no-default-features -- --test-threads=4',
    'run_cargo test --no-default-features --features test-bucket-resize -- --test-threads=4',
]
for command in expected:
    if command not in normalized:
        errors.append(f'validate-salt-path-patch.sh: missing bounded command: {command}')
manifest_before_harness = 'cargo "${PATCH_CONFIG[@]}" "$subcommand" --manifest-path "$SALT_WORKTREE/Cargo.toml" "$@"'
if 'local subcommand="$1"' not in script or manifest_before_harness not in script:
    errors.append('validate-salt-path-patch.sh: run_cargo must place --manifest-path after the cargo subcommand and before harness arguments')
if 'SHARED_COMMITTER' not in script or 'megaeth-labs/salt#146' not in script:
    errors.append('validate-salt-path-patch.sh: missing reviewer-facing root-cause/issue rationale')
if checked == 0:
    errors.append('no Salt target conformance test commands found')
if errors:
    raise SystemExit('\n'.join(errors))
print(f'PASS: all {checked} Salt target conformance test commands use --test-threads=4')
print('PASS: all full/quick path-patch Salt test calls use --test-threads=4')
print('PASS: manifest-path remains before harness arguments')
