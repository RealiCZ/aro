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
| Configured LLM CLI(s) (**logged in**) | generate candidates + semantic review | `command -v <cli>` + an authenticated read-only prompt |
| `rustfilt` or `c++filt` (recommended) | real Rust symbol demangling; without either, a heuristic fallback can mislabel monomorphized hot frames and hide levers from the frontier | `which rustfilt c++filt` |
| PNG on Linux (optional) | any of `rsvg-convert`/`cairosvg`/`inkscape` | `which rsvg-convert` |

### Choosing LLM backends

Set the generator with the top-level target-spec field `llm_backend` (`claude`, `codex`, or
`grok`). Selection precedence is `ARO_LLM_BACKEND` > spec `llm_backend` > `claude`. An optional
top-level `critic_backend` explicitly selects the semantic critic, enabling a cross-model
topology such as Codex generation reviewed by Claude; when absent, the critic follows the
generator backend.

| Backend | Read-only calls | Writable calls |
|---|---|---|
| Claude | bare CLI, using its default permissions | `--dangerously-skip-permissions` |
| Codex | `--sandbox read-only` | `--sandbox workspace-write` |
| Grok | `--sandbox aro-read-only` | `--sandbox aro-workspace --always-approve` |

Writable calls, including Claude's dangerous permission bypass, belong only in ARO's writable
throwaway worktrees. Override binary locations with `ARO_CLAUDE_BIN`, `ARO_CODEX_BIN`, and
`ARO_GROK_BIN`.

On hosts whose kernel blocks Codex's bubblewrap sandbox (e.g. Ubuntu 24.04 with
`kernel.apparmor_restrict_unprivileged_userns=1` and no root to change it), every writable Codex
call fails with `bwrap: … Operation not permitted` before the agent can edit anything. Set
`ARO_CODEX_SANDBOX=danger-full-access` to run writable calls unsandboxed — the same trust level
as Claude's writable tier, which never had a kernel sandbox. Read-only calls (the critic path)
always keep `--sandbox read-only` regardless of this variable, an invalid value fails loudly,
and `aro sweep` prints a `sandbox=…` warning banner whenever a non-default mode is active.

Every CLI selected for generation or criticism must be installed and authenticated
on the host before an unattended run. Grok's approval flag enables headless edit/build calls but
does not widen its OS sandbox. Its built-in profiles may warn and continue without kernel
enforcement, so ARO deliberately selects fail-closed custom profiles. Add them to
`~/.grok/sandbox.toml` during host provisioning:

```toml
[profiles.aro-read-only]
extends = "read-only"

[profiles.aro-workspace]
extends = "workspace"
```

Grok refuses to start if either custom profile is missing, malformed, or cannot be enforced; ARO
also rejects any degradation warning as defense in depth.

Generator availability is deliberately outside the measurement-health contract: `aro selfcheck`
does not launch or authenticate an LLM CLI. Verify each configured backend separately before a
run; each command below should exit zero and emit a structured reply containing `OK` (and consumes
one model request):

