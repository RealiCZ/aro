# ARO server operations manual

Put ARO on a machine and let it run performance optimization unattended. This manual
covers the current **runnable version** (the per-function sweep: profile hotspots,
optimize function by function, compound the wins). It does not cover the unimplemented
"whole-project explore mode".

---

## 0. Platform prerequisites

- **macOS or Linux both work.** The profiler is cross-platform (`_raw_samples` in
  `aro/profile.py`):
  - **macOS**: built-in `/usr/bin/sample`, no sudo, works out of the box.
  - **Linux**: uses **`perf`**. perf must be installed (`linux-tools` / `perf` package), and
    `kernel.perf_event_paranoid <= 1` (`sudo sysctl kernel.perf_event_paranoid=1`), or run as
    root / with CAP_PERFMON. If sampling fails (not installed / no permission), no frontier
    comes out; see section 11.
- PNG output (SVG to image) is best-effort on both platforms: macOS uses `qlmanage`, Linux uses
  `rsvg-convert` / `cairosvg` / `inkscape`. If none is present nothing breaks:
  `decision-tree.html` / `*.svg` still come out, only the `*.png` files are missing (the HTML
  embeds the SVG, so no figures are lost).
- Python needs **zero pip dependencies** (pure stdlib, 3.9+). No venv, no `pip install`.
- The profiler samples a RUNNING probe: it launches the probe with `argv[1] = <seconds>` and
  expects it to spin the same workload until the deadline (spin mode; see
  `skill/references/harness-protocol.md`). As a fallback it retries fixed-iteration probes at
  a high `ARO_BENCH_SCALE`, but that is best-effort; implement spin mode in every probe.

## 1. Dependency checklist

| Need | Purpose | Check |
|---|---|---|
| macOS `/usr/bin/sample` **or** Linux `perf` | sample hot frames | `ls /usr/bin/sample` or `perf --version` |
| Python 3.9+ | ARO itself | `python3 --version` |
| Rust + cargo | build / test / bench the target repo | `cargo --version` |
| git | worktree isolation | `git --version` |
| `claude` CLI (**logged in**) | generate candidates + semantic review | `claude -p "ok" --output-format json` |
| `rustfilt` or `c++filt` (recommended) | real Rust symbol demangling; without either, a heuristic fallback can mislabel monomorphized hot frames and hide levers from the frontier | `which rustfilt c++filt` |
| PNG on Linux (optional) | any of `rsvg-convert`/`cairosvg`/`inkscape` | `which rsvg-convert` |

`claude` must be **fully authenticated** on this machine (log in with `claude`, or set up
`ANTHROPIC_API_KEY`). Verify:
```bash
claude -p "reply with: OK" --output-format json   # should return JSON with result=OK
```

## 2. One-time setup

```bash
# 1) get ARO and the target repo (two independent git repos)
git clone <aro-repo>            ~/aro-py
git clone <target-repo>        ~/work/mega-evm     # the Rust repo you want to optimize

# 2) the target repo must build on its own first (ARO will build/test/bench in its worktrees)
cd ~/work/mega-evm && cargo build --release && cd -
```

## 3. Writing a spec (target JSON)

A spec describes what to optimize, how to measure it, how to verify it, what may be edited,
and how long to run. See the working example `targets/mega-evm-r3.json`:

```jsonc
{
  "name": "mega-evm-r3",
  "target_repo":  { "path": "/abs/path/mega-evm", "baseline_ref": "<commit-sha>" },
  "hot_path":     { "file": "crates/.../host.rs", "fn": "inspect_storage" },
  "metric":       "ns_per_call", "direction": "minimize",
  "benchmark_probe": { "pkg": "mega-evm", "probe": "probes/evm_r3.rs",
                       "example": "evm_r3", "sample_prefix": "BENCH",
                       "profile": { "spin_secs": 8, "sample_secs": 4 } },
  "correctness_oracle": {
    "build": ["cargo","build","--release","-p","mega-evm"],
    "test":  ["cargo","test","--release","-p","mega-evm","--lib"],
    "differential": { "pkg":"mega-evm", "probe":"probes/evm_r3_diff.rs",
                      "example":"evm_r3_diff", "prefix":"DIFF" }   // the byte-identical judge; strongly recommended
  },
  "constraints": { "editable": ["crates/.../host.rs"], "no_new_deps": true, "byte_identical": true },
  "run": { "generator": "agentic", "stop": {"max_rounds":1,"dry_rounds":1},
           "aa_runs": 2, "ab_pairs": 8, "timeout": 1800, "bench_scales": [1,8,64] }
}
```

