# Salt 实际构建下 ADX/BMI2 counterbalanced 决策报告（2026-07-21）

> 结论：**不建议现在修改 Salt/Algebra 生产配置。** Witness 面给出稳定收益（配对延迟中位数 `-9.111%`，5/5 对均更快），但 Field、MSM、state-update 未形成同方向决策证据。若后续做 witness 专用 canary，优先最小旗标 `-C target-feature=+adx,+bmi2`；不要把 `-C target-cpu=x86-64-v3` 当作等价方案，因为本工具链的 v3 cfg **包含 BMI2 但不包含 ADX**。

## 1. 决策摘要

| 测量面 | 配对延迟变化（ON 相对 OFF） | 判定 |
|---|---:|---|
| Field micro | `+7.734%`，MAD `3.231` pp，ON 更快 1/5 | ON 反而较慢，否定全局无条件启用 |
| MSM micro | `-9.158%`，MAD `17.422` pp，ON 更快 3/5 | 方向噪声大，不可拍板 |
| state-update | `+2.789%`，MAD `1.121` pp，ON 更快 2/5 | 轻微回归/不确定 |
| witness | `-9.111%`，MAD `2.575` pp，ON 更快 5/5 | 唯一决策级正信号；95% bootstrap 区间 `-15.253%..-4.355%` |

正数表示 ON 延迟更高（回归），负数表示 ON 更快。主判官是相邻 AB 的逐对百分比中位数；不使用简单全局均值。

## 2. B 类环境留痕

- Salt consumer：`19419f4d13e6c615b7a94cf3d2bf53d1052f723c`。
- Algebra consumer pin：`80ca69c37f79d5d00750edc1602af81b5f456695`；本地测量 baseline：`01b20e377460e7af9da069b0c96f2d1158a7b974`。后者在 pin 之上仅含 ARO probe/profile 测量支撑，不改域算术。
- 构建入口：从 Salt workspace 构建；micro probe 临时安装到 `banderwagon/examples/`，端到端 probe 临时安装到 `salt/examples/`。不是 Algebra standalone micro 环境。
- Path patch：Cargo CLI `--config patch...path=...` 指向 detached 本地 Algebra baseline；metadata 验证 `ark-ff`、`ark-ec`、`ark-serialize`、`ark-poly`、Bandersnatch curve 均为本地 path source。
- Salt release profile：`opt-level=3`、Thin LTO、`codegen-units=1`、`panic=abort`、`strip=debuginfo`、`overflow-checks=false`、`debug=0`。
- 工具链：`nightly-2026-03-20`，`rustc 1.96.0-nightly`，LLVM `22.1.0`。
- 主机：AMD EPYC 9754；32 vCPU / 16 物理核；CPU flags 含 ADX、BMI2。
- 固定运行：`taskset -c 0,2,...,30`，`RAYON_NUM_THREADS=16`，`ARO_BENCH_SCALE=8`。
- OFF：`--check-cfg=cfg(coverage_nightly) -C target-feature=-adx,-bmi2`。
- ON：`--check-cfg=cfg(coverage_nightly) -C target-feature=+adx,+bmi2`。
- 每测量面 ON/OFF 各 5 轮，每轮 7 样本；严格 ABAB。Field/state-update 以 OFF 起步，MSM/witness 以 ON 起步；每个二进制正式计时前预热一次。
- state-update 使用既有 harness 要求的 `test-bucket-resize` feature；witness 使用默认 Salt feature。
- 长测前完成 4×30 秒静默检查。

### 2.1 旗标与指令路径验证

- `rustc --print cfg`：OFF 无 `adx`/`bmi2`；ON 同时有 `adx`/`bmi2`。
- Micro 二进制：OFF 的 `mulx` 均为 0；ON 的 Field/MSM 分别出现 `193` / `134` 个 `mulx`，直接证明 patched ark-ff 路径切换。
- 端到端二进制包含其他依赖自带汇编，OFF 已有 ADX/BMI2 opcodes；ON 的 state-update `mulx` 从 `320` 增到 `1045`，witness 从 `320` 增到 `2164`。只把增量用作路径证据，不把总计数当性能判官。

## 3. 逐轮中位数与 MAD

单位均为 ms；每轮 7 样本。

### 3.1 Field micro

| 模式/轮次 | 中位数 | MAD |
|---|---:|---:|
| OFF-1 | `0.330031` | `0.044488` |
| OFF-2 | `0.281767` | `0.002870` |
| OFF-3 | `0.335687` | `0.011524` |
| OFF-4 | `0.354947` | `0.051740` |
| OFF-5 | `0.400865` | `0.114951` |
| ON-1 | `0.358269` | `0.086673` |
| ON-2 | `0.386243` | `0.066682` |
| ON-3 | `0.309032` | `0.014441` |
| ON-4 | `0.382397` | `0.087574` |
| ON-5 | `0.418915` | `0.102892` |

- OFF 轮中位数的中位数/MAD：`0.335687 / 0.019260 ms`。
- ON 轮中位数的中位数/MAD：`0.382397 / 0.024128 ms`。
- 逐对延迟变化：`+8.556%, +37.079%, -7.940%, +7.734%, +4.503%`。

### 3.2 MSM micro

| 模式/轮次 | 中位数 | MAD |
|---|---:|---:|
| OFF-1 | `2.182735` | `0.195181` |
| OFF-2 | `2.878701` | `0.381568` |
| OFF-3 | `2.115103` | `0.226264` |
| OFF-4 | `2.349645` | `0.514188` |
| OFF-5 | `3.168995` | `0.465780` |
| ON-1 | `3.099600` | `0.450168` |
| ON-2 | `2.474750` | `0.431078` |
| ON-3 | `2.377896` | `0.433102` |
| ON-4 | `2.134467` | `0.484321` |
| ON-5 | `2.326695` | `0.490032` |

