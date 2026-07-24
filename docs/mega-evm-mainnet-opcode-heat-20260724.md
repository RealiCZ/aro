# Mainnet opcode heat + limit proximity (offline, 1000 txs)

**UTC**: 2026-07-24  
**Corpus**: cached `debug_traceTransaction` structLogs, n=1000  
**Nature**: offline; no RPC; no megaeth-labs writes.

## Direct conclusion

- **Ordinary ops** = **92.24%** of executions, **0.00%** of EVM-gas mass. Packaging ceiling 5–9% WP is about **wrapper invocations on ordinary ops** → execution share **92.2%** supports “almost all ops can drop per-op packaging”; gas share is **not** the right premise (storage/CALL dominate gas).
- **Tx compute-proxy** p50/p99/max = **6490098 / 9797678031 / 31705421875** vs limit **200,000,000** (**3.2450% / 4898.839% / 15852.711%** of cap).
- **Volatile-touching txs**: **99.9%**; post-access gas p99/max = **97942200 / 31705421775** vs detention **20M** (489.711% / 158527.109% of 20M).
- **Hard limit hits** (tx_gas≥limit or post-vol≥20M): see cases count **562**.

## 1) Top 30 opcodes by gas mass

| rank | op | class | count% | gas% | max single gas |
|---:|---|---|---:|---:|---:|
| 1 | `STATICCALL` | checkpoint | 0.027% | 98.757% | 9,797,669,624 |
| 2 | `DELEGATECALL` | checkpoint | 0.015% | 1.035% | 979,935,480 |
| 3 | `CALL` | checkpoint | 0.002% | 0.201% | 960,271,700 |
| 4 | `LOG1` | ordinary | 0.047% | 0.002% | 63,636 |
| 5 | `SLOAD` | volatile | 0.448% | 0.002% | 2,100 |
| 6 | `SSTORE` | checkpoint | 0.104% | 0.001% | 22,100 |
| 7 | `JUMPI` | checkpoint | 3.730% | 0.000% | 10 |
| 8 | `JUMP` | checkpoint | 3.366% | 0.000% | 8 |
| 9 | `KECCAK256` | ordinary | 0.626% | 0.000% | 144 |
| 10 | `PUSH1` | ordinary | 7.790% | 0.000% | 3 |
| 11 | `PUSH2` | ordinary | 7.185% | 0.000% | 3 |
| 12 | `LOG3` | ordinary | 0.001% | 0.000% | 97,230 |
| 13 | `DUP2` | ordinary | 5.907% | 0.000% | 3 |
| 14 | `SWAP1` | ordinary | 5.714% | 0.000% | 3 |
| 15 | `LOG2` | ordinary | 0.001% | 0.000% | 126,897 |
| 16 | `MSTORE` | ordinary | 3.334% | 0.000% | 104 |
| 17 | `POP` | ordinary | 7.118% | 0.000% | 2 |
| 18 | `ADD` | ordinary | 4.626% | 0.000% | 3 |
| 19 | `DUP3` | ordinary | 4.182% | 0.000% | 3 |
| 20 | `AND` | ordinary | 3.826% | 0.000% | 3 |
| 21 | `DUP1` | ordinary | 3.365% | 0.000% | 3 |
| 22 | `SWAP3` | ordinary | 2.971% | 0.000% | 3 |
| 23 | `ISZERO` | ordinary | 2.838% | 0.000% | 3 |
| 24 | `MLOAD` | ordinary | 2.672% | 0.000% | 7 |
| 25 | `SWAP2` | ordinary | 2.648% | 0.000% | 3 |
| 26 | `DUP5` | ordinary | 2.295% | 0.000% | 3 |
| 27 | `JUMPDEST` | ordinary | 6.581% | 0.000% | 1 |
| 28 | `PUSH8` | ordinary | 2.191% | 0.000% | 3 |
| 29 | `DUP4` | ordinary | 1.707% | 0.000% | 3 |
| 30 | `LT` | ordinary | 1.319% | 0.000% | 3 |

### Category totals

| class | ops% | gas% |
|---|---:|---:|
| ordinary | 92.245% | 0.003% |
| checkpoint | 7.288% | 99.995% |
| volatile | 0.467% | 0.002% |

