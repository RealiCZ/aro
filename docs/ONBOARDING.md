# Onboarding a Rust project to ARO

Practitioner guide: point ARO at an arbitrary Rust repo and get a judged optimization loop.
Ground truth for fields is `aro/spec.py` and the worked example `targets/mega-evm-v2.json`.
Server ops, Ir gates, and host tooling live in [OPERATIONS.md](OPERATIONS.md).

---

## Two tiers (read this first)

| Tier | What you can do | What you need |
|---|---|---|
| **Exploration** | Profile the hot frontier, generate candidates, accept wins under the probe judge (`accept-ir` / wall-clock paired A/B + differential). Safe for discovery and compounding. | Host with cargo + profiler; two probes (bench + DIFF); minimal 7-slot spec. |
| **Certification** | Ship via the pre-PR terminal gate (`TERMINAL_CONFIRMED` stamped on `manifest.json`). Required when the target declares `terminal_bench_targets`. | Everything above **plus** criterion+codspeed harness in the *target* repo, `measure_bin` (or `ARO_MEASURE_BIN`), `pinned_tools`, floors calibration (`aro terminal --calibrate`), and optional policy/lane fields. |

`aro init` scaffolds the **exploration** tier only.
Certification knobs are deliberate add-ons — see the checklist printed by init and § Spec field reference below.
Worked certification example: `targets/mega-evm-v2.json` + [OPERATIONS.md](OPERATIONS.md) §13.

---

## Prerequisites

| Need | Why | How to verify |
|---|---|---|
| **Linux measurement host** (for Ir / terminal) | Callgrind Ir and CodSpeed tooling; macOS can map/profile with `/usr/bin/sample` but Ir measure steps fail without valgrind | [OPERATIONS.md](OPERATIONS.md) §0 / §13.1 |
| **Python 3.9+**, stdlib only | ARO itself | `python3 --version` |
| **Rust + cargo + git** | Build / test / worktree isolation | `cargo --version`, `git --version` |
| **Profiler** | Frontier map | macOS: `/usr/bin/sample`; Linux: `perf` with `kernel.perf_event_paranoid` usable ([OPERATIONS.md](OPERATIONS.md) §0) |
| **valgrind / codspeed / cargo-codspeed / rustc** | Instruction-count gate + terminal measure | Versions pinned in target JSON `pinned_tools`; checked by `aro selfcheck` ([OPERATIONS.md](OPERATIONS.md) §13.3) |
| **LLM CLI** (claude / codex / grok) | Candidate generation (+ optional critic) | Authenticated; *not* part of selfcheck — verify separately ([OPERATIONS.md](OPERATIONS.md) §1) |
| **Target repo green** | Every candidate rebuilds from a frozen baseline | `cargo build --release` and the package's tests pass standalone |
| **Probes as package examples** | ARO copies probe sources into `<pkg>/examples/<name>.rs` in each worktree (`aro/target.py:write_probe`) | Package must allow auto-discovered examples, or declare `[[example]]` if `autoexamples = false` |

Probes live under **this** repo (`probes/*.rs`); paths in the spec are relative to the aro-py root (`aro/spec.py` module docstring).
They are *not* committed into the target repo by default.

---

## Walkthrough

### 1. Scaffold

```sh
cd /path/to/aro-py
python3 -m aro init --repo /path/to/your-rust-repo [--package <crate>] [--name <slug>]
```

Writes (see `aro/init.py`):

- `targets/<slug>.json` — minimal 7-slot spec (`metric`/`direction` default to `ns_per_call` / `minimize`)
- `probes/<slug>-probe.rs` — BENCH template with `TODO(aro-init)`
- `probes/<slug>-diff.rs` — DIFF template with `TODO(aro-init)`

Multi-member workspaces require `--package` (or the member path).
`--force` overwrites existing files.

### 2. Fill the two probe TODOs

Replace `placeholder_work` with calls through the crate's **public API**.
Keep the stdout contracts — the driver parses them (`aro/target.py`).

