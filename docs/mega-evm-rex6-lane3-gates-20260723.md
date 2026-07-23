# Lane3 REX6 SELFDESTRUCT @ 2454768

## Gates PASS
- Fingerprint DIFF `d451d683ca6cb8c669afb93bb83c5c4d5308f61105ba340da0d3336bece1b931`
- Call-trace 7-file editable
- Mutation PASS (new_account_gas +1; selfdestruct_with_beneficiary_guard burn)

## Spec
- targets/mega-evm-rex6-selfdestruct.json epsilon **0.02%**
- probe floors 10 rows

## Pipeline TRUE NEGATIVE
- attempts: run_without_catch_error / inspect_account / instruction_table — all no-candidate
- 0 accepted; exit 2
- profile floor hash/keccak dominant
