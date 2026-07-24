# Mainnet opcode heat + limit proximity (offline, 1000 txs)

**UTC**: 2026-07-24  
**Corpus**: 1000-tx structLog cache  
**Nature**: offline; no RPC; no megaeth-labs writes.

## Method

CALL/CREATE `gasCost` includes child stipend → naive Σ double-counts. **Effective gas** caps CALL family at 100 and CREATE/CREATE2 at 32k. Length-ops max uses **raw** gasCost.

## Direct conclusion

- **Ordinary**: ops **92.20%**, eff-gas **13.26%** → 5–9% packaging ceiling premise (wrappers on ordinary stream) is supported on **execution count**.
- **Tx compute-proxy** p50/p99/max = **56495 / 485145 / 2279067** vs **200M** → **0.0282% / 0.243% / 1.140%** of cap.
- **Volatile txs** **99.9%**; post-access p99/max **485085 / 2278587** vs 20M (**2.425% / 11.393%**).
- **Hard hits** (tx≥200M or post≥20M): **0**. Near: **4**.

## 1) Top 30 by effective gas mass

| rank | op | class | count% | eff-gas% | max eff single |
|---:|---|---|---:|---:|---:|
| 1 | `LOG1` | checkpoint | 0.047% | 38.155% | 63,636 |
| 2 | `SLOAD` | volatile | 0.448% | 29.085% | 2,100 |
| 3 | `SSTORE` | checkpoint | 0.104% | 14.702% | 22,100 |
| 4 | `JUMPI` | checkpoint | 3.730% | 1.697% | 10 |
| 5 | `JUMP` | checkpoint | 3.366% | 1.226% | 8 |
| 6 | `KECCAK256` | ordinary | 0.626% | 1.202% | 144 |
| 7 | `PUSH1` | ordinary | 7.790% | 1.064% | 3 |
| 8 | `PUSH2` | ordinary | 7.185% | 0.981% | 3 |
| 9 | `LOG3` | checkpoint | 0.001% | 0.899% | 97,230 |
| 10 | `DUP2` | ordinary | 5.907% | 0.806% | 3 |
| 11 | `SWAP1` | ordinary | 5.714% | 0.780% | 3 |
| 12 | `LOG2` | checkpoint | 0.001% | 0.684% | 126,897 |
| 13 | `MSTORE` | ordinary | 3.334% | 0.668% | 104 |
| 14 | `POP` | ordinary | 7.118% | 0.648% | 2 |
| 15 | `ADD` | ordinary | 4.626% | 0.632% | 3 |
| 16 | `DUP3` | ordinary | 4.182% | 0.571% | 3 |
| 17 | `AND` | ordinary | 3.826% | 0.522% | 3 |
| 18 | `DUP1` | ordinary | 3.365% | 0.459% | 3 |
| 19 | `SWAP3` | ordinary | 2.971% | 0.406% | 3 |
| 20 | `ISZERO` | ordinary | 2.838% | 0.388% | 3 |
| 21 | `MLOAD` | ordinary | 2.672% | 0.365% | 7 |
| 22 | `SWAP2` | ordinary | 2.648% | 0.362% | 3 |
| 23 | `DUP5` | ordinary | 2.295% | 0.313% | 3 |
| 24 | `JUMPDEST` | ordinary | 6.581% | 0.300% | 1 |
| 25 | `PUSH8` | ordinary | 2.191% | 0.299% | 3 |
| 26 | `DUP4` | ordinary | 1.707% | 0.233% | 3 |
| 27 | `LT` | ordinary | 1.319% | 0.180% | 3 |
| 28 | `GT` | ordinary | 1.224% | 0.167% | 3 |
| 29 | `MUL` | ordinary | 0.731% | 0.166% | 5 |
| 30 | `DIV` | ordinary | 0.559% | 0.127% | 5 |

### Category totals

| class | ops% | eff-gas% |
|---|---:|---:|
| ordinary | 92.196% | 13.257% |
| checkpoint | 7.337% | 57.627% |
| volatile | 0.467% | 29.116% |

**Ordinary combined: ops 92.20% / eff-gas 13.26%**.

## 2) Length-metered opcodes (raw gasCost)

| op | count% | gas% (raw) | max single gas | txs |
|---|---:|---:|---:|---:|
| `KECCAK256` | 0.62611% | 0.00007% | 144 | 597 |
| `CALLDATACOPY` | 0.07265% | 0.00001% | 716 | 563 |
| `CODECOPY` | 0.00235% | 0.00000% | 15 | 89 |
| `RETURNDATACOPY` | 0.01420% | 0.00000% | 120 | 548 |
| `MCOPY` | 0.00045% | 0.00000% | 70 | 1 |

Count% low ⇒ packaging tax of keeping them wrapped is small. Max single gas tests “one op can still burn a lot”.

## 3) Limit proximity

### Per-tx compute proxy (eff Σ)

| p50 | p90 | p99 | max | limit | max/limit |
|---:|---:|---:|---:|---:|---:|
| 56495 | 484917 | 485145 | 2279067 | 200,000,000 | 0.0114 |

### Volatile + post-access

- volatile txs: **999/1000 = 99.90%**
- post-access p50/p99/max: **56380 / 485085 / 2278587**
- vs 20M: p99 **2.425%**, max **11.393%**
- vs 1M: p99 **48.51%**, max **227.86%**

**Overshoot impact surface:** far under binding caps → overshoot is mainly a correctness-bound design issue, not frequent user-visible OOG in this window.

## 4) Limit cases

- hard: **0**
- **No hard hits.**
- near: **4** (top 10)
  - `0x01b0e00fbc06247a6a9ea0b89ef3c64f18204a7f3f69f4f34620e8fce8cab6a6` {'tx': '0x01b0e00fbc06247a6a9ea0b89ef3c64f18204a7f3f69f4f34620e8fce8cab6a6', 'kind': 'post_vol_ge_0.9M', 'post': 2278587}
  - `0xf75efc3253704e7f4e0a019cba485696c9b56ed719794bfddb8ae32412f882e8` {'tx': '0xf75efc3253704e7f4e0a019cba485696c9b56ed719794bfddb8ae32412f882e8', 'kind': 'post_vol_ge_0.9M', 'post': 2157405}
  - `0x3cacc686b4ec6d62cbeb716fd3d326ac976016cd7bd4acd4a68b35a9831359e5` {'tx': '0x3cacc686b4ec6d62cbeb716fd3d326ac976016cd7bd4acd4a68b35a9831359e5', 'kind': 'post_vol_ge_0.9M', 'post': 2154567}
  - `0x40b2fe171a797e8fa178d162b74bb967d243cb72f1641a22492cda2b0513db4c` {'tx': '0x40b2fe171a797e8fa178d162b74bb967d243cb72f1641a22492cda2b0513db4c', 'kind': 'post_vol_ge_0.9M', 'post': 2052492}

## Self-cert

1. Offline only.
2. No RPC credentials.
3. ARO docs when PAT provided.
4. megaeth-labs zero writes.

