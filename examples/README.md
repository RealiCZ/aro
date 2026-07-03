# examples/

`target.example.json` is a **spec template**, not a runnable target: ARO optimizes an
*external* repo, so there is no bundled target.

To set one up:

1. Copy it: `cp examples/target.example.json targets/<name>.json`.
2. Fill the `<placeholders>`: the repo path, the crate, the hot file/function, the metric
   and direction. Slot reference: `skill/references/spec-slots.md`.
3. Write the probe(s) it points at: `probes/<name>.rs` (microbench, prints `BENCH <ns>…`)
   and optionally `probes/<name>_diff.rs` (the byte-identical differential). How:
   `skill/references/harness-protocol.md`.
4. Run it: `python3 -m aro run targets/<name>.json`.

Or let the plan workflow build a validated spec from a free-form goal (it dry-runs
build + probe + test first): `skill/references/plan-workflow.md`.