Key points:
- `baseline_ref` pins a **commit sha**. ARO cuts an isolated worktree from it, so you can keep
  working in the main checkout without affecting the run.
- The `differential` probe is the byte-identical oracle. **Without it, the significance judge
  rejects every candidate** (unless `constraints.weak_oracle=true`, which downgrades to the test
  suite only and is no longer byte-identical).
- `bench_scales` feeds auto-tighten: when a result is noise-limited, ARO scales up the batch and
  re-measures automatically.

## 4. First run a map-only sanity check (no code changes, no cost)

```bash
cd ~/aro-py
python3 -m aro sweep targets/mega-evm-r3.json --min-pct 1.5
```
This profiles and draws a frontier map (which functions are hot, which are our leverage, which
are untouchable). **Confirm the map has content** (a profile was parsed) before starting the real
run. An empty map usually means the probe cannot spin, or the symbols were stripped.

## 5. The real unattended run (changes code, costs money)

```bash
python3 -m aro sweep targets/mega-evm-r3.json --attempt --diverge --critic \
    --max-attempts 8 --rounds-per-fn 2 --fanout 2 --out-dir ./.aro-runs/megaevm-prod
```

Common knobs:

| Knob | Default (--diverge) | What it does |
|---|---|---|
| `--attempt` | off | enables the L3 unattended loop (otherwise you only get the map) |
| `--diverge` | off | infinite exploration: walk the whole frontier, refill and retry, no early stop on dry |
| `--critic` | off | enables the second judge (semantic review; blocks reward hacking and benchmark gaming); **recommended** |
| `--max-attempts N` | 10000 | **the cost throttle**: at most N function attempts. Controls cost/time linearly |
| `--rounds-per-fn N` | 4 | rounds per function |
| `--fanout N` | 3 | parallel candidates per round (>1 auto-enables prescreen) |
| `--gen-concurrency N` | 8 | cap on parallel `claude` generation (the judge stays serial: that is the moat) |
| `--dry-rounds N` | 3 | rounds without an accept before a function counts as exhausted |
| `--out-dir DIR` | `.aro-runs/<name>-diverge` | artifact directory |

**Cost/time**: token-heavy (the read stage, generation, and review all burn tokens). A repo like
mega-evm runs at roughly $8 to $10 per hour. `--max-attempts` is the main throttle: a medium
setting (8 / 2 / 2) measured about 6 to 7 hours, about $69, 4 accepts. Start small, then scale up.

## 6. Keep it alive across SSH disconnects

A run takes hours. Do not let it die when SSH drops. Pick one of three:

```bash
# tmux (recommended; you can scroll back)
tmux new -s aro
python3 -m aro sweep targets/mega-evm-r3.json --attempt --diverge --critic --out-dir ./.aro-runs/prod
# Ctrl-b d to detach; tmux attach -t aro to come back

# or nohup
nohup python3 -m aro sweep ... > ./.aro-runs/prod.log 2>&1 &

# or a launchd plist (start on boot / daemonize; write your own as needed)
```

## 6.5 Serve the report on port 8010 (view it remotely while the run is live)

The run executes on the server, so `decision-tree.html` sits on a remote disk your local browser
cannot open. `aro serve` serves the `--out-dir` over **pure stdlib HTTP**, port **8010** by
default, and **re-renders the HTML from events.jsonl every 30 s**. So while the run is still
going, refreshing the page shows the latest progress (no need to wait for the run to finish).

```bash
# open a second tmux window (the run stays in the other one), pointed at the same --out-dir
tmux new -s aro-web
python3 -m aro serve ./.aro-runs/prod --port 8010
#   -> http://127.0.0.1:8010/   the root path serves decision-tree.html, re-rendered every 30 s
# Ctrl-b d to detach
```

Common knobs: `--port 8010` changes the port. `--every 30` changes the re-render interval in
seconds. `--no-watch` serves statically (no auto re-render). `--host` sets the bind address; the
default is `127.0.0.1` (local only, right for an SSH tunnel).