```bash
claude --output-format json -p 'Reply exactly OK'
codex exec -C . --sandbox read-only --json 'Reply exactly OK'
grok -p 'Reply exactly OK' --output-format json --max-turns 1 --sandbox aro-read-only
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
and how long to run. See the working example `targets/mega-evm-v2.json`:

```jsonc
{
  "name": "mega-evm-v2",
  "target_repo":  { "path": "/abs/path/mega-evm", "baseline_ref": "<commit-sha>" },
  "hot_path":     { "file": "crates/.../host.rs", "fn": "inspect_storage" },
  "metric":       "ns_per_call", "direction": "minimize",
  "benchmark_probe": { "pkg": "mega-evm", "probe": "probes/sweep_hotloop_v2.rs",
                       "example": "sweep_hotloop_v2", "sample_prefix": "BENCH",
                       "profile": { "spin_secs": 8, "sample_secs": 4 } },
  "correctness_oracle": {
    "build": ["cargo","build","--release","-p","mega-evm"],
    "test":  ["cargo","test","--release","-p","mega-evm","--lib"],
    "differential": { "pkg":"mega-evm", "probe":"probes/evm_semantics_diff.rs",
                      "example":"evm_semantics_diff", "prefix":"DIFF" }   // the byte-identical judge; strongly recommended
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
python3 -m aro sweep targets/mega-evm-v2.json --min-pct 1.5
```
This profiles and draws a frontier map (which functions are hot, which are our leverage, which
are untouchable). **Confirm the map has content** (a profile was parsed) before starting the real
run. An empty map usually means the probe cannot spin, or the symbols were stripped.

## 5. The real unattended run (changes code, costs money)

```bash
python3 -m aro sweep targets/mega-evm-v2.json --attempt --diverge --critic \
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
| `--gen-concurrency N` | 8 | cap on parallel LLM generation (the judge stays serial: that is the moat) |
| `--dry-rounds N` | 3 | rounds without an accept before a function counts as exhausted |
| `--out-dir DIR` | `.aro-runs/<name>-diverge` | artifact directory |
| `--probe-factory` / `--no-probe-factory` | on under `--diverge` | L4a micro-bench rescue for noise-limited nodes, and the dry-frontier factory escalation below |

### Liveness guard (zero-candidate breaker)

Three consecutive attempts that produce **zero candidates** (nothing reaches the judge) trip a
liveness guard. Each zero-candidate attempt is classified from its `generator_error` events:

| Class | Meaning | Signal in `generator_error.stage` |
|---|---|---|
| **down** | generation call failed (quota / auth / CLI timeout / spawn / worktree seed) | backend name (`claude`/`codex`/`grok`), `worktree`, `seed`, `seed-commit`, `read`, `reflect` |
| **dry** | agent replied but produced no usable candidates | `parse`, `diff` (e.g. "agent made no usable .rs edits") |

Mixed errors on one attempt: majority wins; **ties count as down** (liveness protection stays).

| Streak | Action | `attempt_abort` reason |
|---|---|---|
| 3× **down** | abort immediately | `generator hard-down: 3 consecutive zero-candidate attempts (see generator_error events for the underlying failure)` |
| 3× **dry**, factory on (`--probe-factory`, default under `--diverge`) | emit `frontier_dry`, invoke the factory **once** to open new regions; continue the sweep on them | — (no abort if factory returns regions) |
| 3× **dry**, factory returns nothing | abort | `frontier dry: generator healthy, factory produced no new regions` |
| 3× **dry**, factory off (`--no-probe-factory`) | abort | `frontier dry: generator healthy, factory not enabled` |
| 3× **dry** again after a factory escalation | abort (no second escalation) | `frontier dry: generator healthy, factory produced no new regions` |

This is how an exhausted frontier (healthy agent, nothing left to propose on current regions) is
told apart from a dead generation agent. Operator checkpoints (`memory_summary` on
`attempt_finished` / `run_finished`) are unchanged.

**Cost/time**: token-heavy (the read stage, generation, and review all burn tokens). A repo like
mega-evm runs at roughly $8 to $10 per hour. `--max-attempts` is the main throttle: a medium
setting (8 / 2 / 2) measured about 6 to 7 hours, about $69, 4 accepts. Start small, then scale up.

## 6. Keep it alive across SSH disconnects

A run takes hours. Do not let it die when SSH drops. Pick one of three:

```bash
# tmux (recommended; you can scroll back)
tmux new -s aro
python3 -m aro sweep targets/mega-evm-v2.json --attempt --diverge --critic --out-dir ./.aro-runs/prod
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

`aro serve` does not re-optimize, does not call an LLM backend, and costs nothing. It only reads
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

Resume re-applies accepted edits in **acceptance order** (pareto append order — the same sequence
the manifest's `acceptance_seq` records). When a mid-chain edit no longer matches the baseline
(source drift), the engine emits `resume_degraded` naming the failing candidate + file and the
number of clean applies before it, keeps the **last-good prefix**, and continues the attempt on
that prefix. Only a **total** failure (zero edits applied) still raises the hard
`resume failed: could not re-apply …` error. Happy-path resume is unchanged
(`baseline_resumed` with the full edit count).

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
| Hot fn re-skipped every run as `source not located` / `out of editable scope (external)` | **Immediate** `out-of-scope-external` only when the symbol's crate-path tokens are all foreign (none match a workspace member — e.g. `revm` / `alloy_*` vs `mega_evm*`). Target-crate-token misses, tokenless/demangler ghosts, and macro-generated wrappers stay **`unlocated`** and close the same way only after **3** unlocated records (`unlocated 3x — treated as external`). Check `attempt_skipped.reason`. **Reopen a false close:** permtree is append-only last-record-wins — append a corrective row for the same `(workload, fn)` with any non-`out-of-scope-external` verdict (e.g. `unlocated` or a real attempt) so the latest observation is no longer closed; the frontier will re-poll. |
| LLM CLI hangs / errors | Check the selected backend's installation and authentication. The read stage has a 600 s timeout as a backstop |
| `preflight: generator backend '<name>' unavailable` | CLI missing, not authenticated, quota-dead, or wrong backend selected (`ARO_LLM_BACKEND` / spec `llm_backend`). Run the selected CLI by hand once (section 1) before retrying `--attempt` |
| Disk full | Each worktree gets its own target-dir (independent compilation is required for correctness). Clean up `.aro-*-td`, or lower `--gen-concurrency` |
| cargo/LLM CLI processes that will not exit | Leftovers from a mid-run kill; deleting the matching `.aro-worktrees` subdirectory makes them exit |

## 12. Current capability boundaries (honest)

- Optimization scope is the **profile-driven hot frontier** (our functions that are hot at
  >= min_pct and can be located to an `fn` in source). It does **not scan the whole codebase**.
- It needs the spec-fed **bench + differential probes**. Code without an oracle cannot be handled
  today; that is the unimplemented "whole-project explore" tier.
- The judge is the moat: reward-hack guard + byte-identical differential + A/A floor + paired A/B
  + bootstrap CI + auto-tighten, plus the second semantic review (`--critic`). `accepted` means
  correctness and speedup are proven; it does **not** mean "merge it". Merging is a human call
  (the manifest marks a win mergeable only when it is byte-identical and passed the critic).
  On targets that declare `terminal_bench_targets`, mergeable further requires a **tool-written**
  `terminal_stamp` whose verdict is `TERMINAL_CONFIRMED` (see section 13). A bare/legacy
  `"terminal": "TERMINAL_CONFIRMED"` string without a stamp is ignored for mergeability.
  Independently, entries whose \|Δ\| exceeds `outlier_quarantine_pct` (default **5.0 even when
  absent**; explicit `0` disables) are auto-quarantined as `mergeable=false` — a huge win is
  usually a semantics bypass, not a micro-optimization. A human clears one entry with
  `aro manifest <out> --clear-quarantine <order> --by <who> --evidence <text>` (see §13.2a).

### Terminal verdict integrity

A terminal verdict is a pure function of `terminal.json` rows. Every load path
(`aro manifest --terminal` / auto-loaded `<out_dir>/terminal.json`, `aro terminal --rejudge`)
**recomputes** each row's `delta_pct` and `status` and the top-level `verdict` from the stored
`base_ir` / `cand_ir` / `floor_pct` values; a mismatch is a hard error (tamper alarm), not a
verdict. Manifest mergeability is gated only by `terminal_stamp` (`verdict` + `source` path +
`sha256` of the terminal.json file bytes) written by `aro terminal --update-manifest` /
`apply_terminal(..., source=...)`. Hand-edited `terminal` / `verdict` fields are inert. When
a stamped source file still exists, `aro manifest` re-hashes it (missing file → warning; hash
mismatch → hard error).

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
| `control_lanes` | target JSON | list of upstream control-lane names (e.g. `["revm_pinned","revm_latest","op_revm_pinned","op_revm_latest"]`). A row is control iff any `/`-separated path segment **exactly** equals a listed name. Control rows are not counted into improved/regressed. Absent → legacy (every row is subject). |
| `control_composition_bound_pct` | target JSON | \|Δ%\| bound for control rows (default `2.0` when `control_lanes` is set). Beyond bound → `control-anomaly` and verdict `TERMINAL_CONTROL_ANOMALY` (fail-closed). |
| `correctness_oracle.test_full` | target JSON | optional full-suite command (token list) run once in the **candidate** checkout before any terminal measure. Fail-fast: non-zero exit → verdict `TERMINAL_TEST_FAILED`, no measurement. Absent → legacy (no suite at the terminal gate). Inner-loop `test` (`--lib`) is unchanged. Example: `["cargo","test","--release","-p","mega-evm"]` |
| `test_full_timeout_secs` | target JSON | seconds for `test_full` (default `1800`); independent of `terminal_timeout_secs` |
| `icount_epsilon_pct` | target JSON | probe-level Ir ε in percent; default `0.1` (also the floor clamp minimum) |
| `ARO_ICOUNT_EPSILON` | env | **wins** over `icount_epsilon_pct` when set |
| `probe_covers` | target JSON | path prefixes the probe is known to exercise (e.g. `["crates/mega-evm/src"]`). Patch with no overlap → `NO_COVERAGE`. Absent → warn and proceed |
| `selfcheck_probe_max_pct` | target JSON | max same-binary probe A/A spread for `aro selfcheck` (default `0.05`) |
| `pinned_tools` | target JSON | optional `{codspeed, cargo-codspeed, valgrind, …}` pins; mismatch fails selfcheck |
| `ARO_SKIP_SELFCHECK` | env | `1` bypasses marker gate with a loud warning (emergencies only) |
| `outlier_quarantine_pct` | target JSON | manifest tripwire: accepted entries whose \|Δ\| exceeds this percent are auto-quarantined (`mergeable=false` + `quarantine: "outlier: \|Δ\|=\<X\>% \> \<Y\>%"`) until a human clears them with `aro manifest <out> --clear-quarantine <order> --by <who> --evidence <text>` (see §13.2a). **Default `5.0` even when the field is absent** — deliberately not the usual "absent = legacy off" convention; a quarantine nobody declares protects nobody. Explicit `0` disables. Applied in both `build_manifest` and `apply_terminal` so the paths cannot diverge. Never auto-promotes `mergeable` without a recorded human audit. |
| `protected_row_families` | target JSON | list of row-family names (first `/`-segment of `row_key`) that cannot be traded. Absent/empty → legacy verdicts (no `TERMINAL_CONFIRMED_WITH_TRADE`). Control rows remain exempt. |
| `tradeable_regression_cap_pct` | target JSON | max Δ% for a subject regression in a non-protected family under WITH_TRADE (e.g. `1.0`). Only read when `protected_row_families` is declared. |
| `protected_hysteresis` | target JSON | `{margin_pp, floor_multiple}` for protected-family regressions: `H = max(floor+margin_pp, floor_multiple×floor)`. `Δ ≤ floor` clean; `floor < Δ ≤ H` = band (does not block CONFIRMED/WITH_TRADE; ablate may resolution-upgrade); `Δ > H` = violation → MIXED/REGRESSED. |

```bash
# Inspect resolved terminal config (safe anywhere; no target checkout, no measure binary)
python3 -m aro terminal targets/mega-evm-v2.json --list   # --dry-run is an alias
```

#### 13.2a Clearing an outlier quarantine (`quarantine_audit`)

A huge \|Δ\| is usually a semantics bypass, not a micro-optimization — the tripwire holds
`mergeable=false` until a **human** records a ruling. That ruling is escalate-list item 3
(outlier-quarantine adjudication); the global threshold is the wrong lever for one audited
entry.

```bash
python3 -m aro manifest .aro-runs/<RUN> --clear-quarantine <order> \
  --by <who> --evidence "<what was reviewed and why it passed>"
# optional: --spec targets/<spec>.json  (resolves threshold / terminal_required)
```

Writes an additive per-entry record and re-resolves mergeable:

```json
"quarantine_audit": {
  "cleared": true,
  "by": "<who>",
  "date": "<ISO date>",
  "evidence": "<free text>",
  "delta_pct": <entry delta_pct at ruling time>
}
```

**Staleness latch (anti-laundering):** the audit clears the outlier block only while
`|entry.delta_pct − audit.delta_pct| ≤ 0.5` percentage points. Rebuilds recompute Δ and may
mark the audit stale (`quarantine-audit-stale` in merge reasons) — quarantine re-blocks
exactly as if no audit existed. The `quarantine` reason string is **kept** either way
(provenance). Only this CLI command creates `quarantine_audit`; `build_manifest` /
`apply_terminal` carry it through untouched and never auto-create one.

**Worst-case wall-clock budget** (each `measure` may take the full timeout):

| Path | Budget | At defaults |
|---|---|---|
| Terminal gate | `2 × terminal_measure_rounds × terminal_timeout_secs` | `2 × 3 × timeout` = **3× the pre-floors budget** (`2 × 1 × timeout`) |
| Calibration (`terminal --calibrate`) | `rounds × terminal_timeout_secs` | `4 × timeout` at calibrate default rounds |

Size host / CI job timeouts accordingly before enabling median-of-N.

### 13.3 Host selfcheck (measurement health gate)

Before any Ir measurement (Gate 1.5, terminal gate, or `terminal --calibrate`), the host must
prove it can measure. `aro selfcheck` is that proof — a machine-enforced precondition, not a
manual checklist item.

**When to run**

- After provisioning a host (or any new box)
- After **any** tool change (`codspeed`, `cargo-codspeed` / valgrind pin, `rustc`)
- Every **14 days** (marker max age)
- **Before** `terminal --calibrate` (calibrating on a broken host bakes garbage floors)

**What it does**

1. **Probe A/A** — build the spec's probe once, run callgrind Ir twice on the same binary,
   compute spread%. Pass iff spread < `selfcheck_probe_max_pct` (default **0.05%**, ~10× the
   empirical same-binary floor of ≈0.004%).
2. **Tool-version probe** — records `codspeed`, `cargo-codspeed`, `valgrind`, `rustc` into an
   `env_fingerprint` string:
   `codspeed=<v>;cargo-codspeed=<v>;valgrind=<v>;rustc=<v>` (missing tool → `unknown`).
3. **Pin check** (optional) — target JSON `pinned_tools` (e.g.
   `{"codspeed": "4.18.3", "cargo-codspeed": "5.0.1", "valgrind": "3.26.0.codspeed5"}`).
   Mismatch → selfcheck **fails**. Field absent → record-only (no pin enforcement).
4. **Marker** — on pass, writes host-local `.aro-runs/selfcheck/<spec>.json`
   `{passed_at, env_fingerprint, probe_spread_pct, rounds:2}` (gitignored; never commit).
5. **`--rows`** (optional) — one measure against the checkout; verifies every calibrated floor
   row appears in the measure output (row-set integrity) and warns on drift. Does **not** run
   row-level A/A — that is `terminal --calibrate`'s job.

```bash
python3 -m aro selfcheck targets/mega-evm-v2.json
python3 -m aro selfcheck targets/mega-evm-v2.json --rows   # + floor row-set check
```

**What it gates**

The icount gate, the terminal gate, and `terminal --calibrate` load the marker **before**
measuring. Missing / older than 14 days / `env_fingerprint` ≠ current tool versions →
**hard error** (`run python3 -m aro selfcheck <spec> first`; same class as profile-fidelity).

| Override | Effect | Risk |
|---|---|---|
| `ARO_SKIP_SELFCHECK=1` | bypasses the marker gate with a loud stderr warning | floors/verdicts may be garbage; emergencies only |

Host provisioning / pinning of the valgrind–CodSpeed toolchain lives in mega-bench-reporter's
`skills/provision-instructions-lane` (ARO does not install tools; it only fingerprints them).

`env_fingerprint` is also attached additively to lessons / permtree / terminal records and to
floors-file `meta` (alongside the separate Cargo-profile `profile_fingerprint`).

### 13.4 Noise model, floors, and first-run acceptance

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

#### Terminal calibration (`aro terminal --calibrate`)

Run after a successful `selfcheck`, after tool upgrades (`rustc` / reporter), and periodically
(floors older than 30 days warn; rustc mismatch warns — neither blocks the gate). Calibration
itself requires a valid selfcheck marker:

```bash
# Same measure invocation the terminal gate uses; N rounds on ONE checkout (default 4).
python3 -m aro terminal targets/mega-evm-v2.json --calibrate \
  --checkout /path/to/baseline-worktree \
  --rounds 4

# Safe anywhere: prints the measure command + destination, never invokes the binary.
python3 -m aro terminal targets/mega-evm-v2.json --calibrate \
  --checkout /path/to/wt --dry-run
```

Per row: `floor_pct = max_pairwise|Δ%| across the N results × 2.0`, clamped to a minimum of
`icount_epsilon_pct` (0.1). Written to **`memory/floors/<spec>.json`** (versioned institutional
memory — commit it):

```json
{"meta": {"calibrated_at": "<ISO>", "rounds": 4, "checkout_describe": "...",
          "measure_bin": "...", "rustc": "rustc …", "env_fingerprint": "codspeed=…;…"},
 "floors": {"<row_key>": <floor_pct>, ...}}
```

**Before the first calibration**, terminal verdicts use `terminal_default_floor_pct` (default
**1.0%**) for every row and emit one stderr warning with the uncalibrated row count. A missing
floors file does not block the gate.

Gate classification: subject rows — improved iff Δ% < −floor(row); regressed iff
Δ% > +floor(row); else untouched. Control rows (see `control_lanes`) — `control-ok`
when \|Δ%\| ≤ `control_composition_bound_pct`, else `control-anomaly` (not counted into
improved/regressed). Any `control-anomaly` forces verdict **`TERMINAL_CONTROL_ANOMALY`**
regardless of subject outcomes. Absent `control_lanes` → legacy single-threshold on every
row. Each side is measured `terminal_measure_rounds` times (default 3; `ARO_TERMINAL_ROUNDS`
wins); Δ is computed from **per-row median** Ir.

**On a `TERMINAL_CONTROL_ANOMALY`, disambiguate with an A/A before touching the bound**:
measure two independently built checkouts of the SAME tree. Control rows moving in the A/A →
measurement/environment problem (fix it; the bound stays). A/A clean but A/B controls move →
real codegen composition; raise `control_composition_bound_pct` in the target JSON with the
A/A file as recorded justification (ratchet with evidence — never relax the code path).
Case law (mega-evm-v2, 2026-07-15): A/A control max |Δ%| = 0.10% (0/52 over bound) while the
A/B showed 2.1–4.14% on 12 control rows — moving in per-workload lockstep across all four
upstream engine variants, the composition signature (shared workload/harness code shifted;
a real anomaly is lane-idiosyncratic). Bound raised 2.0 → 5.0 on that evidence.

**Offline re-judge** (no re-measure): when a prior `terminal.json` was judged without
lane-aware rules, re-adjudicate with the current spec:

```bash
python3 -m aro terminal targets/mega-evm-v2.json --rejudge .aro-runs/<RUN>/terminal.json
# writes .aro-runs/<RUN>/terminal.json.rejudged.json (input never overwritten)
# prints old → new verdict; preserves profile_fingerprint / env_fingerprint / rounds
```

#### First-run acceptance checklist

Run once when bringing the gate up on a new host (distilled from plan §9). Do not start a
production campaign until these pass.

1. **Selfcheck**: `python3 -m aro selfcheck targets/<spec>.json` (optionally `--rows`). Must
   PASS and write `.aro-runs/selfcheck/<spec>.json`. Re-run after any tool change.
2. **Replay two refuted historical patches** (#326 SLOAD hoist, #332 saturating_sub → bare sub).
   Expect Gate 1.5 or the terminal gate to return **NEUTRAL / TERMINAL_UNTOUCHED** (or
   `refuted-by-icount` in the ledger). **No PR** — that is the pass condition.
3. **One synthetic true-positive**: insert a redundant loop (or reverse a known win). Expect a
   non-zero Ir Δ with the constructed sign that clears the row floor (terminal) or probe ε
   (Gate 1.5).
4. **Floor calibration**: run `aro terminal --calibrate` on a quiet host against the baseline
   checkout; commit `memory/floors/<spec>.json`. Probe-level `icount_epsilon_pct` stays at
   `0.1` (25× margin over whole-probe noise) — do not tighten to 0 from row-level drift.
5. **Normal campaign**: only after 1–4. First real perf PR body must quote criterion row-level
   Ir from `bench_ir_rows` (median-of-N); CodSpeed CI must agree in direction (see run-to-pr §6b).

### 13.5 `aro recheck debts` (historical open debts)

Cheap Ir re-adjudication of permtree open debts (noise-limited / no-attempt / no-candidate / …).
Each debt with a recoverable patch gets one Ir A/B; results write back through the **normal**
`memory/permtree/<spec>.jsonl` and `memory/lessons.jsonl` paths.

```bash
# Safe anywhere: lists open debts + whether a patch is recoverable. Does NOT construct
# SpecTarget, does not need the target checkout, measures nothing.
python3 -m aro recheck debts targets/mega-evm-v2.json --list-only

# Full mode (server host): needs (a) target checkout reachable at target_repo.path,
# (b) the original .aro-runs/<run>/aN dirs on THIS host (events pointers resolve locally).
# Optional: --runs-root <dir> if relative .aro-runs paths should resolve under a root.
python3 -m aro recheck debts targets/mega-evm-v2.json
python3 -m aro recheck debts targets/mega-evm-v2.json --dry-run   # measure, no ledger write
```

Outcomes: `rechecked` (ledger updated — often `refuted-by-icount` or `accepted-ir`),
`regenerate` (no stored patch under the events pointer — operator must re-generate, not invent
a closed verdict), or `error` (worktree / evaluate failure).

**CLI aliases (soft):** `recheck-debts` → `recheck debts`, `reverify` → `recheck
candidates`, bare `recheck <spec>` → `recheck staleness <spec>`, `terminal-calibrate` →
`terminal --calibrate`. Each still works and prints one stderr note; prefer the canonical
forms above.

### 13.6 Where verdicts land; config-drift hard errors

| Signal | Lands in | Notes |
|---|---|---|
| Probe Ir (Gate 1.5) | `memory/lessons.jsonl`, `memory/permtree/<spec>.jsonl` | fields `ir_delta_pct`, `profile_fingerprint`, `env_fingerprint` when measured |
| Terminal gate | `.aro-runs/<RUN>/terminal.json`, stamped onto `manifest.json` | `verdict`, `bench_ir_rows`, `profile_fingerprint`, `env_fingerprint`; per-entry `terminal_stamp` (`verdict`/`source`/`sha256`) is tool-written; `--record` also appends lessons/permtree |
| Historical recheck | same permtree + lessons ledgers | `run_id=recheck-debts`; `refuted-by-icount` closes the debt (last-record-wins) |
| Selfcheck marker | `.aro-runs/selfcheck/<spec>.json` (host-local, not committed) | `passed_at`, `env_fingerprint`, `probe_spread_pct`; required by gates |

`profile_fingerprint` = `rustc -V` + hash of effective `[profile.release]` / `[profile.bench]`.
`env_fingerprint` = host tool triple (`codspeed` / `cargo-codspeed` / `valgrind` / `rustc`).
Keep them separate: profile drift ≠ tool-version skew.

**Hard errors are not verdicts** — fix the environment and re-run; never force a PR:

| Hard error | Meaning | Operator action |
|---|---|---|
| selfcheck marker missing / stale / fingerprint mismatch | host health not proven, or tools changed since last selfcheck | `python3 -m aro selfcheck <spec>`; never skip except emergencies (`ARO_SKIP_SELFCHECK=1`) |
| `profile_fingerprint` mismatch (baseline ≠ candidate) | config drift: rustc or Cargo profile differs across worktrees | align toolchains / profiles; never open a PR on a mixed pair |
| empty / missing `meta.profile_fingerprint` from measure | reporter or env is incomplete | upgrade reporter; re-provision instructions lane |
| row-set mismatch (bench keys differ across sides) | different criterion bench set on the two checkouts | rebuild both sides with the same `terminal_bench_targets` / filter |
| measure binary unset | neither `ARO_MEASURE_BIN` nor `measure_bin` | set one (env wins) and re-run `--list` to confirm |

Terminal verdicts that **are** outcomes (and may block a PR without being "errors"):
`TERMINAL_CONFIRMED` / `TERMINAL_CONFIRMED_WITH_TRADE` (open PR when stamped; WITH_TRADE
requires declared `protected_row_families` and lists every traded regression in notes),
`TERMINAL_UNTOUCHED` / `TERMINAL_REGRESSED` / `TERMINAL_MIXED` (no PR; operator decision
on the last two — use `aro ablate` on MIXED multi-candidate bundles),
`TERMINAL_TEST_FAILED` (full-suite failed), `TERMINAL_CONTROL_ANOMALY` (control lane
moved beyond composition bound — measurement suspect; no PR). See
`python3 -m aro terminal --help` and `skill/references/run-to-pr.md` §1b.

### 13.7 `aro recheck candidates` (re-adjudicate frozen manifest candidates)

After a **gate-hardening deploy** (new differential probe, `correctness_oracle.test_full`,
stricter oracle, …) previously accepted campaign patches must be re-checked against the
**current** correctness chain — mechanically, without re-running the expensive significance
judge or doing human diff archaeology.

```bash
# Campaign run dir already has manifest.json + aN/patches/<id>.txt
python3 -m aro recheck candidates --spec targets/<spec>.json --out .aro-runs/<RUN>

# Gate only some orders (earlier entries still APPLY for compounding, marked skipped)
python3 -m aro recheck candidates --spec targets/<spec>.json --out .aro-runs/<RUN> --orders 1,3,8

# Stamp results onto manifest.json (see no-auto-promotion below)
python3 -m aro recheck candidates --spec targets/<spec>.json --out .aro-runs/<RUN> --apply
```

**When to run it**

- Immediately after changing the target's differential probe, `test_full`, or other Gate 1
  correctness settings that a frozen campaign never saw.
- Before packaging a PR from an old `manifest.json` whose accepts predate the new gates.
- Anytime you suspect an accepted entry is a semantics bypass the old oracle could not see.

**Pre-flight (environment gate)**

Before any candidate is applied or gated, reverify runs **build → test** (the fast
`correctness_oracle.test`, **not** `test_full`) on the pristine, unpatched baseline
worktree. If that fails, the host environment is broken (missing toolchain on `PATH`,
wrong working tree, etc.) — the run writes `reverify.json` with `"preflight": "fail"`,
a `detail` output tail, and an empty `entries` list, prints a loud diagnosis, exits
non-zero, and **does not** judge any candidate or mutate the manifest even with
`--apply`. A pass records `"preflight": "pass"` and reuses that same baseline worktree
(and its test pass count) for the subsequent replay — no second baseline build.

**Manifest acceptance chain fields**

New manifests stamp each accepted entry with an explicit compounding chain derived from the
event stream: `acceptance_seq` (0-based index of the `baseline_advanced` event) and `parent`
(previous accepted candidate id, or the run's `baseline_ref` for the first entry). `order` is
still the 1-based apply index; the chain makes the chronology verifiable. `aro recheck
candidates` validates the chain before any worktree work (strictly increasing
`acceptance_seq`, each `parent` links to the prior id) and aborts on inconsistency. Old
manifests that omit these fields keep order-based replay with a one-line legacy notice —
same skip-when-absent discipline as other additive fields.

**Replay semantics (candidates compound)**

Manifest entries were accepted against an **advancing** baseline: each folded patch sits on
top of the previous ones, and later SEARCH blocks may only match the advanced tree. Reverify
honors that:

1. One worktree from the spec's `baseline_ref`; one pristine baseline worktree for differential
   (created for pre-flight, then reused).
2. Entries in manifest `order` (equal to the verified acceptance chain when chain fields are
   present). Each patch is applied on the current tree.
3. Apply fails → `unappliable` (tree restored to last good state); continue.
4. Applies → Gate 1 chain in that worktree: **build → test → test_full** (only when the
   spec declares `correctness_oracle.test_full`) → **differential** vs the pristine baseline
   (whatever probe the spec **currently** declares).
5. Any gate fails → `reverify-fail` (records `failing_gate` + output tail); **that patch is
   reverted** so later entries still replay on the last good state.
6. All pass → `reverify-pass`; patch stays applied; continue.
7. `--orders` filters which entries get **gated**. Skipped entries still **apply** (marked
   `skipped`) so compounding is preserved; if a skipped entry fails to apply it is
   `unappliable`.

**Outputs**

| Artifact | Contents |
|---|---|
| `<out>/reverify.json` | header `{spec, baseline_ref, gate_config_summary, probe, preflight}` (+ `detail` on preflight fail) + per-entry `{order, id, fn, verdict, gates, detail}` |
| stdout table | order, id, fn, verdict, failing gate if any (skipped on preflight fail) |
| `--apply` | stamps each accepted entry `"reverify": {verdict, failing_gate?}` (no-op on preflight fail) |

**No auto-promotion (hard rule)**

`--apply` may force `mergeable=false` on every non-`reverify-pass` entry. It **never** sets
`mergeable=true`. A reverify-pass only proves the patch still clears the current correctness
gates; whether it should enter a PR remains a human decision (regime, critic, terminal,
quarantine, product judgment).

### 13.8 `aro ablate` (per-entry terminal attribution + greedy sub-bundle)

When a multi-candidate bundle is `TERMINAL_MIXED`, attribute each accepted entry's
**marginal** criterion-Ir effect along the acceptance chain and propose the largest
shippable sub-bundle under the row-family policy.

```bash
python3 -m aro ablate --spec targets/<spec>.json --out .aro-runs/<RUN>
python3 -m aro ablate --spec targets/<spec>.json --out .aro-runs/<RUN> --orders 1,2,8
python3 -m aro ablate --spec targets/<spec>.json --out .aro-runs/<RUN> \
  --rounds 3 --upgrade-rounds 5
python3 -m aro ablate --spec targets/<spec>.json --out .aro-runs/<RUN> --dry-run
```

**What it does**

1. Validate the acceptance chain (same as reverify); preflight the pristine baseline
   (build → test). Environment failure → `preflight: "fail"`, zero attribution.
2. Compound along the chain. At baseline and after each applied entry, measure median-of-N
   (`--rounds` / spec). Entry marginal = prefix_i vs prefix_{i-1} via `judge_terminal`.
3. Per-entry policy: `keep` / `drop` / `band`. Band triggers a **one-shot** re-measure of
   that prefix pair with `--upgrade-rounds` (default 5); the upgraded median stands once.
4. Greedy proposal: drop `drop` entries; survivors keep chain order. If dropping breaks a
   later SEARCH context → `unappliable-after-drop` (reported honestly).
5. Writes `<out>/ablate.json` + a printed table.

**Hard rule: proposal only.** Ablate **never** mutates `manifest.json` and **never** stamps
terminal fields. Certification of the proposed survivors remains `aro terminal` on a
worktree with those patches applied.