#### BENCH contract (`benchmark_probe.sample_prefix`, default `BENCH`)

- Print **one** line: `BENCH <ns> <ns> …` — per-call **nanosecond times**, not counts or throughput.
- Honor `ARO_BENCH_SCALE` (multiply inner reps) so auto-tighten can drop the noise floor.
- Implement **spin mode**: if `argv[1]` is seconds, spin the same workload and print `SPUN <n>` (required for profiling; [OPERATIONS.md](OPERATIONS.md) §0).
- Determinism: fixed seeds / fixed inputs; no OS randomness or wall-clock seeds; `black_box` inputs **and** accumulators so LLVM cannot elide work.

#### DIFF contract (`correctness_oracle.differential.prefix`, default `DIFF`)

- Print **one** line: `DIFF <hex>` — a fingerprint over many deterministic inputs.
- Baseline and candidate must match **byte-for-byte** or Gate 1 fails before significance.
- Determinism: fixed PRNG seed only; fold **every** observable (success, gas, returndata, storage reads, …) into the fingerprint; same corpus in both worktrees.

**Domain-aware corpus (why `probes/evm_semantics_diff.rs` exists).**
A random xorshift over a public API is a start, but optimizations break *domain* invariants (frame lifecycle, stipend edges, system-contract intercepts, depth guards).
The mega-evm DIFF probe is adversarial and domain-aware: it exercises MINI_REX/REX/REX3/REX4/REX5 paths and folds success, gas_used, returndata, and storage into one FNV-1a fingerprint.
Copy that *spirit* for your domain — cover the mechanisms a “faster wrong” patch would skip — not necessarily the EVM opcodes.

Without a differential probe, the judge returns `verify-failed` unless `constraints.weak_oracle=true` (tests-only; verdict tagged `WEAK ORACLE`). See `aro/target.py` (`differential_required`) and § Honest limits.

### 3. Finish the minimal spec

Hand-edit `targets/<slug>.json`:

- Set `hot_path.file` / `hot_path.fn` (or leave advisory; attempt mode retargets per function).
- Pin `target_repo.baseline_ref` to a **commit SHA** when you care about resume/recheck.
- Confirm `constraints.editable` covers the source you allow the generator to touch.
- Ensure `correctness_oracle.build` / `test` match how the package is built in CI.

Load-time validation (`aro/spec.py:validate_artifacts`): probe files must exist; editable region must be non-empty.

### 4. Host selfcheck

```sh
python3 -m aro selfcheck targets/<slug>.json
# optional row-set integrity once floors exist:
python3 -m aro selfcheck targets/<slug>.json --rows
```

Writes host-local `.aro-runs/selfcheck/<slug>.json` (required by icount / terminal / calibrate paths).
Re-run after tool upgrades or every ~14 days ([OPERATIONS.md](OPERATIONS.md) §13.3).

### 5. First map, then first attempt

```sh
# L1 frontier map only — no LLM, no patches
python3 -m aro sweep targets/<slug>.json --min-pct 1.5

# Unattended meta-loop once the map looks sane
python3 -m aro sweep targets/<slug>.json --attempt --diverge --critic \
    --out-dir ./.aro-runs/<slug>-explore
```

Confirm the map has in-crate frames before spending tokens.
Empty map → probe spin / symbols / `perf` — see `skill/references/new-box-checklist.md`.

### 6. Certification tier (optional, for shipping)

Only when you need mergeable terminal stamps:

1. Add criterion benches + CodSpeed integration in the **target** repo.
2. Set `terminal_bench_targets`, `measure_bin` (or `ARO_MEASURE_BIN`), and preferably `pinned_tools`.
3. Calibrate floors: `python3 -m aro terminal targets/<slug>.json --calibrate --checkout <baseline-wt>`.
4. Optional policy: `control_lanes`, `control_composition_bound_pct`, `protected_row_families`, `tradeable_regression_cap_pct`, `protected_hysteresis`.
5. Pre-PR: `aro terminal` → stamp via `--update-manifest`; harvest with `aro manifest --spec …`.

