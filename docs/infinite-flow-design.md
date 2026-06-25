# ARO 无限流深搜 — 设计方案(v1,待 review)

> 把探索器从「贪心 · 1 轮就走」升级为「**并行多 agent 深搜 · 跑完出完整决策树**」,在**不动 judge(moat)**的前提下,把搜索做深、做全、做并行。本文给出原则、架构、逐项机制、不变量、代价、CLI、落地阶段,末尾列出**待你拍板的开放问题**。

---

## 0. 背景:为什么改

现在每个热函数只出 **1 个候选**,within-noise 就走(`rounds_per_fn=1`、`dry_rounds=1`)。决策树里那一堆**紫色「未试」**(agent reflect 提出却没轮到试的 d1/d2/d3)就是被这个浅搜索砍掉的真实想法。结论(无限算力讨论):**搜索是 commodity,judge 是 moat;该松的是「放弃阈值」,该守死的是 judge + 覆盖。** 所以把搜索放开、judge 守住。

---

## 1. 目标 / 非目标

**目标**
- **深度**:多轮 + reflect 喂回下一轮,连试几轮不过才判不可行(不再「估一下就放弃」)。
- **并行**:多 agent **并行生成**多样候选(不同 lens / 框架)。
- **穷尽**:遍历完整前沿 + 升级 tried/gated,不在第 3 个 dry 就缩。
- **可视**:跑完**自动**出 `decision-tree.html`。
- **自治**:within-regime 全自动,人只在真·regime 门拍板。

**非目标(守住,别动)**
- judge 全套(Gate 0/1/2)、crypto/base 不可碰、通用性(无特例,走 cargo metadata)、**bench 测量完整性**。

---

## 2. 核心原则(决定架构的三条)

1. **judge 是 moat,搜索是 commodity** → 放开搜索,守死 judge。
2. **并行生成,串行判分** —— 生成(写候选)可随便并行;**bench 必须串行**。并行 bench 会互抢 CPU/cache/触发降频 → 噪声地板爆 → 没有赢能被证明 = **毁 moat**。这是架构的根。
3. **人门只留两处真·regime 决策**:① 放宽 oracle(接受 should-not-merge)② 换 workload(需领域判断什么算代表性)。其余(换函数 / 爬 lens / 多轮 / re-profile / 叠加)全自治。
4. **【token 无限 → 瓶颈在 judge】** 既然生成不要钱,就可以无限 fan-out;但所有候选都得排队过**那条串行 bench**。于是**串行 judge 吞吐成了唯一瓶颈** —— 这又一次印证"judge 是承重墙"。直接后果(见 §4.3b):必须加**廉价预筛 + 判分队列优先级**,否则无限的垃圾候选会淹没稀缺的串行判分。**「无限」是说生成无限,不是说判分无限;wall-clock 由串行 judge 定。**

---

## 3. 架构

```
                 ┌─ agent(lens=消除冗余) ─┐
   前沿队列  →   ├─ agent(lens=数据布局) ─┤   并行 fan-out 生成
 (函数×lens×     ├─ agent(lens=算法重写) ─┤   墙钟 = 1 个 agent 的时间
  reflect)       └─ agent(框架=风险优先) ─┘   → N 个候选 patch
                                                      │
                                                      ▼
                            ┌──────── 单条串行判分队列 ────────┐
                            │  每候选:Gate0 守卫 → Gate1 正确性  │   隔离 worktree
                            │  (build+test+differential) →       │   bench 绝不并行
                            │  Gate2 显著性(A/A+A/B+CI+地板+自紧)│
                            └────────────────┬──────────────────┘
                                             ▼
                       judge 挑最好(direction-aware)→ accept?
                          accept → 叠基线 → re-profile → 回前沿
                          全 dry → 升级 tried/gated → 穷尽则 STOP
                                             ▼
                          收尾:decision-tree.html + trajectory.png
```

- **并发只在生成**(aro 进程内线程池 spawn `claude -p`,非 harness subagent → 无挂起问题)。
- **判分是单消费者**(测量整洁性),阶段 2 升级成 producer-consumer。

---

## 4. 逐项机制设计

### 4.1 深度:多轮 + reflect 喂回 + lens 阶梯 — 阶段 1
- `rounds_per_fn` 1 → **4~6**;per-fn `dry_rounds` 1 → **3**。run_backtest 已把 agenda(reflect 方向)喂进下一轮 `GenContext`,只需放开轮数,d1/d2/d3 就会被**真的逐个试**。
- **lens 阶梯**:`lens_depth = f(本函数已 dry 轮数)`,注入 prompt —— round1 微消除 → 没过爬到数据布局 → 再没过爬到算法级。
- 触点:`spec.run.stop` / `--rounds-per-fn`;`generator.py` + `prompts/agentic.md`(加 `$lens_depth`)。

