# Add a new Rust target, end to end

The one walkthrough that chains everything: machine → spec → probes → validation →
campaign. Field semantics live in `spec-slots.md`; probe contracts in
`harness-protocol.md`; this file is the ORDER and the review gates.

## 0. Preconditions

- The box passes `new-box-checklist.md` part 1 (perf/sample, demangler, claude auth,
  disk, target-specific tools).
- The target repo builds standalone at the ref you want to optimize:
  `cargo build --release` from a clean checkout, submodules initialized.
- You know which crate to optimize (workspace: pick one; `aro init` requires
  `--package` when there are several).

## Path A (recommended): `aro init` scaffolds the authoring

```sh
python3 -m aro init --repo /abs/path/repo [--package <name>] [--name <spec-name>]
```

Writes `targets/<name>.json` (exploration-tier defaults, baseline pinned to the
current SHA, editable defaulting to the crate src) plus two probe templates.
Fill the probe TODOs, then validate before spending LLM money:

| check | proves |
|---|---|
| `aro selfcheck targets/<name>.json` | host measurement health (Ir/terminal path) |
| `aro sweep targets/<name>.json` | probe builds, spin mode, sampling, demangling, ownership |
| hand dry-run of build / bench / test / DIFF | BENCH samples + DIFF hex + hermetic tests |

**The human gate is the scaffold + probes. Review, do not rubber-stamp:**

- hot_path: is that plausibly where the goal's time goes?
- probe: does it drive the REAL public API with realistic inputs, or a strawman?
- differential: does its input corpus actually exercise the invariants a wrong
  optimization would break? (Adversarial corpus rules: harness-protocol.md.)
- direction/metric: `minimize` + time samples is the norm; anything else, think twice.
- constraints.notes: anything the optimizer must not change (public API, tuning
  constants) goes here; the generator reads it verbatim.

## Path B: manual spec + hand-written probes

1. Copy `examples/target.example.json` → `targets/<name>.json`; fill the 7 slots
   (field semantics: `spec-slots.md`). Pin `baseline_ref` to a SHA, not a branch.
2. Write the two probes to the contract in `harness-protocol.md`. Start from the
   faithful minimal templates: `fixtures/mini-target/probes/mini_target.rs` (bench +
   spin mode + scale-aware) and `mini_target_diff.rs` (seeded corpus → `DIFF <hex>`).
3. Loading the spec validates it: missing probe files, empty editable regions, and
   absent required keys all fail AT LOAD with the slot named. A stale `hot_path.fn`
   only warns (attempt mode retargets per function).

## Validate before spending LLM money

```sh
python3 -m aro sweep targets/<name>.json --min-pct 1.5   # L1: profile-only, no LLM
```

Non-empty in-crate frontier = the whole observation arm works (probe builds, spin
mode, sampling, demangling, ownership). Empty or nonsense = the diagnostic ladder
in `new-box-checklist.md` part 2.

## Launch and harvest

```sh
python3 -m aro sweep targets/<name>.json --attempt --diverge --critic \
    --workloads 3 --out-dir ./.aro-runs/<campaign>
python3 -m aro serve ./.aro-runs/<campaign>              # watch (127.0.0.1:8010)
python3 -m aro manifest ./.aro-runs/<campaign>           # harvest when done
```

Only `mergeable:true` edits go straight to a PR (`run-to-pr.md`); route the rest to
a human.

## Repo-shape gotchas (current, honest)

| Shape | Status |
|---|---|
| workspace or single-crate repo, lib target, default features | supported |
| hot path behind a non-default cargo FEATURE | supported: set `benchmark_probe.cargo_args` (e.g. `["--features","fast"]`) AND mirror the flags in the oracle's build/test commands |
| cross-compilation (`--target`, or `.cargo/config.toml` build.target) | binary paths now come from cargo's own artifact JSON, so custom target layouts resolve; but the probe must still RUN on this machine, so a foreign-arch triple remains unusable (bench and profile both execute the binary) |
| `autoexamples = false` in the crate manifest | detected at probe install with an actionable error naming the exact `[[example]]` stanza to add |
| bin-only crate (no lib) | `aro init` refuses up front with the fix (a thin src/lib.rs re-exporting the kernel); probes cannot `use <crate>::…` without a lib target |
| a workspace member literally named `tests` or `benches` | supported: the guard recognizes `tests/src/`-shaped paths as crate dirs; real harness dirs and in-src test modules stay locked |
| tests needing docker/network/external services | every candidate fails Gate 1 and the run silently accepts nothing; give the spec a hermetic `test` command (e.g. `--lib`) |
| tiny hot kernel (small cross-crate fn) | rustc inlines it into the probe; profile leg goes empty; `#[inline(never)]` while optimizing, or accept bench-only mode |
