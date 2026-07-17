# examples/

`target.example.json` is a **spec template**, not a runnable target: ARO optimizes an
*external* repo, so there is no bundled target.

To set one up:

1. Scaffold: `python3 -m aro init --repo /path/to/rust [--package <crate>] [--name <name>]`
   (or copy `examples/target.example.json` → `targets/<name>.json` and fill placeholders).
2. Fill the probe TODOs: `probes/<name>.rs` (microbench, prints `BENCH <ns>…`)
   and `probes/<name>_diff.rs` (byte-identical differential). How:
   `skill/references/harness-protocol.md`. Slot reference: `skill/references/spec-slots.md`.
3. Host health, then campaign:
   `python3 -m aro selfcheck targets/<name>.json`
   `python3 -m aro sweep targets/<name>.json --attempt --out-dir ./.aro-runs/<name>`
