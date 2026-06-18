# Target spec (the slots)

A target is one declarative JSON file in `targets/`. This is how ARO generalizes: a new repo is a new spec, not new code. Loader: `aro/spec.py`; driver: `aro/target.py:SpecTarget`. (Karpathy's autoresearch infers a 7-slot goal from free text; ARO makes the same idea an explicit, versionable file — and adds the goal/stop slots.)

## Slots

| slot | what | example |
|---|---|---|
| `name` | target id (and default `--out` subdir) | `"salt-committer"` |
| `repo` | path to the target repo (`~` ok) | `"~/work/salt"` |
| `baseline_ref` | git ref frozen as the baseline | `"HEAD"` |
| `build` | command token list to compile | `["cargo","build","--release","-p","banderwagon"]` |
| `test` | command token list (the correctness gate) | `["cargo","test","--release","-p","banderwagon"]` |
| `bench.probe` | path to the microbench probe (`probes/*.rs`) | `"probes/committer_probe.rs"` |
| `bench.pkg` / `bench.example` | crate + example name the probe is run as | `"banderwagon"` / `"aro_committer_probe"` |
| `bench.sample_prefix` | stdout line the probe prints samples on | `"ARO_MULINDEX_SAMPLES"` |
| `bench.metric` | the metric name | `"committer mul_index ns"` |
| `profile` | `{example, spin_secs, sample_secs}` for the observe arm | `{...,8,4}` |
| `regions` | files the generator may edit (**the guard rejects any edit outside these**) | `["banderwagon/src/salt_committer.rs"]` |
| `context.file` / `context.anchors` | code put in front of the generator | file + `[["struct","Committer"],["fn","new"],["fn","add_affine_point"]]` |
| `objectives` | `[{metric, minimize}]` | minimize the kernel ns |
| **`goal`** | `{metric, direction, target}` — `target:null` = open-ended | stop when reached |
| **`stop`** | `{max_rounds, dry_rounds}` | hard cap + diminishing-returns cap |
| `prompts` | `{agentic, hint, hint_blind}` → `prompts/*.md` | guided vs blind |
| **`generator`** | `"agentic"` (heavy: write-compile-fix + read + reflect) \| `"ralph"` (thin: one-shot read-only `claude -p`) | which live driver — default `"agentic"`; pick `"ralph"` when you know the target is all single-site micro-opts and want cheap/fast rounds (it doesn't auto-trigger — you set the driver per target) |
| `read_phase` / `blind` | toggles | `true` / `false` |

## Make the metric measurable (the key setup step)

If the highest-leverage operation has **no benchmark**, write one — a probe in `probes/` that isolates it and prints `<sample_prefix> <ns...>`. ARO drops the probe into a worktree as a cargo `example`, runs it, and parses the samples. Isolation matters: a kernel that is 76% of an end-to-end number is still *diluted* there; only a direct microbench makes it cleanly optimizable and measurable.

## Goal & stop are first-class

The objective alone isn't enough — the system needs to know **when it's done**. `goal.target` (a value to reach) and `stop` (round cap + `dry_rounds`) make stopping an explicit, checked decision. Open-ended targets (`target:null`) run until `dry_rounds` consecutive non-accepts.

## Adding a target

Copy `targets/salt-committer.json`, change the slots, write a probe if the metric isn't already isolable, and `python3 -m aro run targets/<new>.json`. No Python changes.