> Warning: by default the server binds `127.0.0.1`, so nothing is exposed. Passing
> `--host 0.0.0.0` explicitly makes it **network-reachable with no authentication** and exposes
> this run directory. Two safe setups:
> - **SSH tunnel (recommended)**: keep the default `--host 127.0.0.1` on the server, run
>   `ssh -L 8010:127.0.0.1:8010 user@server` locally, then open `http://localhost:8010`.
> - Or, if you do pass `--host 0.0.0.0`, make sure **port 8010 is open only to your IP**
>   (security group / firewall allowlist). Never leave it open to the public internet.

`aro serve` does not re-optimize, does not call `claude`, and costs nothing. It only reads
events.jsonl, re-renders the HTML, and serves it with http.server.

## 7. Artifacts (all under `--out-dir`)

| File | What it is |
|---|---|
| **`events.jsonl`** | **Ground truth**, the event stream, one event per line. When anything disagrees, this wins |
| `decision-tree.html` | The exhaustion ledger report (self-contained single HTML, template `aro/ledger_template.html`, no build step): attempt tree with per-candidate dossier, plus the "speedup vs cumulative tokens" chart at the bottom. Written automatically as the run goes |
| `perf-token.svg` / `.png` | that trajectory chart as standalone files |
| `REPORT.md` | the text report (realized / headroom / floor / verdicts), refreshed live during the run |
| `trajectory.svg` / `.png` | realized vs headroom line chart |
| `a<N>/records.jsonl`, `a<N>/patches/` | per-attempt candidate records and patches |

Reading the report: 1) **remotely on the server**: `python3 -m aro serve <out-dir> --port 8010`,
then open port 8010 in a browser (see section 6.5; it refreshes while the run is live);
2) or copy `decision-tree.html` to your local machine and open it in a browser (self-contained
single file, works offline).

To re-render a fresh report for any **old run**: `python3 -m aro tree <out-dir>` (reads only
events.jsonl; does not re-optimize, costs nothing).

## 8. Watching a live run

```bash
tail -f ./.aro-runs/prod/events.jsonl          # event by event
watch -n5 'tail -20 ./.aro-runs/prod/REPORT.md' # report refreshes live
# key signals: attempt_started/finished, baseline_advanced (accept),
#              gate apply status=fail (drift/sibling), critic verdict=reject (blocked a fake win)
python3 - <<'PY'
import json
e=[json.loads(l) for l in open(".aro-runs/prod/events.jsonl") if l.strip()]
af=[x for x in e if x.get("event")=="attempt_finished"]
print("accepts:", sum(1 for x in af if x.get("accepted")), "/", len(af),
      "| tok:", sum(x.get("tokens") or 0 for x in e),
      "| $:", round(sum(x.get("cost_usd") or 0 for x in e),2))
PY
```

## 9. Stopping and cleanup

```bash
pkill -f "aro sweep.*<out-dir-name>"     # stop the orchestrator
# ARO's isolated worktrees / target-dirs (a mid-run kill can leave leftovers):
git -C <target-repo> worktree list        # check for leftovers under .aro-worktrees
for w in $(git -C <target-repo> worktree list --porcelain | awk '/^worktree/{print $2}' | grep .aro-worktrees); do
  git -C <target-repo> worktree remove --force "$w"; done
git -C <target-repo> worktree prune
rm -rf <target-repo-parent-dir>/.aro-worktrees/* <target-repo-parent-dir>/.aro-*-td   # target-dirs eat a lot of disk
```
> Warning, do not over-delete: `.aro-worktrees/*` and `.aro-<name>-td` are ARO temporaries.
> Your own `cz/*` worktrees in `git worktree list` are not; leave them alone.

## 10. Compounding / resuming

Point `--out-dir` at the same directory and run again: it **resumes from the accepted advanced
baseline**, and wins compound across runs. To start from scratch, use a fresh empty `--out-dir`
(`aro run` also has `--ignore-resume-failure` to deliberately start over; `aro sweep` does not
take that flag).

## 11. Troubleshooting

New machine, or a collapsed frontier map (empty / one bogus giant function / top
functions skipped as `source not located`)? Work through
`skill/references/new-box-checklist.md` first: it has the full preflight checklist
and the three-layer diagnostic ladder (sampling → naming → locating).

