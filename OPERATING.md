# ARO 操作手册

怎么跑、怎么接新目标、输出怎么读。架构与循环协议见 `skill/SKILL.md`;无人值守(agent 自己定位+写 probe)见 `skill/references/autonomous-optimization.md`。

## 0. 心智模型

ARO 是一个**目标驱动的循环**:观察热点 → 读懂代码出计划 → agentic 写-编-修实现 → **判分**(正确性 + 显著性)→ 写记忆 → **反思出下一步研究方向(agenda)** → 直到达标或收益枯竭。

- **薄的、prompt 驱动**:编排、生成、读代码、每个目标的知识(spec)。
- **小的确定性核(被执行的代码,`aro/`)**:判分(`eval`)、统计(`stats`)、防作弊(`guard`)、测量协议。**这部分必须执行、不能用 prompt 推**——写代码的不能自评、统计要可复现、判定要骗不过。这是护城河。

**接一个新仓库 = 写一份 spec(`targets/*.json`),不写代码。** 循环对所有目标一样。

## 1. 前置

- Python 3.9+,标准库(零外部依赖)。
- 目标仓库能 `cargo build --release`;`cargo`/`git` 在 PATH。
- macOS:profiler 用自带 `/usr/bin/sample`(无需 sudo)。
- `claude` CLI:读阶段(只读)+ agentic(写,在抛弃式 worktree 里用 `--dangerously-skip-permissions`,跑完即删)。

每个 worktree 用**独立** `CARGO_TARGET_DIR`(`.aro-<spec.name>-td/<worktree>`)——共享会让 cargo 跨 worktree 复用编译产物,基线和候选就比同一份二进制了(Δ 和差分全失真);代价是每候选多编译一次,这是正确性的必要开销。worktree 在 `.aro-worktrees/`,用完即删。

## 2. 主命令:`python3 -m aro run`

```sh
cd aro
python3 -m aro run targets/<name>.json \
    [--rounds N] [--blind] [--no-read] [--aa-runs N] [--ab-pairs N] [--out DIR]
```

| flag | 默认 | 说明 |
|---|---|---|
| `<spec.json>` | (必填) | 目标 spec |
| `--rounds N` | spec.stop.max_rounds | 轮数硬上限(也受 goal/dry 提前停) |
| `--blind` | (关) | 用 profiler-only hint(不点明技巧),做诚实盲发现测试 |
| `--generator ralph\|agentic` | spec.generator(默认 agentic) | thin 单次 `claude -p` vs heavy 写-编-修(+read+reflect) |
| `--no-read` | (关) | 跳过 read 阶段 |
| `--aa-runs N` | 2 | A/A 标定配对次数 |
| `--ab-pairs N` | 4 | 每候选配对 A/B 次数 |
| `--out DIR` | `./.aro-runs/<name>` | 输出 |

生成默认走 **agentic 写-编-修**(真 `claude`):每轮在抛弃式 worktree 里 edit→build→test→改→迭代,**靠目标自停**(过 build+test 即收;只有一个很高的 hang 兜底,不是 work-cap),ARO 取最终 diff 交判分。

## 3. 接一个新目标(写 spec)

两种方式:跑 **plan 向导**(`skill/references/plan-workflow.md`——交互探测 build/test、写探针、**dry-run build+probe+test** 后才产出 spec),或手动复制一个已有的 `targets/*.json` 改这几槽(schema 见 `skill/SKILL.md`):
- `repo` / `baseline_ref` / `build` / `test`(命令 token 列表);
- `bench`:`probe`(`probes/*.rs` 探针)、`pkg`、`example`、`sample_prefix`、`metric`;
- `regions`(可改路径)、`context`(喂给生成器的 file + anchors);
- `objectives` + **`goal`(metric/direction/target)** + **`stop`(max_rounds/dry_rounds)**;
- `prompts`(引用 `skill/prompts/*.md`)、`blind`、`read_phase`。

`goal.target=null` = open-ended(尽力,受 stop 约束);给个数就到点停。

## 4. 工具

```sh
python3 find_hotpath.py                                  # 自动找真热点 + 隔离内核延迟
python3 verify_patch.py <patch.txt> [--spec ...] [--ab-pairs N]   # 复核某个已记录补丁
python3 selftest.py                                      # 不碰 cargo 的 mock 自检（复利 + 事件）
```

## 5. 看输出（`--out` 目录）

| 文件 | 是什么 |
|---|---|
| `events.jsonl` | **真相源**:逐步事件流（含 `regression_baseline` / `read_phase` / `gate` / `candidate_verdict` / `baseline_advanced` / `direction_proposed` / `goal_met` / `stopped`），实时 flush，可 `tail -f` |
| `RUN-REPORT.md` | 中文叙事——**由 skill 从 `events.jsonl` 渲染**（数字逐字照抄，已无 `report.py`；见 `skill/references/report-protocol.md`） |
| `records.jsonl` / `floors.json` / `agenda.jsonl` / `patches/<id>.txt` | 记忆底账 / 噪声地板 / 研究议程 / 补丁原文 |

**判定**:`accepted`(过双闸进 Pareto)/ `within-noise` / `regressed` / `verify-failed`(测试失败 / 跌破基线测试数 N_pre / 差分不符)/ `build-failed` / `rejected`(防作弊拦下,没开跑)。

## 6. 记忆与续跑

同一 `--out` 再跑会**重建已接受补丁**(从 `pareto` + `patches/`)并应用到基线——续跑从**已优化基线**继续,不是从头(`baseline_resumed`);死路也喂下一轮 prompt。`events.jsonl` 按 `run_id` 追加(不截断,历史不丢)。干净开始就换 `--out`。

## 7. 已知边界

- **测量看机器**:A/A 地板每轮不同;要下结论用稳定机、`--ab-pairs` 给够。
- **差分**:spec 声明 `differential` 探针时,ARO 在基线和候选各跑同一确定性随机输入探针、要求输出一致——真正的逐字节行为校验;没声明则退回干净树 MVP。
- **大重构靠 read 阶段 + 无 work-cap + 复利**落地;单个 `claude` 仍可能很慢。
- 隔离微基准上的收益未必等于生产规模收益(尤其 DRAM-bound 的内核)。
