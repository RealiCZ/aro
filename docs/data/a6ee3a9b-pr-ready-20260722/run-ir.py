#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ARO = Path('/nvme2/mega-engineer/workspace/aro')
ALG = Path('/nvme2/mega-engineer/workspace/algebra')
RUN = ARO / '.aro-runs/a6ee3a9b-pr-ready-20260722/performance/ir'
BASE_SHA = '01b20e377460e7af9da069b0c96f2d1158a7b974'
CAND_SHA = '03ee25353a9ed5655af0a5f8ba4e82982de1189e'
BASE_WT = ARO / '.aro-worktrees/a6ee3a9b-ir-base-20260722'
CAND_WT = ARO / '.aro-worktrees/a6ee3a9b-ir-candidate-20260722'
LANES = [
    ('field', ARO / 'targets/algebra-bandersnatch-field.json'),
    ('msm', ARO / 'targets/algebra-bandersnatch-msm.json'),
]
RUN.mkdir(parents=True, exist_ok=True)

def utc():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

def cmd(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout

def remove_wt(path):
    subprocess.run(['git', '-C', str(ALG), 'worktree', 'remove', '--force', str(path)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

remove_wt(BASE_WT); remove_wt(CAND_WT)
try:
    cmd('git', '-C', str(ALG), 'worktree', 'prune')
    cmd('git', '-C', str(ALG), 'worktree', 'add', '--detach', str(BASE_WT), BASE_SHA)
    cmd('git', '-C', str(ALG), 'worktree', 'add', '--detach', str(CAND_WT), CAND_SHA)
    sys.path.insert(0, str(ARO))
    from aro import spec as specmod
    from aro.target import SpecTarget
    from aro import icount as icmod
    from aro import selfcheck as scmod
    out = {
        'started_at': utc(), 'base_sha': BASE_SHA, 'candidate_sha': CAND_SHA,
        'scale': 1, 'rayon_num_threads': 1, 'lanes': {}
    }
    for lane, spec_path in LANES:
        spec = specmod.load(spec_path)
        target = SpecTarget(spec)
        env_fp = scmod.require_selfcheck(spec)
        # Alternate order across independent lanes while keeping exact A/B values.
        if lane == 'field':
            base = target.icount(BASE_WT, scale=1)
            cand = target.icount(CAND_WT, scale=1)
        else:
            cand = target.icount(CAND_WT, scale=1)
            base = target.icount(BASE_WT, scale=1)
        eps = float(spec.icount_epsilon_pct)
        decision = icmod.judge_ir(base, cand, epsilon_pct=eps, locality=False)
        row = {
            'epsilon_pct': eps,
            'baseline_ir': base.ir,
            'candidate_ir': cand.ir,
            'delta_pct': decision.ir_delta_pct,
            'verdict': decision.verdict.value,
            'notes': list(decision.notes),
            'profile_fingerprint': cand.profile_fingerprint or base.profile_fingerprint,
            'env_fingerprint': env_fp,
            'baseline_events': base.events,
            'candidate_events': cand.events,
        }
        out['lanes'][lane] = row
        print(json.dumps({lane: row}, sort_keys=True), flush=True)
    out['finished_at'] = utc()
    (RUN / 'summary.json').write_text(json.dumps(out, indent=2, sort_keys=True) + '\n')
finally:
    remove_wt(BASE_WT); remove_wt(CAND_WT)
    for p in (BASE_WT, CAND_WT):
        shutil.rmtree(p, ignore_errors=True)
