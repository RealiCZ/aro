# mega-evm 周期/Ir 背离测量报告

**日期（UTC）**: 2026-07-24  
**分支**: `server/mega-evm-hwcounters`  
**性质**: 纯发现型 B 类；不改判官、不改 spec、megaeth-labs 零远端写入  
**基线 pin**: `245476834741de1e1a615d22e6287621b64f30cb`（与 mega-evm-v2 / REX6 lane specs 一致）  
**证据根**: `docs/data/mega-evm-hwcounters-20260723/`  
**机读汇总**: `docs/data/mega-evm-hwcounters-20260723/analysis.json`

---

## 1. 测量契约

| 项 | 值 |
|---|---|
| CPU | AMD EPYC 9754；`taskset -c 2`（sibling 2-3，只钉 2） |
| 线程 | `RAYON_NUM_THREADS=1` `OMP_NUM_THREADS=1` |
| Scale | `ARO_BENCH_SCALE=8` |
| 主探针 | `probes/sweep_hotloop_v2.rs` |
| 辅探针 | Lane1 SSTORE/LOG、Lane2 CREATE |
| stat | 每组 **5 轮**，**median / MAD** |
| record | `perf record -e … -F 4k–12k -g`；**Total Lost Samples: 0** |
| 二进制 | release+`CARGO_PROFILE_RELEASE_DEBUG=full`（主机 `.dbg` 不入 git，见 SHA256） |

**计数器组（EPYC 实测可用）**: A=`cycles,instructions,branches,branch-misses,stalled-cycles-frontend`；B=L1i/L1d/iTLB/cache-misses；C=page/minor/major-faults。

> 「Ir 对照」= 同二进制 HW `instructions` 采样份额。ARO 判官 Callgrind Ir 量纲不同；既有 pipeline 自报 push1 Ir≈18–19.5% 并列表述。绝对数不可跨工具比，**份额背离方向**可作判官扩展依据。

环境指纹: `docs/data/mega-evm-hwcounters-20260723/env-fingerprint.txt`

---

## 2. 全局 IPC / 分支（主探针）

| 指标 | median | MAD |
|---|---:|---:|
| cycles | 4381779198 | 38928676 |
| instructions | 13487698310 | 114517 |
| **IPC** | **3.078** | 0.028 |
| branch-miss % | **0.0928%** | 0.0012 |
| frontend stall / cycles | **10.37%** | — |

辅探针 IPC：Lane1 **2.823**；Lane2 **3.366**。  
原始: `stat/*_A_ipc/`

---

## 3. 四个必答问题

### Q1. push1 / 包装操作码：周期 vs 指令

| 符号 | cycles% | instructions% | cyc/instr | 方向 |
|---|---:|---:|---:|---|
| `compute_gas_ext::push1` | **18.27** | **23.72** | **0.77** | 指令份额更高 → Ir/指令**高估**周期重要性 |
| `compute_gas_ext::add` | 4.79 | 6.49 | 0.74 | 同向 |
| `compute_gas_ext::pop` | 4.09 | 5.59 | 0.73 | 同向 |
| `revm::host::log` | 2.18 | 1.89 | 1.15 | 周期略重 |
| sstore* | 1.89 | 2.46 | 0.77 | 指令略高估 |

对照 Callgrind 历史 push1 Ir≈**18–19.5%**：HW cycles **18.27%** 与之接近；HW instructions **23.72%** 高约 5pp。  
**结论**：push1 是高 IPC 热函数，不是「周期远高于 Ir」源。真正被 Ir **低估**的是 HashMap/hashbrown（§4，div 4–9×）。  
证据: `record/sweep_hotloop_v2_cycles/report.symbol.txt`, `record/sweep_hotloop_v2_instructions/report.symbol.txt`

### Q2. 派发 vs branch-miss

全局 branch-miss rate = **0.0928%**（极低）。

| # | 符号 | miss 份额% | samples |
|---|---|---:|---:|
| 1 | foldhash::hash_bytes_long | 40.59 | 5383 |
| 2 | RINvXs0_NtNtNtCsjOiS4846Slk_4core4iter8adapters3mapINtB6_3Ma | 11.16 | 1473 |
| 3 | glibc::_int_malloc | 8.97 | 1193 |
| 4 | revm::host::log | 8.62 | 1145 |
| 5 | glibc::malloc_consolidate | 2.14 | 286 |

push1 仅占 branch-misses **0.79%**。`instruction_table` 未进 top（percent-limit 0.2）。  
**结论**：派发点 **不是** miss 大户；miss 在 **哈希 + LOG/malloc**。  
证据: `record/sweep_hotloop_v2_branch-misses/report.symbol.txt`

### Q3. I-cache 与 gas 包装膨胀

