# REX6 preview re-settle 2026-07-23

- previous baseline: `996c16a91d071e3bb95780ea7dc5d4f1677bf746`
- new ship_target head: `245476834741de1e1a615d22e6287621b64f30cb` (`origin/cz/feat/rex6-preview`)
- commits moved:
2454768 docs: note the unstable-tracking spec default and the post-execution accounting carve-out
1d90ab1 fix(state-test): map Rex6 in the fixture spec names
d71f061 docs: drop duplicated stipend block, sync pre-inner recorder note, comment journal-read invariants
- delta: mostly docs + state-test Rex6 mapping + small execution.rs note (30+/13-)
- action: one-shot re-settle; invalidate prior selfcheck/floors/pipeline measurements bound to old SHA; keep T51 tried/lessons untouched; Lane1 call-trace evidence retained untracked under docs/data/.../call-trace (immutable gens chmod u+w restored)
- no megaeth-labs remote writes
