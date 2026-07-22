#!/usr/bin/env python3
import csv
import json
import os
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

run = Path(sys.argv[1])
bin_dir = run / 'bin'
rounds = 5
samples_per_mode_round = 3
surfaces = ['state-update', 'witness', 'msm']
modes = ['baseline', 'candidate']

def utc():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

def mad(xs):
    m = statistics.median(xs)
    return statistics.median(abs(x - m) for x in xs)

def invoke(surface, mode, phase, round_id=0, sample=0):
    env = os.environ.copy()
    env['ARO_BENCH_SCALE'] = '1'
    env['RAYON_NUM_THREADS'] = '16'
    cmd = ['taskset', '-c', '0-15', str(bin_dir / f'{surface}-{mode}')]
    p = subprocess.run(cmd, env=env, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=1800)
    event = {'utc': utc(), 'phase': phase, 'surface': surface, 'mode': mode,
             'round': round_id, 'sample': sample, 'rc': p.returncode,
             'output': p.stdout}
    with (run / 'events.jsonl').open('a') as f:
        f.write(json.dumps(event, separators=(',', ':')) + '\n')
    if p.returncode:
        raise RuntimeError(f'{surface}/{mode} rc={p.returncode}: {p.stdout[-2000:]}')
    line = next((x for x in p.stdout.splitlines() if x.startswith('BENCH ')), None)
    if line is None:
        raise RuntimeError(f'{surface}/{mode}: BENCH line missing')
    return float(line.split()[1])

# Warm every artifact exactly once, outside recorded data.
for surface in surfaces:
    for mode in modes:
        invoke(surface, mode, 'warmup')

rows = []
# Strict A/B alternation: for each sample in every round, baseline then candidate.
for surface in surfaces:
    for r in range(1, rounds + 1):
        for sample in range(1, samples_per_mode_round + 1):
            for order, mode in enumerate(modes):
                ns = invoke(surface, mode, 'recorded', r, sample)
                rows.append({'surface': surface, 'round': r, 'sample': sample,
                             'order': order, 'mode': mode, 'ns_per_call': ns})

with (run / 'raw.csv').open('w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]))
    w.writeheader(); w.writerows(rows)

round_rows = []
summary = {}
for surface in surfaces:
    pair_deltas = []
    round_deltas = []
    by_mode = {m: [] for m in modes}
    for r in range(1, rounds + 1):
        med = {}
        for mode in modes:
            xs = [x['ns_per_call'] for x in rows if x['surface'] == surface and x['round'] == r and x['mode'] == mode]
            med[mode] = statistics.median(xs)
            by_mode[mode].append(med[mode])
            round_rows.append({'surface': surface, 'round': r, 'mode': mode,
                               'median_ns': med[mode], 'mad_ns': mad(xs)})
        round_deltas.append((med['candidate'] / med['baseline'] - 1) * 100)
        for sample in range(1, samples_per_mode_round + 1):
            a = next(x['ns_per_call'] for x in rows if x['surface'] == surface and x['round'] == r and x['sample'] == sample and x['mode'] == 'baseline')
            b = next(x['ns_per_call'] for x in rows if x['surface'] == surface and x['round'] == r and x['sample'] == sample and x['mode'] == 'candidate')
            pair_deltas.append((b / a - 1) * 100)
    summary[surface] = {
        'rounds_per_mode': rounds,
        'samples_per_mode_round': samples_per_mode_round,
        'baseline_median_of_round_medians_ns': statistics.median(by_mode['baseline']),
        'baseline_mad_of_round_medians_ns': mad(by_mode['baseline']),
        'candidate_median_of_round_medians_ns': statistics.median(by_mode['candidate']),
        'candidate_mad_of_round_medians_ns': mad(by_mode['candidate']),
        'round_delta_pct': round_deltas,
        'round_delta_median_pct': statistics.median(round_deltas),
        'round_delta_mad_pct': mad(round_deltas),
        'candidate_faster_rounds': sum(x < 0 for x in round_deltas),
        'adjacent_pair_delta_median_pct': statistics.median(pair_deltas),
        'adjacent_pair_delta_mad_pct': mad(pair_deltas),
        'candidate_faster_adjacent_pairs': sum(x < 0 for x in pair_deltas),
        'adjacent_pairs': len(pair_deltas),
    }
with (run / 'rounds.csv').open('w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(round_rows[0]))
    w.writeheader(); w.writerows(round_rows)
(run / 'summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
print(json.dumps(summary, indent=2, sort_keys=True))