| # | 符号 | L1-icache-miss% |
|---|---|---:|
| 1 | foldhash::hash_bytes_long | 10.97 |
| 2 | EthFrame::init_with_context | 8.42 |
| 3 | MegaHandler::run_without_catch_error | 7.40 |
| 4 | glibc::__memmove_avx512 | 6.82 |
| 5 | HashMap::get_mut | 4.80 |

push1 的 icache-miss 份额仅 **0.78%**（vs cycles 18.27%）。  
**结论**：包装宏未表现为 push1/add/pop 的 I-cache 税；压力在控制面+哈希/memmove。  
证据: `record/sweep_hotloop_v2_L1-icache-miss/report.symbol.txt`

### Q4. page fault（数字）

| 场景 | page-faults | minor | major |
|---|---:|---:|---:|
| scale=8 短进程 median | 219 | 219 | **0** |
| spin 8s 整段 median | 218 | 218 | **0** |
| `perf stat -I 1000` spin 6s | 第1s **215**；第2–5s **0**；结束s **3** | 同左 | **全程 0** |

**结论**：预热后稳态 **可忽略**——major **恒 0**；minor 几乎全在启动首秒，稳态 **0 faults/s**。  
证据: `stat/steady_faults/v2_interval.txt`, `stat/*_C_faults/`, `stat/steady_faults/*_spin8_*.stat.csv`

---

## 4. Top 30 周期占比（主探针）+ 背离

div = cycles%/instr%；>1 → 周期份额高于指令（Ir **低估**墙钟）。

| # | 函数 | cycles% | instr% | div | 分类 |
|---|---|---:|---:|---:|---|
| 1 | `compute_gas_ext::push1` | 18.27 | 23.72 | 0.77 | instr-heavy (Ir overstates) |
| 2 | `MegaHandler::run_without_catch_error` | 10.09 | 8.53 | 1.18 | aligned |
| 3 | `foldhash::hash_bytes_long` | 8.96 | 9.27 | 0.97 | aligned |
| 4 | `compute_gas_ext::add` | 4.79 | 6.49 | 0.74 | instr-heavy (Ir overstates) |
| 5 | `HashMap::get_mut` | 4.60 | 0.51 | 9.02 | memory-bound-ish |
| 6 | `hashbrown::rustc_entry` | 5.52 | 0.87 | 4.90 | memory-bound-ish |
| 7 | `compute_gas_ext::pop` | 4.09 | 5.59 | 0.73 | instr-heavy (Ir overstates) |
| 8 | `EthFrame::init_with_context` | 2.81 | 1.56 | 1.80 | cycle-heavy (Ir understates) |
| 9 | `revm::host::log` | 2.18 | 1.89 | 1.15 | aligned |
| 10 | `FoldHasher::hash` | 1.99 | 0.48 | 4.15 | memory-bound-ish |
| 11 | `RINvMs2_NtCs5GT9zvB7TYh_12revm_handler5frameNtB6_8EthFrame13` | 1.96 | 1.01 | 1.94 | cycle-heavy (Ir understates) |
| 12 | `RINvNtNtCs8jNs2MqwBKX_16revm_interpreter12instructions4host5` | 1.83 | 1.43 | 1.28 | cycle-heavy (Ir understates) |
| 13 | `RNvXs5_NtNtCs40yuATWtwNz_8mega_evm3evm9executionINtB7_7MegaE` | 1.82 | 0.32 | 5.69 | cycle-heavy (Ir understates) |
| 14 | `RINvNtNtCs8jNs2MqwBKX_16revm_interpreter12instructions8contr` | 1.75 | 1.65 | 1.06 | aligned |
| 15 | `llvm` | 3.71 | 0.70 | 2.39 | cycle-heavy (Ir understates) |
| 16 | `glibc::_int_malloc` | 1.25 | 2.26 | 0.55 | instr-heavy (Ir overstates) |
| 17 | `RNvMs1_NtCs8jNs2MqwBKX_16revm_interpreter11interpreterNtB5_1` | 0.90 | 0.71 | 1.27 | cycle-heavy (Ir understates) |
| 18 | `glibc::__memmove_avx512` | 0.90 | 0.69 | 1.30 | memory-bound-ish |
| 19 | `RINvMs2_NtCs5GT9zvB7TYh_12revm_handler5frameNtB6_8EthFrame19` | 0.87 | 1.01 | 0.86 | aligned |
| 20 | `sstore*` | 1.56 | 0.35 | 2.34 | cycle-heavy (Ir understates) |
| 21 | `RINvNtNtNtCs40yuATWtwNz_8mega_evm3evm12instructions15storage` | 0.79 | 0.29 | 2.72 | cycle-heavy (Ir understates) |
| 22 | `RNvXNtNtCs40yuATWtwNz_8mega_evm3evm4hostINtNtB4_7context11Me` | 1.43 | 0.63 | 1.22 | aligned |
| 23 | `_int_free                                                                       ` | 0.76 | 0.93 | 0.82 | aligned |
| 24 | `RINvNtNtNtCs40yuATWtwNz_8mega_evm3evm12instructions17volatil` | 0.76 | 0.33 | 2.30 | cycle-heavy (Ir understates) |
| 25 | `malloc                                                                          ` | 0.69 | 0.70 | 0.99 | aligned |

