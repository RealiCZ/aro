# Epsilon revise after rows selfcheck (re-settle baseline)

- baseline: `245476834741de1e1a615d22e6287621b64f30cb`
- selfcheck spreads (full precision):
  - initial 0.01 selfcheck: 0.002532941067657824%
  - selfcheck --rows: 0.006792410633858276%
- worst applicable: 0.006792410633858276%
- 3× lower bound: 0.020377231901574828%
- previous provisional: 0.01%  (now BELOW 3× bound → revise)
- selected epsilon: 0.03%  (margin 0.009622768098 pp)
- still 3.3× tighter than pre-campaign 0.1%
- criterion floors remain separate (192-row four-round set on 2454768)

## Post-revise selfcheck

- Ir marker spread: 0.0004053169423269848%
- passed_at: 2026-07-23T08:22:38Z
- fingerprint: codspeed=4.18.3;cargo-codspeed=5.0.1;valgrind=3.26.0.codspeed5;rustc=1.96.0
- worst across three runs: 0.006792410633858276%
- 3× bound: 0.020377231901574828%
- final epsilon: 0.03% (still holds)