**Ordinary combined: ops 92.24% / gas 0.00%** — 5–9% packaging ceiling tracks **ops** share of ordinary stream, not gas.

## 2) Length-metered opcodes

| op | count% | gas% | max single gas | txs |
|---|---:|---:|---:|---:|
| `KECCAK256` | 0.62611% | 0.00007% | 144 | 597 |
| `CALLDATACOPY` | 0.07265% | 0.00001% | 716 | 563 |
| `CODECOPY` | 0.00235% | 0.00000% | 15 | 89 |
| `RETURNDATACOPY` | 0.01420% | 0.00000% | 120 | 548 |
| `MCOPY` | 0.00045% | 0.00000% | 70 | 1 |

Interpretation: count% should be tiny (packaging tax negligible if still wrapped); max single gas can still be large (one op can burn past code-length-style bounds).

## 3) Limit proximity

### Per-tx compute proxy (Σ gasCost)

| p50 | p90 | p99 | max | limit | max/limit |
|---:|---:|---:|---:|---:|---:|
| 6490098 | 9795416064 | 9797678031 | 31705421875 | 200,000,000 | 158.5271 |

### Volatile access + post-access compute

- txs with any volatile op: **999/1000 = 99.90%**
- post-access Σgas p50/p99/max: **1694678 / 97942200 / 31705421775**
- vs block-env/oracle detention **20M**: p99 ratio **4.8971**, max ratio **1585.2711**
- vs pre-Rex3 oracle **1M**: p99 ratio **97.942**, max ratio **31705.422**

**Overshoot impact surface:** with traffic this far under detention caps, checkpoint overshoot almost never collides with a binding limit in this window — design risk is correctness bound size, not frequent user-visible OOG from overshoot.

## 4) Limit-adjacent / hit cases

