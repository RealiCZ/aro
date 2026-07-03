# Target spec (the 7 slots)

A target is one declarative JSON file in `targets/`. This is how ARO generalizes: a new repo is a new spec, not new code. The authored file is **7 slots** (a human-readable contract for *what are we optimizing, and how do we know a win is real*) plus a `run` block of loop knobs. (Karpathy's autoresearch infers a 7-slot goal from free text; ARO makes the same idea an explicit, versionable file, validated by a dry-run.) `aro/spec.py:load` normalizes the 7 slots into the flat fields the driver/judge read, so the file stays clean while the internals don't churn. Template: `examples/target.example.json`. Loader: `aro/spec.py`; driver: `aro/target.py:SpecTarget`.

## The 7 slots

| slot | what | shape |
|---|---|---|
| **`target_repo`** | the repo + the frozen baseline | `{ "path": "/abs/path", "baseline_ref": "HEAD" }` |
| **`hot_path`** | where the time goes: the file + function to optimize (feeds the region hint + the editable default) | `{ "file": "<crate>/src/x.rs", "fn": "hot_fn" }` |
| **`metric`** | the one number that defines a win | `"ns_per_call"` |
| **`direction`** | which way is better | `"minimize"` \| `"maximize"` |
| **`benchmark_probe`** | how the metric is *measured*: the microbench (see `harness-protocol.md`) | `{ "pkg", "probe":"probes/x.rs", "example":"x", "sample_prefix":"BENCH", "profile":{ "spin_secs":8, "sample_secs":4 } }` |
| **`correctness_oracle`** | how behaviour is *proven unchanged*: build + test + (optional) random-input differential | `{ "build":[…], "test":[…], "differential":{ "pkg", "probe":"probes/x_diff.rs", "example":"x_diff", "prefix":"DIFF" } }` |
| **`constraints`** | the edit surface + hard rules | `{ "editable":["<crate>/src/x.rs"], "no_new_deps":true, "byte_identical":true, "notes":"don't touch the window size" }` |

`editable` is what the **guard** enforces (any edit outside these files is rejected); if omitted it defaults to `[hot_path.file]`. `no_new_deps` / `byte_identical` restate rules the guard + differential already enforce, and read into the generator's context as explicit constraints.

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

If the highest-leverage operation has **no benchmark**, write one, see `harness-protocol.md`. ARO drops the probe into a worktree as a cargo `example`, runs it, and parses the samples. Isolation matters: a kernel that is most of an end-to-end number is still *diluted* there; only a direct microbench makes a sub-1% change resolvable above the noise floor.

## Adding a target

Run `python3 -m aro plan "<free-form goal>" <repo>`: it detects build/test, has the agent fill the judgment slots and write the probes, **dry-runs** build+probe+test+differential, prints the SLOT DUMP for you to review, and writes `targets/<name>.json` (see `plan-workflow.md`). Or copy `examples/target.example.json`, fill the slots by hand, and `python3 -m aro run targets/<new>.json`. No Python changes either way. (For a repo you want optimized fully unattended with no spec at all, see `autonomous-optimization.md`.)