- OFF 轮中位数的中位数/MAD：`2.349645 / 0.234542 ms`。
- ON 轮中位数的中位数/MAD：`2.377896 / 0.096854 ms`。
- 逐对延迟变化：`+42.005%, -14.032%, +12.425%, -9.158%, -26.579%`。

### 3.3 state-update

| 模式/轮次 | 中位数 | MAD |
|---|---:|---:|
| OFF-1 | `56.333374` | `3.619481` |
| OFF-2 | `62.111657` | `3.346916` |
| OFF-3 | `58.700037` | `2.479464` |
| OFF-4 | `55.507753` | `2.250005` |
| OFF-5 | `59.951321` | `3.671323` |
| ON-1 | `58.535577` | `1.187356` |
| ON-2 | `57.273375` | `1.496801` |
| ON-3 | `55.214570` | `3.652087` |
| ON-4 | `57.070804` | `2.194944` |
| ON-5 | `61.623138` | `6.230682` |

- OFF 轮中位数的中位数/MAD：`58.700037 / 2.366663 ms`。
- ON 轮中位数的中位数/MAD：`57.273375 / 1.262201 ms`。
- 逐对延迟变化：`+3.909%, -7.790%, -5.938%, +2.816%, +2.789%`。

### 3.4 witness

| 模式/轮次 | 中位数 | MAD |
|---|---:|---:|
| OFF-1 | `86.288651` | `8.615543` |
| OFF-2 | `95.004065` | `9.290978` |
| OFF-3 | `84.889086` | `4.914158` |
| OFF-4 | `97.956404` | `9.859808` |
| OFF-5 | `96.061493` | `8.017203` |
| ON-1 | `78.426877` | `4.091543` |
| ON-2 | `87.508523` | `5.483068` |
| ON-3 | `81.192356` | `2.249231` |
| ON-4 | `83.015508` | `3.213606` |
| ON-5 | `84.835582` | `0.812178` |

- OFF 轮中位数的中位数/MAD：`95.004065 / 2.952339 ms`。
- ON 轮中位数的中位数/MAD：`83.015508 / 1.823152 ms`。
- 逐对延迟变化：`-9.111%, -7.890%, -4.355%, -15.253%, -11.686%`。

原始 280 个样本：`docs/data/algebra-adx-bmi2-salt-counterbalanced-20260721.csv`。完整中间证据在 `.aro-runs/algebra-adx-bmi2-salt-counterbalanced-20260721/`。

## 4. 为什么墙钟是正确判官

本次改变目标指令集和真实机器上的吞吐/延迟。Callgrind `Ir` 无法可靠表达 `mulx`、carry chain、端口占用、流水线和目标 CPU 调度差异。因此本工单用固定 CPU/线程/顺序的墙钟 counterbalanced 数据拍板；Ir 仅可作结构辅助。

## 5. 旗标方案与部署约束

### 5.1 最小面：`-C target-feature=+adx,+bmi2`

- 优点：只打开 ark-ff 所需两个 compile-time cfg，变化面最小，最适合归因和 canary。
- 约束：所有执行节点都必须有 ADX 和 BMI2，否则可能非法指令。当前 EPYC 9754 满足，但必须另做全 fleet CPUID 盘点。
- 建议：仅在同构硬件的 witness 专用 artifact canary；不要全局覆盖所有 Salt 路径。

### 5.2 `-C target-cpu=x86-64-v3`

- 当前 Rust cfg 实测：v3 带 `avx/avx2/bmi/bmi2/fma`，但**不带 ADX**，所以不能单独启用 ark-ff 的 `all(bmi2, adx)` 路径。
- `-C target-cpu=x86-64-v3 -C target-feature=+adx` 才覆盖本路径，但同时扩大到 AVX2/FMA/BMI 等更广 codegen，归因、兼容和频率行为更复杂。
- `target-cpu=native` 不适合可重复发布。混合 fleet 应使用分 artifact/调度约束或显式运行时多版本分派。

## 6. 决策与局限

1. **本轮不改生产配置。** Witness 支持下一步专用 canary，但不足以支持全局旗标。
2. 如另开实施单，优先 witness-only、同构硬件、最小 `+adx,+bmi2`；部署前需 CPUID gate、回滚 artifact 和线上墙钟/CPU 指标。
3. Field 有回归方向；MSM 方差过大；state-update 的配对结果与简单模式聚合方向相反，应以配对结果为主。
4. 局限：单一 EPYC 9754、每模式 5 轮、确定性 probe；未覆盖 Intel、真实 fleet 争用、线上输入分布与长期温度。
5. 未修改 Salt/Algebra 生产配置，未创建 megaeth-labs PR。临时 worktree 的 Cargo.lock 因 path patch 发生预期变化并随 worktree 删除；原 tracked tree 未改变。

## 7. 知识源

- 主源：`viking://resources/mega-agents/knowledge/repo-references/Repo_Digest_salt/Repo_Digest_salt/Tech_stack_6more_1d83f76a.md`。
- 主源：`viking://resources/mega-agents/knowledge/Stateless_Validation/Stateless_Validation/SALT_Trie_and_IPA_Commitments/Witness_Decode_Hot_Path.md`。
- Feishu 次源未返回 ADX/BMI2 专项事实；旗标与数值以仓库源码、实际构建和原始数据为准。
