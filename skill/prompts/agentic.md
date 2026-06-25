You are in a git worktree of a Rust project (your cwd). Make ONE behaviour-preserving performance optimization to the hot path described below.
$lens
$plan
$agenda
$lessons
Build and test with these EXACT commands (the judge uses them — do NOT guess your own):
  build: `$build_command`
  test:  `$test_command`
Iterate: edit -> build -> test -> fix -> repeat until it BUILDS and all tests PASS. A multi-site change is fine and encouraged if that is the real win.
$benchmark_contract

Hard rules:
  - Edit ONLY implementation source (never Cargo.toml/Cargo.lock, benches/, tests/).
  - Add no dependencies; keep behaviour byte-identical.
  - Do NOT `git commit`; leave changes in the working tree.

Maintainability (the judge measures speed + correctness, NOT this — but a human reviewer WILL reject a faster change that worsens it; byte-identical + faster is necessary, NOT sufficient):
  - Do NOT make one case the sole exception to a uniform pattern the file documents, and do NOT delete a layer that pattern relies on. If you'd have to edit a convention table/comment to explain your special case, that's a red flag — don't.
  - Do NOT conflate two responsibilities (e.g. make a limit-tracking fn also do gas-pricing) or hurt discoverability (a reader asking "where does X happen?" should not have to find a special case) just to reuse a value.
  - Weigh the win: a few in-memory HashMap probes on a warm path (no I/O saved), with no benchmark isolating the effect, is SMALL — it does not justify a structural cost.
  - Prefer the LAYER-PRESERVING variant: thread the already-loaded value DOWN through the existing interface instead of dissolving the boundary. Canonical example (a real reviewer rejection): to drop a redundant per-SSTORE `inspect_storage`, do NOT inline-and-delete `storage_gas_ext::sstore`; instead pass the already-loaded slot INTO it, keeping the layer.
  - Do NOT delete "dead"/"redundant" code on a hunch — prove the invariant (trace every mutator) and pin it with `debug_assert!`, or leave it.
$constraints
$prior
When build + tests pass, STOP and end your reply with exactly:
SUMMARY: <one line — what you changed, INCLUDING any data-layout choice; and IF the change trades maintainability for speed (breaks a layer/convention/single-responsibility), say so explicitly so it surfaces as should-not-merge, not a clean win>

$region_hint
