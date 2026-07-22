# Salt issue #146 follow-up comment: ordinary `cargo test` evidence

- Issue: https://github.com/megaeth-labs/salt/issues/146
- Comment: https://github.com/megaeth-labs/salt/issues/146#issuecomment-5044593534
- Comment ID: `5044593534`
- Author: `mega-putin[bot]`
- Created: `2026-07-22T10:13:41Z`
- Live readback: exact byte-for-byte match with the submitted body, including the final newline
- Authorization scope: this single comment only; no general authorization for further replies

## Final comment text

Additional evidence from a later validation run broadens the affected test surface.

On the same 32-logical-CPU host, the ordinary `cargo test` command—without the `test-bucket-resize` feature and without the resize-specific environment variables—also entered the same nondeterministic initialization hang. This occurred before the subsequent resize-feature command was reached.

This indicates that the livelock is not specific to `test-bucket-resize` or its environment variables. The relevant trigger is high-concurrency first access to `SHARED_COMMITTER`; therefore, the `test-bucket-resize` qualifier in the issue title is narrower than the observed impact. The default test suite can also be affected under high libtest concurrency.

This is consistent with the scheduling-sensitive behavior described in the Reproduction section: the same build sometimes completes in 6.74 seconds and sometimes remains hung. The same pass-or-hang behavior has now been observed with ordinary `cargo test`.

Bounding libtest concurrency with `--test-threads` is therefore also applicable to ordinary `cargo test`. Detailed logs from this occurrence are archived and available on request.