| Symptom | Likely cause / fix |
|---|---|
| Empty map / "no profile parsed" | **Linux**: usually `perf` not installed or `perf_event_paranoid > 2`; run `sudo sysctl kernel.perf_event_paranoid=2`. **macOS**: `/usr/bin/sample` should be present. Both: do not strip release symbols (ARO already forces `CARGO_PROFILE_RELEASE_DEBUG=2` / `CARGO_PROFILE_RELEASE_STRIP=none`), and install `rustfilt` or `c++filt` for real demangling. Then check whether the probe example runs standalone with `cargo run`. Full ladder: `skill/references/new-box-checklist.md` |
| Every candidate gets `verify-failed: no differential oracle` | The spec is missing the `differential` probe. Add it, or set `constraints.weak_oracle=true` (a downgrade; the judge marks it) |
| `apply failed: search text not found` | Drift / same-round sibling conflict; benign (anchor fixing plus end-of-round folding already handle it). Dig deeper only if it is a genuinely new pattern |
| `claude` hangs / errors | Auth expired; log `claude` in again. The read stage has a 600 s timeout as a backstop |
| Disk full | Each worktree gets its own target-dir (independent compilation is required for correctness). Clean up `.aro-*-td`, or lower `--gen-concurrency` |
| cargo/claude processes that will not exit | Leftovers from a mid-run kill; deleting the matching `.aro-worktrees` subdirectory makes them exit |

## 12. Current capability boundaries (honest)

- Optimization scope is the **profile-driven hot frontier** (our functions that are hot at
  >= min_pct and can be located to an `fn` in source). It does **not scan the whole codebase**.
- It needs the spec-fed **bench + differential probes**. Code without an oracle cannot be handled
  today; that is the unimplemented "whole-project explore" tier.
- The judge is the moat: reward-hack guard + byte-identical differential + A/A floor + paired A/B
  + bootstrap CI + auto-tighten, plus the second semantic review (`--critic`). `accepted` means
  correctness and speedup are proven; it does **not** mean "merge it". Merging is a human call
  (the manifest marks a win mergeable only when it is byte-identical and passed the critic).
  On targets that declare `terminal_bench_targets`, mergeable further requires the criterion-Ir
  terminal gate `TERMINAL_CONFIRMED` (see section 13).

## 13. Instruction-count gate (operator runbook)

CPU-bound candidates are judged primarily by deterministic instruction counts (callgrind Ir),
not wall-clock. Inner loop: probe-level Ir (Gate 1.5). Pre-PR: criterion row-level Ir via
`mega-bench-reporter measure` (terminal gate). Wall-clock remains only for locality/memory
claims. Full protocol: `skill/references/run-to-pr.md` §1b / §6b.

### 13.1 Prerequisites (host tooling)

1. **Valgrind / CodSpeed toolchain** — follow the mega-bench-reporter provisioning runbook
   `skills/provision-instructions-lane` in that repo (installs valgrind, codspeed CLI pins,
   and the instructions-lane preflight). Do this once per host; ARO does not provision it.
2. **Reporter binary on this host** — ARO shells out to it for the terminal gate only:
   ```bash
   git clone <mega-bench-reporter-repo>  ~/workspace/mega-bench-reporter
   cd ~/workspace/mega-bench-reporter && cargo build --release
   # binary: target/release/mega-bench-reporter
   ```
3. Point ARO at that binary: target JSON `measure_bin`, **or** env `ARO_MEASURE_BIN`
   (**env wins**). Sanity-check without measuring:
   ```bash
   python3 -m aro terminal targets/mega-evm-v2.json --list
   # prints terminal_bench_targets / measure_bin / ε — no binary required for --list
   ```
Inner-loop probe Ir uses bare `valgrind --tool=callgrind` on the probe binary (no reporter).
Terminal gate needs the reporter binary. macOS hosts without valgrind can still list config
and run wall-clock-only paths; Ir measure steps fail hard until tooling is present.

### 13.2 Config knobs (target JSON + env)

Live example: `targets/mega-evm-v2.json`. All fields are optional for backward compatibility;
terminal gate is **off** until `terminal_bench_targets` is non-empty.