Full runbook: [OPERATIONS.md](OPERATIONS.md) §13.

---

## Spec field reference

Authored shape is the **7-slot** contract plus optional top-level / `run` knobs.
Loader: `aro/spec.py:from_dict` / `TargetSpec`.
Worked full example: `targets/mega-evm-v2.json`.
Template: `examples/target.example.json`.

### Minimal tier (required for exploration)

| Field | Shape / notes | Source |
|---|---|---|
| `name` | Spec slug | required |
| `target_repo` | `{path, baseline_ref?}` — `path` required; `baseline_ref` default `HEAD` | required |
| `metric` | e.g. `ns_per_call` | required |
| `direction` | `minimize` \| `maximize` (default `minimize`) | optional w/ default |
| `hot_path` | `{file, fn?}` — seed for context / editable default; `fn` advisory | optional |
| `benchmark_probe` | `{pkg, probe, example, sample_prefix?, profile?, cargo_args?}` — probe path relative to aro-py root | required |
| `correctness_oracle` | `{build:[…], test:[…], differential?, test_full?}` — build/test are token lists | required |
| `correctness_oracle.differential` | `{pkg, probe, example, prefix}` — strongly recommended | optional but enforced unless weak_oracle |
| `constraints` | `{editable, no_new_deps, byte_identical, notes, weak_oracle}` — empty `editable` fails load | optional w/ defaults |
| `run` | Loop knobs (below) | optional |

### `run` block (loop knobs)

| Field | Default | Notes |
|---|---|---|
| `generator` | `agentic` | `agentic` \| `ralph` |
| `goal_target` | `null` | absolute metric stop; null = open-ended |
| `stop` | `{max_rounds:3, dry_rounds:2}` | hard cap + dry streak |
| `aa_runs` / `ab_pairs` | `2` / `4` | CLI `--aa-runs` / `--ab-pairs` override |
| `timeout` | `1800` | per build/test/bench/probe subprocess (s) |
| `bench_scales` | `[1, 8, 64]` | auto-tighten ladder on noise-limited |
| `read_phase` / `blind` | `true` / `false` | |
| `prompts` | built-in agentic/hint templates | override template names |

### Certification tier

| Field | Notes | Source |
|---|---|---|
| `terminal_bench_targets` | Non-empty enables terminal gate; e.g. `["mega_bench"]` | top-level |
| `terminal_bench_filter` | Optional criterion filter string | top-level |
| `measure_bin` | Path to `mega-bench-reporter`; **env `ARO_MEASURE_BIN` wins** | top-level |
| `pinned_tools` | e.g. `{codspeed, cargo-codspeed, valgrind, rustc}` — selfcheck pin enforcement | top-level (raw) |
| `icount_epsilon_pct` | Probe Ir ε %; default `0.1`; env `ARO_ICOUNT_EPSILON` wins | top-level |
| `probe_covers` | Path prefixes the probe exercises; no overlap → `NO_COVERAGE` | top-level |
| `terminal_timeout_secs` | Per measure; default `4 × run.timeout` | top-level (raw via `spec_field`) |
| `terminal_measure_rounds` | Median-of-N; default `3`; env `ARO_TERMINAL_ROUNDS` wins | top-level (raw) |
| `terminal_default_floor_pct` | Pre-calibration floor; default `1.0` | top-level (raw) |
| `selfcheck_probe_max_pct` | Max same-binary probe A/A spread; default `0.05` | top-level (raw) |
| `correctness_oracle.test_full` | Full suite in candidate before terminal measure | oracle block |
| `test_full_timeout_secs` | Default `1800` | top-level (raw) |

Floors file (not a spec field): `memory/floors/<name>.json` written by `aro terminal --calibrate`.

