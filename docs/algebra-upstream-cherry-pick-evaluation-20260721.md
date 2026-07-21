# Arkworks 上游热路径 cherry-pick 评估与排序 pick list（2026-07-21）

> 结论：**只建议把 `a6ee3a9b`（去 `% N`）进入下一阶段隔离 backport/certify；MSM 架构栈暂缓；GLV 修复对 Salt Bandersnatch 不适用。** 本工单没有执行 cherry-pick，没有改生产分支。

## 1. 范围与基线

- MegaETH consumer pin：`80ca69c37f79d5d00750edc1602af81b5f456695`；与 upstream base `da450f98b9b4bf1b4c8eec8f96b4501f9705c517` 的 fork delta 是 9 个 crate root、18 行新增，均为 coverage cfg。
- 本地测量 baseline：`01b20e377460e7af9da069b0c96f2d1158a7b974`。
- upstream 最新：`e341a1a115a65390e5b66c6d41ea0b159d3c7ee9`（2026-07-06）；从 base 起 55 commits。
- 范围：`ark-ff` Montgomery、`ark-ec` VariableBaseMSM/GLV、Bandersnatch/TE。

## 2. 排序后的 pick list

| 排名 | 候选 | 结论 |
|---:|---|---|
| 1 | `a6ee3a9b88058af37905dc462ce91ed2074a241c` 去 `% N` | **进入下一阶段隔离 backport/certify**；低移植成本，现有 MSM probe 粗测正收益 |
| 2 | `da611a3c` + `104444d9`（依赖 `2c4a6950` 和中间 EC API） | **暂缓**；Bandersnatch probe 粗测回归，stack 耦合/共识风险高 |
| 3 | `2c4a6950` XYZZ/Extended Jacobian | 不单独 pick；对 TE Bandersnatch 无直接坐标收益 |
| 4 | `65f9aa25` GLV 修复 | 不为 Salt pick；Bandersnatch 无 `GLVConfig` |
| 5 | `8de9a9d9` dead spill-buffer cleanup | 无运行时收益，不列性能 pick |

## 3. 四字段评估

### 3.1 `a6ee3a9b` — remove useless modular operation (#982)

- **适用性：高。** 修改 `MontBackend::into_bigint` 的 limb 索引；VariableBaseMSM 将 scalar 转 bigint 时命中，Salt 序列化/证明路径也使用 field conversion。
- **移植成本：低。** 单文件、单表达式；fork 18 行 delta 只在 crate root，补丁可直接 apply-check。
- **风险评级：中低。** 域表示属共识关键，但循环保证 `i < N`，所以 `i % N == i`。`ark-ff` 52 unit + 42 doc tests 全过；MSM differential fingerprint 与 baseline 相同：`d6ca53e6...31b8`。
- **预估收益：正向但仅粗测。** MSM 轮中位数从 `2.724 ms` 到 `2.104 ms`，延迟 `-22.755%`，候选轮 MAD `0.008 ms`。数值可能含编译布局/主机噪声，不能外推端到端；下一步需隔离 backport 后做 Salt E2E counterbalanced。

### 3.2 `65f9aa25` — GLV performance fix (#1025)

- **适用性：无（Salt 当前曲线）。** 变更位于 SW `GLVConfig`；Bandersnatch 是 twisted Edwards，源码无 `impl GLVConfig`。
- **移植成本：低。** 核心 scalar bit-length/zero handling 与 fork delta 不重叠。
- **风险评级：中。** 改变标量乘控制流；若用于别的 GLV 曲线，最低证据应是 GLV 与普通 scalar multiplication 的随机等价测试。
- **预估收益：Salt 为 0。** 现有 probe 无可达 GLV 路径，未制造无意义数字。

### 3.3 `2c4a6950` — Extended Jacobian/XYZZ for SW MSM (#961)

