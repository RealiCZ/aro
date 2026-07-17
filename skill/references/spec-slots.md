# Target spec (the 7 slots)

A target is one declarative JSON file in `targets/`. This is how ARO generalizes: a new repo is a new spec, not new code. The authored file is **7 slots** (a human-readable contract for *what are we optimizing, and how do we know a win is real*) plus a `run` block of loop knobs. (Karpathy's autoresearch infers a 7-slot goal from free text; ARO makes the same idea an explicit, versionable file, validated by a dry-run.) `aro/spec.py:load` normalizes the 7 slots into the flat fields the driver/judge read, so the file stays clean while the internals don't churn. Template: `examples/target.example.json`. Loader: `aro/spec.py`; driver: `aro/target.py:SpecTarget`.

## The 7 slots

| slot | what | shape |
|---|---|---|
| **`target_repo`** | the repo + the frozen baseline | `{ "path": "/abs/path", "baseline_ref": "<commit-sha>" }` |
| **`hot_path`** | where the time goes: the file + function to optimize (feeds the region hint + the editable default) | `{ "file": "<crate>/src/x.rs", "fn": "hot_fn" }` |
| **`metric`** | the one number that defines a win | `"ns_per_call"` |
| **`direction`** | which way is better | `"minimize"` \| `"maximize"` |
| **`benchmark_probe`** | how the metric is *measured*: the microbench (see `harness-protocol.md`). Optional `cargo_args` (token list, e.g. `["--features","fast"]`) is appended to EVERY probe/example build+run, for hot paths behind non-default features; mirror the same flags in the oracle's build/test commands so bench and oracle compile the same code | `{ "pkg", "probe":"probes/x.rs", "example":"x", "sample_prefix":"BENCH", "cargo_args":[], "profile":{ "spin_secs":8, "sample_secs":4 } }` |
| **`correctness_oracle`** | how behaviour is *proven unchanged*: build + test + (optional) random-input differential | `{ "build":[…], "test":[…], "differential":{ "pkg", "probe":"probes/x_diff.rs", "example":"x_diff", "prefix":"DIFF" } }` |
| **`constraints`** | the edit surface + hard rules | `{ "editable":["<crate>/src/x.rs"], "no_new_deps":true, "byte_identical":true, "notes":"don't touch the window size" }` |

`editable` is what the **guard** enforces (any edit outside these files is rejected); if omitted it defaults to `[hot_path.file]`. `no_new_deps` / `byte_identical` restate rules the guard + differential already enforce, and read into the generator's context as explicit constraints.

### Profile fidelity (`profile_fidelity`)

Optional top-level field controlling the Ir measurement seam's profile guard (`icount.check_profile_fidelity`). The real invariant is **measurement build config == adjudication/production build config** (and per-candidate untampered) — not "CGU must not be 1".

| value | when to pick it | guard behavior |
|---|---|---|
| **`codspeed-ci`** (default when absent) | Target has an **external** measurement adjudicator (e.g. CodSpeed CI) that builds with cargo's default multi-CGU profile | A-priori reject `[profile.bench]` `codegen-units`/`lto` overrides and `[profile.release].codegen-units == 1` |
| **`repo-release`** | No external adjudicator — the **repo's own checked-in `[profile.release]` is production truth** (e.g. salt: `opt-level=3, lto="thin", codegen-units=1, panic="abort"`) | Comparative only: fingerprint every `profile.*` section of the candidate worktree vs the baseline worktree; any drift (value/key/section) rejects naming the culprit. No a-priori rejection of any value |

Any other value fails loud at spec load. Cargo.toml is outside every editable region, so a candidate editing it is already guard-rejected; the fingerprint check is belt-and-braces at the measurement seam.

### Terminal lane (`terminal_lane` / `terminal_probe_workloads`)

Optional top-level fields selecting how the pre-PR terminal gate sources its rows. Verdict math, floors format, membership, and ship gate semantics are identical across lanes — only the row source and disclosure change.

| field | values / default | selection criterion |
|---|---|---|
| **`terminal_lane`** | `"bench"` (default when absent) \| `"probe"`. Any other value → load-time `SystemExit` | **`bench`**: target has (or will have) a criterion/CodSpeed suite — independent-instrument confirmation. **`probe`**: explicit opt-in when the target has **no** independent bench suite; high-power probe×scale re-measure disclosed as resolution upgrade + variant generalization, **not** independent-instrument confirmation. Never auto-select probe because `terminal_bench_targets` is empty (that stays a hard error under bench). |
| **`terminal_probe_workloads`** | non-negative int; default `4` | K generated workload variants **beyond** the original probe under probe lane. Row matrix = (original + up to K variants) × `run.bench_scales`; keys `probe/<variant>/<scale>`. Prefer previously saved workload-factory variants under `targets/<name>.workloads/` when present; remaining slots are deterministic synthetic identities per (spec, baseline). |

Probe lane forces `control_lanes: []` in the terminal doc (no upstream control composition). Ship package Provenance includes the probe-lane disclosure line when the stamp carries `terminal_lane: "probe"`. Long-term: upgrade to bench whenever a real criterion suite lands in the target repo.

### Baseline pin + ship target

- **`baseline_ref` (required via `target_repo`):** pin a **commit sha** at campaign start, not a floating branch tip. `aro sweep --attempt` runs a baseline preflight (`recheck.assess`, no fetch): region churn or "baseline not ancestor of head" aborts the campaign (re-pin first); out-of-region churn only warns. Override with `--allow-stale-baseline` only when you intentionally campaign on a drifted pin.
- **`ship_target` (optional top-level):** remote/branch the PR will target, default `origin/main`. Used by `aro ship gate` / `package` / `open` (and overridable with `--target`). The gate requires every mergeable `terminal_stamp.baseline_sha` to equal that ref's head after fetch. `ship open` uses the branch half as `gh pr create --base`.
- **`ship_remote` (optional top-level string):** git remote name used by `aro ship open` for `git push -u <remote> <branch>`. Default `origin`.
- **`pr_labels` (optional top-level list of strings):** labels applied by `aro ship open` via `gh pr create --label` per entry. Empty/absent → no labels. Set this when the target repo's CI requires labels (mega-evm require-label CI went red in #346 from a missing label).
- **`ship_conformance` (optional top-level list):** target-repo quality checks run on the **final PR-branch checkout** by `aro ship conformance` (after `ship package` + supplements, before `ship open`). Each item is `{"name": "<short>", "cmd": "<shell command>", "timeout_s"?: <secs>}`. Commands run sequentially in the workdir through the shell; default per-check timeout is 1800s. Empty/absent → the command fails closed (no silent empty-pass). The record (`head_sha`, per-check exit/duration/tail, `all_green`) is written to `<workdir>/.aro-conformance.json` (or `--out`). `ship open` refuses without a green record bound to the current HEAD. Starter set for mega-evm: `fmt` / `clippy` / `test`. Coverage and mutation stay CI-adjudicated for heavy targets — declare only what is practical to re-prove locally; the prose requirements in `pr-discipline.md` still apply.

When main moves under an editable region mid-campaign, re-pin `baseline_ref` to the new head, run `aro recheck candidates --baseline <new-sha>` (replay; unappliable orders drop), then re-measure terminal on survivors — never hand-rebase certified edits.

## The `run` block (loop knobs, not "what we optimize")

| key | what | default |
|---|---|---|
| `generator` | `"agentic"` (heavy: write-compile-fix + read + reflect) \| `"ralph"` (thin one-shot) | `"agentic"` |
| `goal_target` | an absolute value of `metric` to stop at; `null` = open-ended (run until `dry_rounds`) | `null` |
| `stop` | `{ max_rounds, dry_rounds }`: hard cap + diminishing-returns cap | `{3, 2}` |
| `aa_runs` / `ab_pairs` | measurement power (A/A calibration runs / paired A/B count); CLI `--aa-runs`/`--ab-pairs` override | `2` / `4` |
| `timeout` | per build/test/bench/probe subprocess seconds: guards a hung compile during unattended runs | `1800` |
| `read_phase` / `blind` | toggles | `true` / `false` |

`objectives` and `goal` are **derived** from `metric` + `direction` + `goal_target`: no need to repeat them. (A multi-objective target that wants to *guard* a second metric may pass an explicit `objectives: [{metric, minimize}, …]`; the goal stays the primary `metric`/`direction`.)

## Make the metric measurable (the key setup step)

Optional top-level `classify` slot: `{ "runtime": ["tokio"], "crypto": ["ring"] }` extends
the builtin owner-label lists (which are EVM/arkworks-flavored) so a different dependency
ecosystem's untouchable frames get a specific label instead of `unknown`. Labeling only;
never affects the ours/not-ours decision.

If the highest-leverage operation has **no benchmark**, write one, see `harness-protocol.md`. ARO drops the probe into a worktree as a cargo `example`, runs it, and parses the samples. Isolation matters: a kernel that is most of an end-to-end number is still *diluted* there; only a direct microbench makes a sub-1% change resolvable above the noise floor.

## Adding a target

Run `python3 -m aro init --repo <repo>` to scaffold `targets/<name>.json` and two probe templates, then fill the judgment slots / probe bodies and dry-run by hand (see `plan-workflow.md` / `add-a-target.md`). Or copy `examples/target.example.json`, fill the slots by hand, and `python3 -m aro sweep targets/<new>.json --attempt`. No Python changes either way. (For a repo you want optimized fully unattended with no spec at all, see `autonomous-optimization.md`.)
