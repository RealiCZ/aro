# Manifest policy

`manifest.json` covers every regular evidence file under this directory except:

- `manifest.json` itself (a cryptographic manifest cannot include its own final hash);
- `.campaign.lock` (ephemeral runtime lock);
- Python/Ruff cache files (disposable tooling caches, removed before publication).

The final publication manifest was regenerated after `VERDICT.md` was added. Its entries are sorted by relative path and bind each included file's byte size and SHA-256 digest.