### 4.2 并行多 agent 生成 — 阶段 1(核心)
- `AgenticGenerator.propose(ctx, N)`:用 `ThreadPoolExecutor` **并发 spawn N 个 `claude -p`**,各带**不同 lens / 框架**的 prompt → 收 N 个候选 patch。
- 并发上限(默认 `min(N, 8)`);单个 agent 挂 → 丢该候选,不影响其余(`.filter(Boolean)` 思路)。
- engine 仍**串行判分**这 N 个,judge 挑 direction-aware 最优。
- 墙钟:从 N×agent 降到 ≈1×agent(最慢一环并行化)。
- 触点:`generator.py` 的 `propose`;engine 已支持 `candidates_per_round=N`。

### 4.3 串行判分(不变 + 可强化)
- 每候选 Gate0/1/2 **串行**、隔离 worktree、bench 绝不并行(不变量,见 §6)。
- token 无限下:默认就开**更高 `bench_scales` / 更多 `ab_pairs` / 更多 `aa_runs`** → 地板压到极低,`noise-limited` 几乎消失,小赢也能分辨;每个 accept 再走**对抗复核**(§4.7)。

### 4.3b 廉价预筛 + 判分队列优先级 — 阶段 1(token 无限下新增的必做项)
> 生成无限 → 串行 judge 是瓶颈 → **不能让垃圾候选白占串行 bench**。在昂贵的 A/A+A/B 之前加一道**廉价闸**,并给判分队列排优先级。
- **廉价预筛(秒级,可并行)**:① 能 build?② patch 与 baseline **是否真的不同**(纯格式化/等价改 → 丢)③ **一次性快速 smoke bench**(单次、低样本)估个粗 Δ。三关任一不过 → 不进串行判分队列。
- **去重**:把生成的 N 个候选按"改了哪几行 / AST 形状"**去重**,等价候选只判一次。
- **优先级**:串行 bench 队列按 smoke-Δ 从大到小判 —— 最像赢的先上,稀缺的判分时间不浪费在没希望的候选上。
- 触点:`engine.py` 判分前加 prescreen;`generator` 产候选后去重。

### 4.4 穷尽前沿(不早停) — 阶段 1
- `_explore_decision`:**去掉「`dry_streak≥3` 跨函数就停」**(那是省钱逻辑)。改成:遍历完整前沿 → 升级 tried/gated → 只在 **headroom ≤ 阈值**(真没可达)或 **预算到顶** 停。
- per-fn:穷尽 lens 层 + reflect 方向才判这函数不可行。
- 触点:`sweep.py` `_explore_decision` / `attempt` 主循环。

### 4.5 收尾自动出决策树 — 阶段 1
- `sweep.main` 的 `--attempt` 分支收尾:调 `tree.build_tree` → 写 `decision-tree.html`;`trajectory.svg → trajectory.png`。
- 触点:`sweep.py` `main()` 收尾几行。

### 4.6 跨函数并行 + 单串行 bench 队列(producer-consumer) — 阶段 2
- 整条前沿的 (函数×lens×reflect) 一起喂 agent 池**并行生成**;所有候选汇到**同一条串行 bench 队列**判分。这是真·无限流的完整形态。大改,单列。

### 4.7 双 regime / 对抗复核 / 自动多 workload — 阶段 2
- **双 regime**:`--allow-relaxed` 开关,自动也打 gated,relaxed 赢标 should-not-merge。
- **对抗复核**:accept 后 fan out N 个 skeptic 重验(重 bench / 重 differential / 反向举证),扛过才算。
- **自动多 workload**:合成一批负载覆盖不同行为路径,profile 并集 —— **最大件,建议单独立项**。

---

## 5. 停机条件(更新)

**token 无限 → 只剩两个真停机条件(预算不再是理由):**

| 触发 | 阈值 | 说明 |
|---|---|---|
| **可达枯竭** | `addressable headroom ≤ headroom_min`(默认 2%) | 没有自家·可定位·够热的函数 |
| **真穷尽** | 所有 函数×lens×reflect 都判过,无新赢 | 走完整棵决策树 |
| ~~预算到顶~~ | (默认关) | token 无限 → 取消;`--max-attempts` 仅作**可选安全阀**,默认不限 |

> 关键变化:**停机从「成本性递减(dry_streak≥3 省钱停)」变成「可证穷尽」** —— 无限流走完整棵树,只在真没东西可打时停。

---

## 6. 不变量(任何改动都不能破)

1. **bench 串行** —— 永远不并行测量。
2. **写手不自评** —— Gate0 守卫,绝不编辑 `Cargo.toml`/lock、`benches/`、`tests/`。
3. **正确性先于显著性** —— build + test + 随机输入 differential 全过,才测 A/B。
4. **数字 verbatim** —— 报告/树只读 events.jsonl,不二次判分。
5. **通用性** —— owner/定位走 cargo metadata,不堆特例。

