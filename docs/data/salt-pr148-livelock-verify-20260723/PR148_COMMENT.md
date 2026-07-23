# PR #148 validation comment archive

- Comment ID: `5053958095`
- URL: https://github.com/megaeth-labs/salt/pull/148#issuecomment-5053958095
- Author: `mega-putin[bot]`
- Created: `2026-07-23T03:24:55Z`
- Read-back verification: exact body match
- Body SHA-256: `3f71e6f812b1520762c518428de2d1b9ce93c6e7a4579f5129a3035a8cfadc81`

## Final text

Independent A/B validation on the same 32-logical-CPU x86_64 host where issue #146 was naturally reproduced: **PASS**.

| Revision / configuration | Result |
| --- | ---: |
| Control (`19419f4`, before this fix), ordinary `cargo test` | 3/5 hung; each classified by the 300 s watchdog |
| Control (`19419f4`, before this fix), `NUM_DATA_BUCKETS=2` resize configuration | 3/5 hung; each classified by the 300 s watchdog |
| PR head (`ff8442f`), ordinary `cargo test` | 15/15 terminated normally |
| PR head (`ff8442f`), `NUM_DATA_BUCKETS=2` resize configuration | 15/15 terminated normally |

All A/B runs used default libtest concurrency, with no `--test-threads` restriction.

The two new dedicated regression-test binaries also passed repeated execution:

- `shared_committer_init`: 50/50 PASS
- `shared_committer_init_os_winner`: 50/50 PASS

For first-initialization cost, 20 alternating fresh-process control/PR pairs produced a paired median delta of **+0.669%**, below the predeclared practical-significance threshold; no perceptible regression was observed.

Each trial ran in an independent process group. Timed-out trials were cleaned up with SIGTERM followed by SIGKILL where needed, with zero residual PIDs. The complete raw record set (298 subprocess records plus a SHA-256 manifest) has been archived and is available on request.
