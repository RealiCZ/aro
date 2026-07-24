# Lane2 REX6 CREATE/CREATE2 @ 2454768

## Gates PASS
- Fingerprint DIFF `86afb497b572ccca00c0000c46edff599f203ec7a15ff3bd9a25ff3a1fd4e203`
- Call-trace gen `20260723T103716Z-full-f79481459503` editable 7 files (no data_size/state_growth op evidence)
- Mutation: semantic create_contract_gas +1; perf burn in create_rex6 Ir +108M

## Spec
- targets/mega-evm-rex6-create.json epsilon **0.01%** (A/A ~0.0009%)
- probe floors 10 rows

## Pipeline TRUE NEGATIVE
- attempts: run_without_catch_error no-candidate; process_next_action out-of-scope
- profile floor keccak_p 55% — create path not self-time dominant under this tuple
- 0 accepted; exit 2