- hard hits: **562**
- tx ≥50% compute limit: **354**
  - `0x009d3d56baca6fe9becda4103e004db00cf2a0751bbcf5120242202d9e53d952` tx_gas_ge_tx_compute_limit {'tx': '0x009d3d56baca6fe9becda4103e004db00cf2a0751bbcf5120242202d9e53d952', 'kind': 'tx_gas_ge_tx_compute_limit', 'tx_gas': 9794929513, 'limit': 200000000}
  - `0x01b0e00fbc06247a6a9ea0b89ef3c64f18204a7f3f69f4f34620e8fce8cab6a6` post_volatile_ge_block_env_cap_20M {'tx': '0x01b0e00fbc06247a6a9ea0b89ef3c64f18204a7f3f69f4f34620e8fce8cab6a6', 'kind': 'post_volatile_ge_block_env_cap_20M', 'post_gas': 5049985804, 'cap': 20000000}
  - `0x01b0e00fbc06247a6a9ea0b89ef3c64f18204a7f3f69f4f34620e8fce8cab6a6` post_volatile_ge_oracle_rex3_cap_20M {'tx': '0x01b0e00fbc06247a6a9ea0b89ef3c64f18204a7f3f69f4f34620e8fce8cab6a6', 'kind': 'post_volatile_ge_oracle_rex3_cap_20M', 'post_gas': 5049985804, 'cap': 20000000}
  - `0x01b0e00fbc06247a6a9ea0b89ef3c64f18204a7f3f69f4f34620e8fce8cab6a6` tx_gas_ge_tx_compute_limit {'tx': '0x01b0e00fbc06247a6a9ea0b89ef3c64f18204a7f3f69f4f34620e8fce8cab6a6', 'kind': 'tx_gas_ge_tx_compute_limit', 'tx_gas': 5049986284, 'limit': 200000000}
  - `0x01eed6114a016db06405efb38b6ab256396f2bd52106f2748a84b0a609f24740` tx_gas_ge_tx_compute_limit {'tx': '0x01eed6114a016db06405efb38b6ab256396f2bd52106f2748a84b0a609f24740', 'kind': 'tx_gas_ge_tx_compute_limit', 'tx_gas': 9794954685, 'limit': 200000000}
  - `0x02119729fbe4a4efc5c00afd3b4d5380344bfa8ce04dc7179c177ae921588e5f` post_volatile_ge_block_env_cap_20M {'tx': '0x02119729fbe4a4efc5c00afd3b4d5380344bfa8ce04dc7179c177ae921588e5f', 'kind': 'post_volatile_ge_block_env_cap_20M', 'post_gas': 97942070, 'cap': 20000000}
  - `0x02119729fbe4a4efc5c00afd3b4d5380344bfa8ce04dc7179c177ae921588e5f` post_volatile_ge_oracle_rex3_cap_20M {'tx': '0x02119729fbe4a4efc5c00afd3b4d5380344bfa8ce04dc7179c177ae921588e5f', 'kind': 'post_volatile_ge_oracle_rex3_cap_20M', 'post_gas': 97942070, 'cap': 20000000}
  - `0x037ed3e0cea2cff9422eeb6b0d5beade981eaad70ca37046fa3be50c1a9c1ce6` tx_gas_ge_tx_compute_limit {'tx': '0x037ed3e0cea2cff9422eeb6b0d5beade981eaad70ca37046fa3be50c1a9c1ce6', 'kind': 'tx_gas_ge_tx_compute_limit', 'tx_gas': 9791050636, 'limit': 200000000}
  - `0x03f3d59383ef8d0ea2967b961492eff3695dd7d23ae3eeddd667755983eca364` tx_gas_ge_tx_compute_limit {'tx': '0x03f3d59383ef8d0ea2967b961492eff3695dd7d23ae3eeddd667755983eca364', 'kind': 'tx_gas_ge_tx_compute_limit', 'tx_gas': 9791796567, 'limit': 200000000}
  - `0x056415a9b42f7afdac831c81bdcfc4606186fe3f9ad7e3f9b4d0be9d9e98019d` tx_gas_ge_tx_compute_limit {'tx': '0x056415a9b42f7afdac831c81bdcfc4606186fe3f9ad7e3f9b4d0be9d9e98019d', 'kind': 'tx_gas_ge_tx_compute_limit', 'tx_gas': 9792082244, 'limit': 200000000}
  - `0x057482255747a94c2e3b0513bf060000354b6ca193eb889313a8b6f8f49d3a2a` tx_gas_ge_tx_compute_limit {'tx': '0x057482255747a94c2e3b0513bf060000354b6ca193eb889313a8b6f8f49d3a2a', 'kind': 'tx_gas_ge_tx_compute_limit', 'tx_gas': 9792132666, 'limit': 200000000}
  - `0x065ecc50aa27c861257098c348c8531b9260ce05dad8a6f3e7210c62eb13a058` tx_gas_ge_tx_compute_limit {'tx': '0x065ecc50aa27c861257098c348c8531b9260ce05dad8a6f3e7210c62eb13a058', 'kind': 'tx_gas_ge_tx_compute_limit', 'tx_gas': 9794756523, 'limit': 200000000}
  - `0x06d1853f336abd01870bc631457e78acd9346cb832283560cf0ea32197141333` tx_gas_ge_tx_compute_limit {'tx': '0x06d1853f336abd01870bc631457e78acd9346cb832283560cf0ea32197141333', 'kind': 'tx_gas_ge_tx_compute_limit', 'tx_gas': 9790421284, 'limit': 200000000}
  - `0x084d5dddaecf42d0656090b65c5d9b4754769f666b218734b758b71f80245d18` tx_gas_ge_tx_compute_limit {'tx': '0x084d5dddaecf42d0656090b65c5d9b4754769f666b218734b758b71f80245d18', 'kind': 'tx_gas_ge_tx_compute_limit', 'tx_gas': 9795496926, 'limit': 200000000}
  - `0x084ed8086aaf0503e7bd396a61ddd08d205d5c4191a0cf71cacd712e22a95db3` tx_gas_ge_tx_compute_limit {'tx': '0x084ed8086aaf0503e7bd396a61ddd08d205d5c4191a0cf71cacd712e22a95db3', 'kind': 'tx_gas_ge_tx_compute_limit', 'tx_gas': 9794139695, 'limit': 200000000}
  - `0x094af5e052d02a11a43305bc1f3cafc9fd599e151c9d77cd146c70bbf1e813a5` tx_gas_ge_tx_compute_limit {'tx': '0x094af5e052d02a11a43305bc1f3cafc9fd599e151c9d77cd146c70bbf1e813a5', 'kind': 'tx_gas_ge_tx_compute_limit', 'tx_gas': 9791696735, 'limit': 200000000}
  - `0x09d8c081302d94def755d37a0c8a01b677626874681694b686549ceddf7d0237` tx_gas_ge_tx_compute_limit {'tx': '0x09d8c081302d94def755d37a0c8a01b677626874681694b686549ceddf7d0237', 'kind': 'tx_gas_ge_tx_compute_limit', 'tx_gas': 9794731693, 'limit': 200000000}
  - `0x0a0262e99d9ce06013e55e054a9311f3a2a4fe698b1d152c52f46169e0d52a64` tx_gas_ge_tx_compute_limit {'tx': '0x0a0262e99d9ce06013e55e054a9311f3a2a4fe698b1d152c52f46169e0d52a64', 'kind': 'tx_gas_ge_tx_compute_limit', 'tx_gas': 9792801556, 'limit': 200000000}
  - `0x0a0a5df7fe91949dd888b5390d89adae1779e484063e8e4c1d00e0f9eda74a28` tx_gas_ge_tx_compute_limit {'tx': '0x0a0a5df7fe91949dd888b5390d89adae1779e484063e8e4c1d00e0f9eda74a28', 'kind': 'tx_gas_ge_tx_compute_limit', 'tx_gas': 9794260788, 'limit': 200000000}
  - `0x0a6df1b372c564d77613df5cde91cbb251f01e99909a2c3f0f8a8612fd7fa5da` post_volatile_ge_block_env_cap_20M {'tx': '0x0a6df1b372c564d77613df5cde91cbb251f01e99909a2c3f0f8a8612fd7fa5da', 'kind': 'post_volatile_ge_block_env_cap_20M', 'post_gas': 32395237, 'cap': 20000000}
