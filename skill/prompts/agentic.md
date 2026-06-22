You are in a git worktree of a Rust project (your cwd). Make ONE behaviour-preserving performance optimization to the hot path described below.
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
$constraints
$prior
When build + tests pass, STOP and end your reply with exactly:
SUMMARY: <one line — what you changed, INCLUDING any data-layout choice>

$region_hint