| Knob | Where | Default / notes |
|---|---|---|
| `measure_bin` | target JSON | path to `mega-bench-reporter`; overridden by `ARO_MEASURE_BIN` |
| `ARO_MEASURE_BIN` | env | **wins** over JSON when set and non-empty |
| `terminal_bench_targets` | target JSON | list of criterion bench targets, e.g. `["mega_bench"]`. Empty → terminal gate disabled |
| `terminal_bench_filter` | target JSON | optional criterion filter string passed through to `measure` |
| `terminal_timeout_secs` | target JSON | seconds per `measure` invocation; default `4 × run.timeout` |
| `terminal_measure_rounds` | target JSON | measure each side this many times; median Ir per row (default `3`) |
| `ARO_TERMINAL_ROUNDS` | env | **wins** over `terminal_measure_rounds` when set |
| `terminal_default_floor_pct` | target JSON | per-row floor when no calibrated entry (default `1.0`) |
| `icount_epsilon_pct` | target JSON | probe-level Ir ε in percent; default `0.1` (also the floor clamp minimum) |
| `ARO_ICOUNT_EPSILON` | env | **wins** over `icount_epsilon_pct` when set |
| `probe_covers` | target JSON | path prefixes the probe is known to exercise (e.g. `["crates/mega-evm/src"]`). Patch with no overlap → `NO_COVERAGE`. Absent → warn and proceed |

```bash
# Inspect resolved terminal config (safe anywhere; no target checkout, no measure binary)
python3 -m aro terminal targets/mega-evm-v2.json --list   # --dry-run is an alias
```

### 13.3 Noise model, floors, and first-run acceptance

**Scaling law (server-measured facts).** Run-to-run Ir noise is **not** bit-for-bit zero on
criterion rows. The noise source is per-process hasher seeding (entropy includes address + time
terms; an `LD_PRELOAD` getrandom shim has no effect). Each criterion bench binary is its own
process → a fresh seed per row per run. Magnitude scales **inversely** with measured-region
size:

| Scope | Typical run-to-run | Notes |
|---|---|---|
| Whole-probe aggregates | ~0.004% | probe Gate 1.5 ε=0.1% has ~25× margin — leave it alone |
| Criterion single-iteration rows | 0.01–1% | 127/159 rows drifted across two runs of identical binaries; worst observed ~0.94% |
| Rebuild contribution | ~0.004% | 3 full rebuilds of identical source → probe spread 0.0041%. Negligible vs row noise |

**Consequence:** floors can be calibrated by **repeated measure of one checkout** — no rebuilds.
Do **not** require re-runs to match bit-for-bit, and do **not** tighten probe ε to 0 on that
basis. The terminal gate absorbs row noise via (a) median-of-N sampling per side and (b)
per-row floors.

#### Terminal calibration (`aro terminal-calibrate`)

Run after provisioning a host, after tool upgrades (`rustc` / reporter), and periodically
(floors older than 30 days warn; rustc mismatch warns — neither blocks the gate):

```bash
# Same measure invocation the terminal gate uses; N rounds on ONE checkout (default 4).
python3 -m aro terminal-calibrate targets/mega-evm-v2.json \
  --checkout /path/to/baseline-worktree \
  --rounds 4

# Safe anywhere: prints the measure command + destination, never invokes the binary.
python3 -m aro terminal-calibrate targets/mega-evm-v2.json \
  --checkout /path/to/wt --dry-run
```

Per row: `floor_pct = max_pairwise|Δ%| across the N results × 2.0`, clamped to a minimum of
`icount_epsilon_pct` (0.1). Written to **`memory/floors/<spec>.json`** (versioned institutional
memory — commit it):

```json
{"meta": {"calibrated_at": "<ISO>", "rounds": 4, "checkout_describe": "...",
          "measure_bin": "...", "rustc": "rustc …"},
 "floors": {"<row_key>": <floor_pct>, ...}}
```

**Before the first calibration**, terminal verdicts use `terminal_default_floor_pct` (default
**1.0%**) for every row and emit one stderr warning with the uncalibrated row count. A missing
floors file does not block the gate.

Gate classification (unchanged verdict names): improved iff Δ% < −floor(row); regressed iff
Δ% > +floor(row); else untouched. Each side is measured `terminal_measure_rounds` times
(default 3; `ARO_TERMINAL_ROUNDS` wins); Δ is computed from **per-row median** Ir.

#### First-run acceptance checklist

Run once when bringing the gate up on a new host (distilled from plan §9). Do not start a
production campaign until these pass.

