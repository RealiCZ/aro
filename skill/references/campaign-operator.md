# Campaign operator: the autonomous lifecycle loop

One sentence starts it: **"Operate `targets/<spec>.json` per campaign-operator.md."**
From that point the operator (you, an agent) runs the whole lifecycle; the human
holds exactly three things: the ignition budget, upstream PR merges, and the
escalations listed below.

## Steady-state shipping interface (`aro pipeline`)

Once the campaign is seeded and the human has approved the ignition budget, the
**default path from campaign to opened PR is two commands + one work order**:

```sh
# 1) Sweep-inclusive (or --no-sweep on an existing out-dir) through package:
python3 -m aro pipeline targets/<spec>.json --manifest .aro-runs/<RUN>
#    → exit 2: supplement work order (touched paths, pr-discipline, resume cmd)

# 2) Operator: dual-green tests on the packaged branch (+ optional cargo fmt)

# 3) Resume through conformance → open:
python3 -m aro pipeline targets/<spec>.json --manifest .aro-runs/<RUN> --continue
#    → exit 0: PR URL
```

Pipeline stages: sweep → certify → gate → package ──(work order)──► conformance → open.
Durable checkpoints in `<out_dir>/pipeline-state.json`. Granular tools
(`aro certify`, `aro ship gate|package|conformance|open`, `aro sweep --attempt`)
remain available as re-entry / debug. Details: `run-to-pr.md` (top-level flow) and
`docs/OPERATIONS.md` §13.10. The ladder below (`aro next`) still owns ignition,
debts, coverage, and harvest bookkeeping — pipeline closes the certify→PR segment.

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
running_pid sidecar, process alive    →  WAIT               the run finishes and closes its state
running_pid sidecar, process dead     →  MARK-INTERRUPTED    `--mark interrupted`, then the ladder
                                                              re-evaluates from author-error(...)
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

WAIT and MARK-INTERRUPTED are liveness guards, not lifecycle steps: a running
campaign MERGES a `running_pid` sidecar onto the state file at ignition
(`aro/sweep.py`) — leaving the previous campaign's closure (its `debts_open`
especially) intact — and the closing write drops the sidecar. Liveness ("is a
process alive now") and lifecycle ("where did the last run leave things") are
deliberately separate fields, so a crash can never blank the debt floor. WAIT
means "don't act, a campaign is genuinely mid-run and every other signal below
is mid-write" — do nothing and check again later, never re-ignite or read the
ledger as final. MARK-INTERRUPTED means the running_pid's process is dead but
the run never closed (crash, OOM-kill, box reboot) — run the printed
`--mark interrupted` command before trusting anything else; it clears the
sidecar and sets `author-error(interrupted)`. Because the prior `debts_open`
survived, an unchanged debt set then falls through to RETRY-FACTORY (a changed
one to PAY-DEBTS) — the crash is handled like any other infrastructure failure.

Caveat the operator carries: the liveness probe is a same-host `kill -0` on the
recorded pid. It cannot see pid REUSE — if WAIT persists across many wake-ups on
a run that should have finished, check the `running_since` timestamp in the WAIT
line and force the autopsy with `--mark interrupted` rather than waiting forever.

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

## Running unattended (server loop)

The loop above is designed to be driven by a scheduled agent (e.g. Claude
Code's `/loop` or a cron-triggered routine on the box that owns the target
repo) instead of a human re-invoking it each time — WAIT and
MARK-INTERRUPTED exist specifically so an unattended loop never mis-acts
while a run is mid-flight or after a crash.

**Prompt to hand the loop:**

```
Operate targets/<spec>.json per skill/references/campaign-operator.md.
```

This is the same sentence that starts a human-invoked session — the
operator's job doesn't change, only who re-invokes it.

**Cadence.** Pick the interval from what the ladder is waiting on, not a
fixed number: a `sweep --attempt --diverge` run can take hours, so checking
every 1-2 minutes just burns cache on repeated WAIT reads. 20-30 minutes is
a reasonable default; tighten it only while genuinely watching something
that changes fast (e.g. right after ignition, confirming the run actually
started and isn't dead on arrival).

**What the loop does each wake-up:**
1. Run `python3 -m aro next <spec> --json`.
2. If `action` is `wait`: do nothing, schedule the next wake-up, don't touch
   the repo or the ledger.
3. Otherwise: execute exactly one step of the printed command (following the
   judgment points below), then let the *next* wake-up re-consult the
   oracle rather than chaining multiple ladder steps in one wake-up — this
   keeps every action grounded in freshly-read state.
4. On an escalation (✋, ✋², ✋³, or anything the standing approval rules
   gate — pushes, PRs, comments): stop, report to the human, and hold the
   loop there. Do not retry an escalation on the next wake-up hoping it
   resolves itself; wait for the human.

**What never changes for an unattended loop:** the permission boundary. The
loop only ever has the authority the box was already given — it must not
push, open a PR, or post a comment on its own just because no human is
watching in real time. "No one saw it happen" is not consent.

## Judgment points (yours, not the oracle's)

- **Before ignition** (`ignite-first`, and any re-ignition): run the L1 map and
  judge its health — frontier non-empty, ≥1 in-crate frame, no one-blob
  collapse. Debugging ladder and gotchas: `add-a-target.md`,
  `new-box-checklist.md`. Then confirm the ignition budget with the human
  UNLESS they already gave one for this campaign.
- **Harvest**: follow `evaluate-run.md` (decide first, act second; independent
  analysis — never inherit a pre-digested verdict) and `pr-discipline.md`
  (test gates, merge gate, number provenance). **Preferred path:**
  `python3 -m aro pipeline <spec> --manifest <out-dir> [--no-sweep]` then the
  printed supplement work order, then `--continue` (see top of this file /
  `run-to-pr.md`). Pipeline runs certify end-to-end (decision table + MIXED
  prune; OPERATIONS §13.9–§13.10). Exit 2 mid-chain is a work order (certify
  stop, gate re-cert, or dual-green supplements) — not a free-form question.
  Granular `aro certify` / `recheck candidates` / `terminal` / `ablate` /
  `ship *` remain re-entry tools. When all PR decisions are made, record it:
  `python3 -m aro next <spec> --mark harvested` — the oracle cannot see
  upstream PRs, so this mark is how it advances.
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