---

## 7. 代价与权衡(token 无限版)

- **token 不是约束**(按你定):生成尽管 fan-out。**真正的瓶颈 = 串行 judge 吞吐**(§2.4)。所以 §4.3b 的预筛/去重/优先级**不是优化,是必做** —— 它决定那条稀缺串行 bench 花在哪。
- **wall-clock**:由"进串行队列的候选数 × 单次 bench 时间"定。预筛把多数垃圾挡在队列外 → wall-clock 可控,即便生成无限。
- **风险**:深搜爬到算法级 → 更多结构改 → 更多 `verify-failed`/`build-failed`,但**全由 judge 兜**,不污染结论。
- **CPU**:生成(等网络/IO)不占 bench 资源;判分串行独占,测量干净。

---

## 8. 配置 / CLI(新增/改)

| 参数 | 默认(token 无限) | 作用 |
|---|---|---|
| `--exhaustive` | **on(默认)** | 穷尽前沿,去掉省钱式 dry-stop |
| `--fanout N` | **大(每个 lens×框架 各一个)** | 每轮并行生成的候选数;token 无限 → 尽量铺满 |
| `--gen-concurrency` | **16** | 生成 agent 并发上限(纯网络/IO) |
| `--rounds-per-fn` | **不限**(到穷尽) | 每函数轮数;靠 reflect 喂回逐轮深入 |
| `--dry-rounds` | **3** | 连续几轮无新方向才判该函数穷尽 |
| `--prescreen` | **on** | §4.3b 廉价预筛(build+differs+smoke)+ 去重 + 队列优先级 |
| `--max-attempts` | **不限**(可选安全阀) | 只在你想兜底时设 |
| `--allow-relaxed` | off | 开放 relaxed regime(should-not-merge);**因正确性 gated,非成本**,默认仍关 |

---

## 9. 落地阶段

- **阶段 1 ✅ 已落地(本轮)**:4.1 深度(lens 阶梯 micro→layout→algorithm + rounds_per_fn 4 + per-fn dry 3)+ 4.2 并行生成(`AgenticGenerator/RalphGenerator` 线程池 fan-out N 个不同 lens 候选,`--gen-concurrency` 封顶,各候选独立 worktree/CARGO_TARGET_DIR,id 不撞)+ 4.3b 预筛(`eval.dedup_candidates` 去重 + `eval.prescreen` build+smoke 廉价闸 + `engine` 判分队列按 smoke-Δ 排序,掉队候选照记不静默)+ 4.4 穷尽(`_explore_decision(exhaustive=True)` 去掉省钱式 dry-stop,只剩 headroom 枯竭 + 前沿真穷尽;`--max-attempts` 变可选安全阀)+ 4.5 自动决策树(`_finalize_run` 收尾自动出 `decision-tree.html` + `trajectory.png`)。selftest #21 覆盖,全 21 例过。
  - CLI:`aro sweep <spec> --attempt --diverge [--fanout N] [--gen-concurrency N] [--prescreen/--no-prescreen] [--exhaustive/--no-exhaustive] [--dry-rounds N] [--rounds-per-fn N]`。
- **阶段 2**:4.6 producer-consumer 全异步(跨函数并行生成 → 单串行 bench 队列)+ 4.7 双 regime / 对抗复核。
- **单独立项**:4.7 自动多 workload(覆盖轴)。
- **配套穿插**:例子库 `optimization-examples.md`、region 扩到直接 callee、文档可读性。

---

## 10. 待你 review 拍板的开放问题

> 已定:**token 无限** → ① fanout 尽量铺满、② `--exhaustive` 默认开、③ 取消预算停机(`--max-attempts` 仅可选安全阀)、④ 新增 §4.3b 廉价预筛(因 judge 成瓶颈,必做)。

**剩下要你拍板的:**
1. **§4.3b 预筛**这轮就做(我认为必须,否则无限候选淹没串行 judge)—— 同意把它并进阶段 1 吗?
2. **dry_rounds=3 / lens 3 层** 作为"穷尽"的判据合理吗?(穷尽 = 连 3 轮 reflect 提不出新方向)
3. **region 扩到直接 callee**(深度结构改要跨同 crate 几个 callee)—— 松紧如何?(绝不放 Cargo/bench/test)
4. **双 regime(`--allow-relaxed`)+ 对抗复核**:这轮就上,还是留阶段 2?(注:它们 gated 是为**正确性**,不是成本 —— 即便 token 无限,放宽 oracle 仍是改变"赢的种类",建议仍人门 opt-in)
5. **自动多 workload** 确认单独立项?
6. **gen 并发上限 16** 够吗?(机器/网络能撑多少并发 `claude -p`)