- **适用性：低（直接）/中（作为 stack 依赖）。** 新 bucket 坐标针对 short-Weierstrass；Salt 的 Bandersnatch 是 TE。
- **移植成本：中。** 约 500 行 EC API/坐标改动，且后续 MSM stack 依赖该 bucket 架构。
- **风险评级：高。** 新群运算公式属共识关键；隔离 worktree 的 ark-ec、Bandersnatch tests 和 differential probe 通过，但仍需独立公式审计/property/fuzz。
- **预估收益：对 TE 无可信正收益。** 上游 PR 的 15–18% 是 BLS/BN SW MSM，不能外推 Bandersnatch；不单独 pick。

### 3.4 `da611a3c` + `104444d9` — small-scalar MSM + cleanup (#995/#996)

- **适用性：中。** 通用 VariableBaseMSM 覆盖 TE；Salt IPA scalars 混合 transcript full-width 与 domain index/小整数，只对小标量桶受益。现有 256-base probe 使用约 40-bit scalars。
- **移植成本：高。** 最小相关路径 backport 首次编译因缺少中间 `AffineRepr::ZERO` API 失败；粗测需采用截至 `104444d9` 的完整 EC diff。成本来自 upstream 依赖栈，不来自 fork 18 行 delta。
- **风险评级：高。** 改变 scalar 分桶、signed/unsigned 路径和数据结构；ark-ec、Bandersnatch tests 与 differential fingerprint 通过，但无形式证明/fuzz/E2E certify。
- **预估收益：当前 probe 回归。** baseline `2.724 ms`，stack `2.924 ms`，延迟 `+7.344%`，stack 轮 MAD `0.348 ms`。上游 PR 对随机 scalar 也报告约 `+0.6%..+1.1%` 回归，而小 scalar 报告 43–90% 改善。当前不足以 pick。

### 3.5 `8de9a9d9` — dead spill buffer cleanup (#1039)

- **适用性：无运行时收益。** 修改 `ff-macros` dead code。
- **移植成本：低。** 约 56 行清理，fork delta 不冲突。
- **风险评级：低到中。** 宏生成代码仍属域算术供应链；需 generated-code snapshot + field tests。
- **预估收益：0。** 不列入性能 pick。

## 4. 粗测留痕

- disposable detached worktrees；用 `git diff | git apply` 构造临时测量树，**未 cherry-pick、未 commit**。
- AMD EPYC 9754；16 物理核 affinity；`RAYON_NUM_THREADS=16`；scale 8。
- 每 variant 5 轮×7 samples；15-run 平衡顺序。原始数据：`docs/data/algebra-upstream-msm-rough-20260721.csv`。

| Variant | 5 个轮中位数（ms） | 中位数 / MAD（ms） | 相对 baseline 延迟 |
|---|---|---:|---:|
| baseline | `1.910, 3.082, 2.845, 2.611, 2.724` | `2.724 / 0.121` | `+0.000%` |
| 去 `% N` | `2.097, 2.092, 2.888, 2.109, 2.104` | `2.104 / 0.008` | `-22.755%` |
| MSM stack | `3.342, 2.924, 3.175, 2.473, 2.577` | `2.924 / 0.348` | `+7.344%` |

正确性：`a6ee3a9b` 的 ark-ff tests 与 MSM differential 通过；`2c4a6950`/MSM snapshot 的 ark-ec、Bandersnatch tests 与 differential 通过。粗测不替代未来 backport 的 Salt E2E certify。

## 5. 自然停点与下一步

- 自然停在排序 pick list：没有 cherry-pick 到 fork，没有生产配置变更，没有 PR。
- 若获批下一阶段：只从排名 1 的 `a6ee3a9b` 开始，做隔离 backport、完整 correctness 和 Salt E2E counterbalanced；通过后再决定 MSM stack。
- MSM stack 在补齐真实 scalar bit-length 分布 probe 前不进入集成。

## 6. 知识源

- 主源：`viking://resources/mega-agents/knowledge/repo-references/Repo_Digest_salt/Repo_Digest_salt/Tech_stack_6more_1d83f76a.md`。
- 主源：`viking://resources/mega-agents/knowledge/Stateless_Validation/Stateless_Validation/SALT_Trie_and_IPA_Commitments/Witness_Decode_Hot_Path.md`。
- Feishu 次源没有上述 commits 的算术证据；commit/PR、适用性、测试和数值以 upstream Git、fork 源码和实测为准。
