# Add a new Rust target, end to end

The one walkthrough that chains everything: machine → spec → probes → validation →
campaign. Field semantics live in `spec-slots.md`; probe contracts in
`harness-protocol.md`; this file is the ORDER and the review gates.

## 0. Preconditions

- The box passes `new-box-checklist.md` part 1 (perf/sample, demangler, claude auth,
  disk, target-specific tools).
- The target repo builds standalone at the ref you want to optimize:
  `cargo build --release` from a clean checkout, submodules initialized.
- You know which crate to optimize (workspace: pick one; `aro plan` requires
  `--crate` when there are several).

## Path A (recommended): `aro plan` does the authoring

```sh
python3 -m aro plan "<free-form goal>" /abs/path/repo [--crate <name>] [--name <spec-name>]
```

One agent call, everything else deterministic: the agent reads the code in a
throwaway worktree, names the hot path, WRITES both probes
(`probes/<name>.rs` + `probes/<name>_diff.rs`), and emits the judgment slots. The
tool then assembles the spec (baseline pinned to the current SHA, editable
defaulting to the WHOLE crate src) and dry-runs six legs:

| leg | proves | when it fails |
|---|---|---|
| build | baseline compiles in a worktree | fix the repo/toolchain first |
| bench | probe emits `BENCH <ns> ...` samples | probe bug; see harness-protocol.md |
| polarity | samples are per-op TIMES, not counts (scale x8 must not scale the sample) | the probe prints a count/throughput number; the judge would score it backwards |
| test | the oracle passes and reports a pass count | non-hermetic tests? see gotchas |
| differential | `DIFF <hex>` fingerprint appears | probe bug, or the fn truly has no varying inputs (then `constraints.weak_oracle`, a downgrade) |
| profile | the spun probe shows >= 1 in-crate frame | probe lacks SPIN MODE, workload never reaches the crate, or the kernel inlined into main (`#[inline(never)]` it) |

VERDICT `clean` = safe to run. `INCOMPLETE` = the spec file is still written; fix
the named legs and re-check.

**The human gate is the slot dump. Review, do not rubber-stamp:**

- hot_path: is that plausibly where the goal's time goes? (The profile leg checks
  "some in-crate code is hot", not "the RIGHT code is hot".)
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
| hot path behind a non-default cargo FEATURE | NOT supported yet: the probe/profile builds pass no `--features` |
| cross-compilation (`--target`, or `.cargo/config.toml` build.target) | NOT supported: binaries are looked up under `release/examples/` on the host triple |
| `autoexamples = false` in the crate manifest | breaks the probe drop-in ("no example target"); remove it or add an `[[example]]` stanza |
| bin-only crate (no lib) | probes cannot `use <crate>::…`; expose the kernel via a lib target first |
| a workspace member literally named `tests` or `benches` | the reward-hacking guard rejects every edit inside it |
| tests needing docker/network/external services | every candidate fails Gate 1 and the run silently accepts nothing; give the spec a hermetic `test` command (e.g. `--lib`) |
| tiny hot kernel (small cross-crate fn) | rustc inlines it into the probe; profile leg goes empty; `#[inline(never)]` while optimizing, or accept bench-only mode |
