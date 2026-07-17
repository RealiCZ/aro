# Plan / scaffold workflow

Use `aro init` to scaffold a target, then fill probes and dry-run by hand
(or let a campaign via `aro sweep --attempt` exercise the judge).

```sh
python3 -m aro init --repo /path/to/repo [--package <crate>] [--name <spec-name>]
# fill probes/<name>.rs + probes/<name>_diff.rs TODOs
python3 -m aro selfcheck targets/<name>.json
python3 -m aro sweep targets/<name>.json --attempt --out-dir ./.aro-runs/<name>
```

## What init writes

1. Detects the crate (package name / workspace member).
2. Writes `targets/<name>.json` with exploration-tier defaults.
3. Writes two probe templates under `probes/` with BENCH / DIFF stdout contracts.
4. Prints next steps (selfcheck → fill probes → sweep).

The human is the gate: review the SLOT DUMP / probe bodies before a campaign.
Hand-author path: copy `examples/target.example.json` and fill slots yourself
(`spec-slots.md`).