### Policy tier (optional; terminal / ablate)

| Field | Notes |
|---|---|
| `control_lanes` | Upstream row names excluded from improved/regressed; identity = exact `/`-segment match |
| `control_composition_bound_pct` | Max \|Δ%\| on control rows; default `2.0` when lanes set; anomaly → `TERMINAL_CONTROL_ANOMALY` |
| `protected_row_families` | Families that cannot be traded; enables `TERMINAL_CONFIRMED_WITH_TRADE` |
| `tradeable_regression_cap_pct` | Cap for non-protected subject regressions under WITH_TRADE |
| `protected_hysteresis` | `{margin_pp, floor_multiple}` band for protected-family regressions |
| `outlier_quarantine_pct` | Manifest tripwire; **default `5.0` even when absent**; `0` disables |

### Other top-level

| Field | Notes |
|---|---|
| `llm_backend` | `claude` (default) / `codex` / `grok`; env `ARO_LLM_BACKEND` wins |
| `critic_backend` | Optional cross-model critic |
| `classify` | `{runtime:[…], crypto:[…]}` extends owner-label lists |
| `objectives` | Optional multi-metric guard list; else derived from metric+direction |

---

## Honest limits

### Shared-bench composition and sub-1% certification

The terminal gate measures criterion rows via a shared harness / workload binary.
Control lanes (rows that do **not** execute candidate code) bound how much of an observed Δ can be blamed on composition vs the candidate.
If control \|Δ%\| exceeds `control_composition_bound_pct`, the verdict is `TERMINAL_CONTROL_ANOMALY` — fail-closed, no PR.

**A/A before relaxing the bound** ([OPERATIONS.md](OPERATIONS.md) §13.4): measure two independently built checkouts of the *same* tree.
Controls moving in A/A → host/environment problem (fix tooling; keep the bound).
A/A clean but A/B controls move → real codegen composition; raise the bound only with that evidence on record.
Case law on mega-evm-v2: A/A control max \|Δ%\| ≈ 0.10% while A/B showed multi-percent lockstep moves across upstream engine lanes — composition signature, not lane-idiosyncratic noise.

**Practical bound:** shared-bench composition variance means **sub-1% terminal certification is not a free lunch**.
Probe-level Ir (Gate 1.5, ε default 0.1%) can still resolve tight wins in the inner loop; shipping claims that rely on criterion rows must clear row floors **and** control composition checks.

### `weak_oracle` escape hatch

Setting `constraints.weak_oracle=true` drops the differential requirement (`aro/target.py:differential_required`).
Gate 1 becomes build + test only; the verdict is tagged **WEAK ORACLE**.
What you forfeit: byte-identical behaviour proof — “faster” can mean “different on untested inputs.”
Use only when a true DIFF probe is impossible; never for consensus-critical code you intend to merge on automation alone.

### Other hard limits (unchanged)

- Generator is a model; only the judge is code — re-runs propose different patches.
- Metric must be isolable in a microbench; end-to-end dilution hides real wins.
- Single-machine measurement; quiet host matters for wall-clock paths.
- `accepted` ≠ `mergeable`; terminal stamp + critic + quarantine rules apply ([OPERATIONS.md](OPERATIONS.md) §12–13).

---

## Related docs

| Doc | Role |
|---|---|
| [OPERATIONS.md](OPERATIONS.md) | Server ops, Ir/terminal runbook, selfcheck, control-lane protocol |
| [../OPERATING.md](../OPERATING.md) | Short day-to-day operator index |
| [../README.md](../README.md) | What ARO is, CLI surface, quickstart |
| `skill/references/harness-protocol.md` | Probe/DIFF authoring depth |
| `skill/references/spec-slots.md` | 7-slot narrative (may lag certification fields — prefer this file + `aro/spec.py`) |
| `skill/references/add-a-target.md` | Alternate path via `aro init` + hand-authored probes |
| [archive/](archive/) | Historical design docs (not current behaviour) |