1. **Replay two refuted historical patches** (#326 SLOAD hoist, #332 saturating_sub → bare sub).
   Expect Gate 1.5 or the terminal gate to return **NEUTRAL / TERMINAL_UNTOUCHED** (or
   `refuted-by-icount` in the ledger). **No PR** — that is the pass condition.
2. **One synthetic true-positive**: insert a redundant loop (or reverse a known win). Expect a
   non-zero Ir Δ with the constructed sign that clears the row floor (terminal) or probe ε
   (Gate 1.5).
3. **Floor calibration**: run `aro terminal-calibrate` on a quiet host against the baseline
   checkout; commit `memory/floors/<spec>.json`. Probe-level `icount_epsilon_pct` stays at
   `0.1` (25× margin over whole-probe noise) — do not tighten to 0 from row-level drift.
4. **Normal campaign**: only after 1–3. First real perf PR body must quote criterion row-level
   Ir from `bench_ir_rows` (median-of-N); CodSpeed CI must agree in direction (see run-to-pr §6b).

### 13.4 `recheck-debts` (historical open debts)

Cheap Ir re-adjudication of permtree open debts (noise-limited / no-attempt / no-candidate / …).
Each debt with a recoverable patch gets one Ir A/B; results write back through the **normal**
`memory/permtree/<spec>.jsonl` and `memory/lessons.jsonl` paths.

```bash
# Safe anywhere: lists open debts + whether a patch is recoverable. Does NOT construct
# SpecTarget, does not need the target checkout, measures nothing.
python3 -m aro recheck-debts targets/mega-evm-v2.json --list-only

# Full mode (server host): needs (a) target checkout reachable at target_repo.path,
# (b) the original .aro-runs/<run>/aN dirs on THIS host (events pointers resolve locally).
# Optional: --runs-root <dir> if relative .aro-runs paths should resolve under a root.
python3 -m aro recheck-debts targets/mega-evm-v2.json
python3 -m aro recheck-debts targets/mega-evm-v2.json --dry-run   # measure, no ledger write
```

Outcomes: `rechecked` (ledger updated — often `refuted-by-icount` or `accepted-ir`),
`regenerate` (no stored patch under the events pointer — operator must re-generate, not invent
a closed verdict), or `error` (worktree / evaluate failure).

### 13.5 Where verdicts land; config-drift hard errors

| Signal | Lands in | Notes |
|---|---|---|
| Probe Ir (Gate 1.5) | `memory/lessons.jsonl`, `memory/permtree/<spec>.jsonl` | fields `ir_delta_pct`, `profile_fingerprint` on the record when measured |
| Terminal gate | `.aro-runs/<RUN>/terminal.json`, stamped onto `manifest.json` | `verdict`, `bench_ir_rows`, `profile_fingerprint`; `--record` also appends lessons/permtree |
| Historical recheck | same permtree + lessons ledgers | `run_id=recheck-debts`; `refuted-by-icount` closes the debt (last-record-wins) |

`profile_fingerprint` = `rustc -V` + hash of effective `[profile.release]` / `[profile.bench]`.
Use it to attribute "same opt, two conclusions" to config drift.

**Hard errors are not verdicts** — fix the environment and re-run; never force a PR:

| Hard error | Meaning | Operator action |
|---|---|---|
| `profile_fingerprint` mismatch (baseline ≠ candidate) | config drift: rustc or Cargo profile differs across worktrees | align toolchains / profiles; never open a PR on a mixed pair |
| empty / missing `meta.profile_fingerprint` from measure | reporter or env is incomplete | upgrade reporter; re-provision instructions lane |
| row-set mismatch (bench keys differ across sides) | different criterion bench set on the two checkouts | rebuild both sides with the same `terminal_bench_targets` / filter |
| measure binary unset | neither `ARO_MEASURE_BIN` nor `measure_bin` | set one (env wins) and re-run `--list` to confirm |

Terminal verdicts that **are** outcomes (and may block a PR without being "errors"):
`TERMINAL_CONFIRMED` (open PR), `TERMINAL_UNTOUCHED` / `TERMINAL_REGRESSED` / `TERMINAL_MIXED`
(no PR; operator decision on the last two). See `python3 -m aro terminal --help` and
`skill/references/run-to-pr.md` §1b.
