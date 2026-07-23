# REX6 five-lane build status @2454768

All five proposal lanes completed B-class gates + lane pipeline. **All true-negative (0 candidates).** No megaeth-labs writes; ship stopped before PR.

| Lane | Target | ε | Pipeline |
|---|---|---|---|
| 1 SSTORE/LOG | mega-evm-rex6-sstore-log | 0.05% | TN |
| 2 CREATE/CREATE2 | mega-evm-rex6-create | 0.01% | TN |
| 3 SELFDESTRUCT | mega-evm-rex6-selfdestruct | 0.02% | TN |
| 4 EIP-7702 | mega-evm-rex6-eip7702 | 0.5% (selfcheck_max 0.25%) | TN |
| 5 system/SALT | mega-evm-rex6-system-salt | 0.1% | TN |

Branch `server/mega-evm-rex6` local commits; push needs aro credentials.
