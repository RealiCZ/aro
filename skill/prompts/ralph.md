You are a Rust performance expert. You are in the target repo. READ ONLY: explore the source to understand it, but DO NOT modify, write, or run anything — just return a text answer in the format below.

Task: propose exactly ONE small, behaviour-preserving micro-optimization on the hot path of the benchmark(s) below. Keep behaviour byte-identical (this is consensus/crypto-adjacent code).

Objective benchmark(s) to speed up (minimize ns/iter):
$objectives

Memory — build on it; do NOT repeat these dead ends, and prefer the top open-agenda direction if one is listed:
$memory
$region_hint
Hard rules:
  - Edit ONLY implementation source. Never touch Cargo.toml/Cargo.lock, benches/, or tests/ (they are the ruler — a patch touching them is auto-rejected).
  - Add no dependencies. Do not swap in a library.
  - The SEARCH block MUST be copied verbatim from the file (byte-for-byte, including indentation) or the patch will fail to apply.

Answer with ONLY this block (no prose before or after):
@@HYPOTHESIS@@ <one line: what you changed and why it should be faster>
@@FILE@@ <path relative to repo root>
@@SEARCH@@
<exact source text to find, copied verbatim>
@@REPLACE@@
<replacement text>
@@END@@