- nearest ≥50% limit samples:
  - `0x9f01e5c334f638ed49b5c6ac15cd854a69d55790109872d510816a5646daaf29` pct=158.5271 gas=31,705,421,875
  - `0xf8ac511ea36777fdf6b40af3b0cee0cfbd8798521ddd51c2bb87d4885da530af` pct=140.7215 gas=28,144,305,947
  - `0x0b0598743709eebd145f21435ea2002929dde190ecfe67eeb81b30985270b2f9` pct=48.9884 gas=9,797,678,160
  - `0x0ffb902e85015d0fcfca0b00385ab5585c8b52ce50b415b6c0e84f626f0b9480` pct=48.9884 gas=9,797,678,160
  - `0x97238ce536104c7b2a3d9f673030db504bbe385f8ca2475bf7e73c147cae63af` pct=48.9884 gas=9,797,678,160
  - `0xe60e9d1b50d0e9e0aa16814af7b1e2d74be9e6efbdf43e67a435f1ff2295a9b1` pct=48.9884 gas=9,797,678,160
  - `0xf3c55784107fe997966c4197e9b859a9f633f0d2b80b2eb6107c06626a6317d5` pct=48.9884 gas=9,797,678,160
  - `0x3a4427747b2b22dd59351a59dd8af99a33edcdb9c5950628946cd838a264ad1c` pct=48.9884 gas=9,797,678,031
  - `0x3b85afa0e179a2c53b692b021f5e74f753a2b06275338ff591374a8f04e4f8d7` pct=48.9884 gas=9,797,678,031
  - `0x418340a67a4cf1202968e9a773c19a8cad9aba6ff3080f33c4028bf25a850b90` pct=48.9884 gas=9,797,678,031

## Method caveats

gasCost from debug structLogs is EVM gas; used as compute-gas proxy. Storage-gas-heavy ops overstate pure compute; CALL gasCost may include child stipend.
- Classification: volatile = env/oracle-arm set (incl. SLOAD); checkpoint = V1 storage/call/return + JUMP/JUMPI; else ordinary.

## Self-cert

1. Cleanup: offline only.
2. Credential: no RPC in this job.
3. Identity: ARO docs when pushed.
4. megaeth-labs: zero writes.

