You are in a git worktree of a Rust project (your cwd). Make ONE behaviour-preserving performance optimization to the hot path described below.
$plan
$agenda
$lessons
You MAY edit source files and run shell commands to verify (e.g. `cargo build --release` and `cargo test --release` for the relevant crate). Iterate: edit -> build -> test -> fix -> repeat until it BUILDS and all tests PASS. A multi-site change is fine and encouraged if that is the real win.

Hard rules:
  - Edit ONLY implementation source (never Cargo.toml/Cargo.lock, benches/, tests/).
  - Add no dependencies; keep behaviour byte-identical.
  - Do NOT `git commit`; leave changes in the working tree.
$prior
When build + tests pass, STOP and end your reply with exactly:
SUMMARY: <one line — what you changed, INCLUDING any data-layout choice>

$region_hint