机读: `analysis.json` → `divergence_top30.sweep_hotloop_v2`

**辅探针**: Lane1 IPC=2.823（keccak/HashMap/sstore）；Lane2 IPC=3.366，**keccak ~49.6% cycles vs ~2.6% instr**（极端 cycle-heavy）。

---

## 5. Ir 看不见但周期质量大的面（排序）

1. **foldhash::hash_bytes_long [branch-miss]** — 40.6% of branch-miss samples 可行性：可行：状态/哈希路径专项 lane；需 DIFF+周期验收，非纯 Ir。
2. **RINvXs0_NtNtNtCsjOiS4846Slk_4core4iter8adapters3mapINtB6_3Ma [branch-miss]** — 11.2% of branch-miss samples 可行性：需分支/布局优化；Ir 门无法验收。
3. **hashbrown::rustc_entry** cyc/instr=4.90 — Ir understates wall-time share 可行性：可行：状态/哈希路径专项 lane；需 DIFF+周期验收，非纯 Ir。
4. **HashMap::get_mut** cyc/instr=9.02 — Ir understates wall-time share 可行性：可行：状态/哈希路径专项 lane；需 DIFF+周期验收，非纯 Ir。
5. **glibc::_int_malloc [branch-miss]** — 9.0% of branch-miss samples 可行性：可行：减 LOG/分配/拷贝；正确性门严格。
6. **foldhash::hash_bytes_long [L1-icache-miss]** — 11.0% of L1-icache-miss samples 可行性：可行：状态/哈希路径专项 lane；需 DIFF+周期验收，非纯 Ir。
7. **revm::host::log [branch-miss]** — 8.6% of branch-miss samples 可行性：可行：减 LOG/分配/拷贝；正确性门严格。
8. **llvm** cyc/instr=2.39 — Ir understates wall-time share 可行性：需 call-trace editable + 周期验收。
9. **EthFrame::init_with_context [L1-icache-miss]** — 8.4% of L1-icache-miss samples 可行性：解释器控制面，改动面大，宜专项设计。
10. **MegaHandler::run_without_catch_error [L1-icache-miss]** — 7.4% of L1-icache-miss samples 可行性：解释器控制面，改动面大，宜专项设计。
11. **EthFrame::init_with_context** cyc/instr=1.80 — Ir understates wall-time share 可行性：解释器控制面，改动面大，宜专项设计。
12. **glibc::__memmove_avx512 [L1-icache-miss]** — 6.8% of L1-icache-miss samples 可行性：可行：减 LOG/分配/拷贝；正确性门严格。

### 判官扩展含义

1. 仅 Ir 足以抓 push1 类包装热（与周期同量级）。  
2. 要看见墙钟必须扩：hashbrown/HashMap、foldhash、LOG→malloc、frame init（div≫1 或 miss 主导）。  
3. 分支 miss 全局仅 0.093%——优先数据面哈希/分配，非派发表。  
4. page fault 稳态 0，可从噪声模型降权。

---

## 6. 局限

HW instructions ≠ Callgrind Ir；短探针采样方差更大；demangle 启发式；与 CodSpeed CI codegen 可能有细差——本单自洽于同机同二进制。

---

## 7. 产物路径

| 路径 | 内容 |
|---|---|
| `docs/mega-evm-hwcounters-cycle-ir-divergence-20260724.md` | 本报告 |
| `docs/data/mega-evm-hwcounters-20260723/env-fingerprint.txt` | 环境 |
| `docs/data/mega-evm-hwcounters-20260723/stat/**` | 5 轮 stat |
| `docs/data/mega-evm-hwcounters-20260723/record/**` | perf.data + 符号报告 |
| `docs/data/mega-evm-hwcounters-20260723/analysis.json` | 汇总 |
| `docs/data/mega-evm-hwcounters-20260723/SHA256SUMS` | 清单（不含 37MB .dbg） |
| `docs/data/mega-evm-hwcounters-20260723/bin/SHA256SUMS.dbg` | .dbg 校验（二进制留主机） |

---

## 8. 四件套自证

1. **清理**: 无残留 perf/aro 测量进程；`perf.data` 归档于 evidence 树。  
2. **凭证**: 本单不落盘 PAT；推送用临时 askpass。  
3. **身份**: 推 RealiCZ/aro 用 **aro 凭证**，不用 mega-putin。  
4. **megaeth-labs 零写入**: 仅本地 worktree 编译，无远端写。

*自然停点：报告落盘 + 推送 `server/mega-evm-hwcounters`。*
