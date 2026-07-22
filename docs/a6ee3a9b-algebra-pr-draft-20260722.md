# Algebra PR draft: remove redundant index reduction in Montgomery squaring

## Proposed title

`perf(ff): remove redundant modular reduction in Montgomery squaring`

## Proposed body

### Summary

Backport upstream arkworks-rs/algebra commit `a6ee3a9b` (`remove useless modular operation (#982)`).

In `MontConfig::square_in_place`, replace:

```rust
r[i % N] = carry;
```

with:

```rust
r[i] = carry;
```

The enclosing loop bounds `i` to `0..N`, so `% N` cannot change the index. This removes redundant work without changing the selected limb or arithmetic semantics.

### Validation

- Full Salt consumer conformance passed with exit `0`.
- 45 successful test summaries, zero failed summaries, and zero error lines.
- Random stress completed all 100 iterations.
- Default-feature, resize-feature, and no-default-features variants passed.
- Salt path-patch metadata confirmed the candidate Algebra worktree for all required ark packages.

Salt tests were bounded to `--test-threads=4`, equivalent to Salt's four-core CI environment. This keeps the complete test selection while avoiding the independently tracked `SHARED_COMMITTER` initialization livelock: megaeth-labs/salt#146.

### Performance evidence

Deterministic instruction counts were neutral under the existing lane floors:

- Field: `114,747,092 → 114,746,998 Ir` (`-0.0000819%`; epsilon `0.003%`).
- MSM: `277,650,590 → 277,644,983 Ir` (`-0.002019%`; epsilon `0.015%`).

A strict ABAB Salt consumer campaign used five measured rounds per mode, three samples per round, fixed CPU affinity, and state-update/witness/MSM surfaces. Witness and MSM were neutral. State-update was noisy and produced opposite-sign round and adjacent-pair estimates, so no reproducible wall-clock change is claimed.

### Scope

The proposed product PR is the one-line `montgomery_backend.rs` change only. Local conformance-harness commits used to make the evidence deterministic are not part of the product diff.

### Source

Upstream commit: `a6ee3a9b`
