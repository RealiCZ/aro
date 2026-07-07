# Campaign operator: the autonomous lifecycle loop

One sentence starts it: **"Operate `targets/<spec>.json` per campaign-operator.md."**
From that point the operator (you, an agent) runs the whole lifecycle; the human
holds exactly three things: the ignition budget, upstream PR merges, and the
escalations listed below.

## Division of labor

| layer | who | does |
|---|---|---|
| oracle | `python3 -m aro next <spec>` | deterministic: reads all recorded state (ledger, campaign state, manifest, recheck, coverage, merge gate), prints THE next action + why. Never executes anything. |
| operator | you | executes the action; supplies the judgment the oracle cannot (L1 health, PR content, conflict calls); loops. |
| human | the user | approves ignition budget (once per campaign), merges upstream PRs, answers escalations. Nothing else. |

## The state machine

`aro next` evaluates these guards top to bottom; the first true one is the
action. Every guard names the state change that advances past it, so the
machine provably terminates at WATCH (each transition either consumes its own
trigger or escalates):

```
guard (first true wins)                  action             what advances past it
─────────────────────────────            ─────────────      ─────────────────────────────────────
no ledger AND no campaign state       →  IGNITE-FIRST       campaign writes ledger + state
baseline unresolvable / not ancestor  →  RE-PIN         ✋   human re-pins the spec
state has out-dir, manifest unreadable→  REBUILD-MANIFEST   `aro manifest <out-dir>` writes it
manifest has accepts, not harvested   →  HARVEST            operator decides PRs, `--mark harvested`
churn under the editable regions      →  RE-RUN             re-pin + re-derive DIFF + L1 + ignite
open debts ≠ last run's debt set      →  PAY-DEBTS          campaign updates ledger + debt set
state = author-error(N)               →  RETRY-FACTORY  ✋²  campaign closes dry / exhausted
coverage report missing OR stale      →  COVERAGE           `aro coverage` writes a fresh report
fresh dark fns > 0                    →  LIGHT-DARK     ✋³  new workload lights it, dark shrinks
(nothing left)                        →  WATCH              upstream merge flips recheck → RE-RUN
```

Escalations (✋): RE-PIN always; RETRY-FACTORY on the second consecutive
occurrence (✋²); LIGHT-DARK when the same dark fns survive two campaigns (✋³,
harness-unreachable — an operator judgment, see below). Warn lines ride on
EVERY action: merge-gate conflicts, the probe-capped debt floor (a debt set the
last campaign failed to move), and recheck-blind (target repo unreachable).

The ladder's reasoning and anti-loop rules are documented in `aro/next.py`'s
module docstring; do not re-derive or reorder them.

## The loop

1. `python3 -m aro next <spec>` (add `--json` when you want fields, not prose).
2. Execute the printed command, honoring the judgment points below.
3. Repeat. Stop at `watch` (report + schedule a recheck cadence) or at an
   escalation.

Never skip ahead of the oracle ("I know coverage is next") — recorded state can
have changed since you last looked, and the ladder exists so debts and
contradictions are never jumped over.

## Judgment points (yours, not the oracle's)

- **Before ignition** (`ignite-first`, and any re-ignition): run the L1 map and
  judge its health — frontier non-empty, ≥1 in-crate frame, no one-blob
  collapse. Debugging ladder and gotchas: `add-a-target.md`,
  `new-box-checklist.md`. Then confirm the ignition budget with the human
  UNLESS they already gave one for this campaign.
- **Harvest**: follow `evaluate-run.md` (decide first, act second; independent
  analysis — never inherit a pre-digested verdict) and `pr-discipline.md`
  (test gates, merge gate, number provenance). When all PR decisions are made,
  record it: `python3 -m aro next <spec> --mark harvested` — the oracle cannot
  see upstream PRs, so this mark is how it advances.
- **Merge-gate conflicts** (printed as `warn:` lines on every action): resolve
  by re-measuring on the contradicting workload, or disclose verbatim in the PR
  body. Never silently ship the winning lane's number.
- **Persistent debts**: the oracle already refuses to re-ignite over a debt set
  the last campaign failed to move (it degrades to a probe-capped warning).
  Your job is to REPORT that floor honestly, not to argue with it.
- **Persistent dark regions**: dark fns surviving two consecutive
  `light-dark-regions` campaigns are harness-unreachable — record that as the
  honest coverage boundary and let the ladder reach `watch`. The oracle cannot
  compute this one: "the workload cannot reach it" and "the author has not
  found it yet" look identical on disk.

## Escalate to the human (stop the loop, report, wait)

- `re-pin`: baseline rewrite/divergence — re-pinning changes what every future
  number means; the human re-pins (or approves your proposed pin).
- `retry-factory` twice in a row (author infrastructure is broken, not busy).
- A merge-gate conflict you cannot resolve by re-measuring.
- Anything remote: pushes, PRs, comments follow the standing approval rules of
  the box you run on — the loop never widens a permission you were not given.

## What you never do

- Ignite without a budget from the human.
- `aro clean --registered` while any campaign might be live; never bypass the
  ledger-referenced run-dir protection.
- Re-judge numbers by hand — numbers come from manifests/ledgers verbatim
  (pr-discipline "Numbers carry their origin").
- Mark `harvested` before the PR decisions are actually made.
