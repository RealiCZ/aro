# T3: State locality boundary survey (read-only)

**UTC date**: 2026-07-24  
**Branch**: `server/mega-evm-hwcounters`  
**Probe**: `sweep_hotloop_v2.dbg` (main Ir-blind cluster host)  
**Evidence**: `docs/data/mega-evm-hwcounters-20260723/t3_locality_survey/`  
**Method**: `perf record -e cycles -F 8000 -g` spin 8s; children report + symbol type signatures. No code changes.

## 1. Ownership table (attack-rank order)

| Rank | Symbol | self cycles% (spin8) | prior div | Owner crate | In mega-evm editable? | Feasibility one-liner |
|---|---|---:|---:|---|---|---|
| 1 | `revm host sstore + mega-evm additional_limit_ext::sstore` | 1.68 | 2.34 | mega-evm (wrapper) + revm_interpreter host + revm journal | YES | 仓内可直接动包装/limit 记账；map 局部性仍在 revm。包装优化与 T2 同类 |
| 1 | `AdditionalLimit::check_limit` | 1.85 | n/a | mega-evm::limit | YES | 仓内可直接动：快路径、布局、减分支；与 T2 record_compute_gas 同一攻击面家族 |
| 2 | `hashbrown::rustc_entry (U256 -> EvmStorageSlot)` | 4.44 | 4.90 | hashbrown + revm_state::EvmStorageSlot map | NO | 需上游 revm storage map；或 mega-evm 侧减少重复 sstore/sload、预热 slot（语义约束强） |
| 3 | `hashbrown::HashMap::get_mut (Address -> Account)` | 4.65 | 9.02 | hashbrown (via revm_state / alloy map hasher) | NO | 需上游 revm/state 布局或更少 account 查找；mega-evm 仅能减少触达次数/批处理，不能改 hashbrown 本体 |
| 4 | `foldhash::hash_bytes_long + FoldHasher::hash` | 11.00 | 4.15 | foldhash (alloy DefaultHashBuilder) | NO | 需上游 hasher 选择或 key 表示；mega-evm 不可直接替换而不动 revm/alloy 依赖 |
| 5 | `hashbrown::RawTable::reserve_rehash` | 1.11 | n/a | hashbrown + glibc malloc | NO | 间接：减少 unique slot 触达、控制 map 增长；或上游 pre-size（revm） |
| 6 | `EthFrame::init_with_context` | 2.69 | 1.80 | revm_handler::frame | NO | 需上游 revm_handler PR；mega-evm 仅能减少建帧次数（call 模式/深度） |
| 7 | `JournalInner::sload` | 1.12 | n/a | revm_context::journal | NO | 上游 revm journal；mega-evm 可审查是否多余 inspect |

Machine-readable: `t3_locality_survey/t3_ownership.json`  
Call graph: `t3_locality_survey/v2_cycles_cg.children.txt`

## 2. Path synthesis

### Cluster A — Storage/account HashMap (Ir-blind, ~12%+ cycles)

Type signatures from mangled symbols:

- `HashMap<Address, Account, DefaultHashBuilder>` → **account map**
- `HashMap<Uint<256>, EvmStorageSlot, ...>` → **storage slot map**
- Hasher: **foldhash** via alloy `DefaultHashBuilder`

**Hot path**: opcode SLOAD/SSTORE (and account touch) → `revm` host → `JournalInner` → hashbrown get_mut/rustc_entry → foldhash.

**Boundary**: maps and hasher live in **revm_state / revm_context / hashbrown / foldhash**. mega-evm wraps opcodes and limit/gas but **does not own the maps**.

### Cluster B — Frame init (~2.7% self)

`revm_handler::EthFrame::init_with_context` with `bytes::shared_to_vec` child.  
**Upstream revm_handler**. mega-evm drives frames via MegaHandler but cannot edit EthFrame in-tree.

### Cluster C — mega-evm-owned wrappers (editable)

- `additional_limit_ext::sstore` (~0.77%)
- `AdditionalLimit::check_limit` (~1.85%)
- Host/limit packaging around SSTORE (together with revm host sstore ~0.91%)

These are the **only in-repo levers** adjacent to the locality cluster without an upstream PR.

## 3. Recommended attack order

1. **First (in-repo)**: mega-evm limit/gas packaging on SSTORE and `check_limit` / `record_compute_gas` family — same family as T2 push1 wrapper mine; high control, byte-identical DIFF gates.
2. **Second (product/semantics)**: reduce redundant storage/account map traffic from mega-evm host (extra inspects, double lookups) if any exist — still in-repo, needs careful DIFF.
3. **Third (upstream)**: revm journal storage map / account map layout, pre-sizing, or hasher — requires **revm PR**; largest cycle pile (~hashbrown+foldhash) sits here.
4. **Defer**: EthFrame::init_with_context — upstream, smaller %, call-pattern dependent.
5. **Do not** treat hashbrown/foldhash symbols themselves as mega-evm edit targets.

## 4. Implication for locality mine strategy

| Approach | Verdict |
|---|---|
| Directly optimize hashbrown in mega-evm tree | **Blocked** — not editable surface |
| mega-evm wrapper/limit fast paths | **Go** — editable, Ir+wall-clock measurable |
| Upstream revm map/hasher | **Strategic** — where most cycles live; separate workstream |
| Config-only (no code) | Limited — no switch removes journal maps |

**Bottom line**: Locality mine is **real in cycles** but **mostly upstream-owned**. In-repo play is **narrow the envelope** (fewer map ops + cheaper mega-evm wrappers). Full hash map win needs revm.

## 5. Self-cert

1. Cleanup: perf finished; only read-only survey  
2. Credentials: none  
3. Identity: aro at push  
4. megaeth-labs: zero remote writes  
