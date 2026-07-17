# Campaign operator: the autonomous lifecycle loop

One sentence starts it: **"Operate `targets/<spec>.json` per campaign-operator.md."**
From that point the operator (you, an agent) runs the whole lifecycle; the human
holds exactly three things: the ignition budget, upstream PR merges, and the
escalations listed below.

## Steady-state shipping interface (`aro pipeline`)

Once the human has approved the ignition budget, the **default path is literally
one sentence** — no manifest arg, no PR bookkeeping by hand:

```sh
# 1) Stage-0 bootstrap (settle ledger → re-pin baseline → seed → auto out-dir)
#    then sweep → certify → gate → package:
python3 -m aro pipeline targets/<spec>.json
#    → exit 2: supplement work order (touched paths, pr-discipline, resume cmd)

# 2) Operator: dual-green tests on the packaged branch (+ optional cargo fmt)

# 3) Resume through conformance → open (pass the auto-named out-dir):
python3 -m aro pipeline targets/<spec>.json --manifest .aro-runs/<spec>-auto-<YYYYMMDD> --continue
#    → exit 0: PR URL (also appends the ship ledger so the next bootstrap can settle it)
```

Pipeline stages: **bootstrap** → sweep → certify → gate → package ──(work order)──►
conformance → open. Durable checkpoints in `<out_dir>/pipeline-state.json`.
Bootstrap is skipped when `--manifest` is given (T44 path for an existing run).
Granular tools (`aro certify`, `aro ship gate|package|conformance|open|watch --all`,
`aro sweep --attempt --seeds`) remain available as re-entry / debug. Details:
`run-to-pr.md` (top-level flow) and `docs/OPERATIONS.md` §13.10–§13.11.

## Division of labor

| layer | who | does |
|---|---|---|
| pipeline | `python3 -m aro pipeline <spec>` | checkpointed campaign → certify → package (and open after work order) |
| operator | you | executes work orders; dual-green / judgment the machine cannot; loops or re-enters stages |
| human | the user | approves ignition budget (once per campaign), merges upstream PRs, answers escalations |

## Operator loop (without pipeline)

When debugging or re-entering mid-campaign, the granular ladder is:

1. `python3 -m aro recheck staleness <spec>` — baseline still valid?
2. `python3 -m aro recheck debts <spec>` — open measurement debt under Ir?
3. `python3 -m aro sweep <spec> --attempt …` — campaign (or resume out-dir)
4. `python3 -m aro manifest <out-dir> --spec <spec>` — harvest accepted set
5. `python3 -m aro recheck candidates --spec <spec> --out <out-dir> --apply`
6. `python3 -m aro terminal …` / `aro certify` — stamp mergeability
7. `python3 -m aro ship package|conformance|open|watch …` — PR path

Escalate to a human on: baseline re-pin required, second consecutive author-error,
merge-gate conflicts across workload lanes, or harness-unreachable dark regions
after two campaigns. Never delete registered worktrees while a campaign might be
live on this host.

## Budget and stop

Ignition budget is a human decision (max attempts / wall clock / LLM spend).
Stop when the frontier is exhausted, debts are probe-capped, or the human says so.
Record outcomes in the ship ledger so the next `aro pipeline` bootstrap can settle
open PRs (`aro ship watch --all`).
