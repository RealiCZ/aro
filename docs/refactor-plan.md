# ARO 接手评估与重构方案（v1，待 review）

> 结论先行：**judge（护城河）名副其实，基本不动；该动的是它周围的一切。**
> 项目的核心资产是 `eval.py`/`stats.py`/`guard.py` 那套确定性评判 + `events.jsonl` 单一
> 事实源的架构决策，这两样质量很高。主要债务在：上帝模块（`sweep.py` 1049 行）、
> 巨型函数（`run_backtest` 290 行 / 17 参数）、六类跨模块复制粘贴、真实 I/O 边界
> （cargo/git/claude/profiler）零测试、零 CI 零打包，以及若干健壮性缺口。
> 方案分 6 个阶段，每个阶段可独立合并、独立回滚，并为 infinite-flow 阶段 2
> （producer-consumer）预留接缝。

---

## 0. 项目现状一览

- **是什么**：ARO 是一个自主性能优化循环 —— profile 找真热点 → LLM 生成一个行为保持的
  改动 → 确定性三门 judge（防作弊守卫 / 正确性含字节相同 differential / A/A 地板 +
  配对 A/B + bootstrap CI 显著性）→ 接受的补丁叠进基线复利 → reflect 喂回下一轮。
  纯 stdlib Python（~5.5k 行）驱动 Rust 目标；Svelte 前端渲染决策树报告。
- **当前分支** `infinite-flow-phase1` 已落地无限流阶段 1（并行 fan-out 生成、预筛、
  穷尽前沿、自动决策树）；设计文档规划了阶段 2（跨函数 producer-consumer、对抗复核）。
- **测试**：`selftest.py` 799 行、22 组用例（#5–#27），纯逻辑覆盖尚可，
  **cargo/git/claude/profiler 全部真实 I/O 零覆盖**；无 CI、无打包、无 lint/类型检查。

---

## 1. 评估

### 1.1 做得好的（重构中必须保住）

1. **judge 是真护城河**：A/A 校准地板、顺序交替配对 A/B、seeded bootstrap CI、
   方向感知判定、auto-tighten 的防"换探针钓鱼"（符号一致性 + 地板必须下降）、
   per-worktree `CARGO_TARGET_DIR` + 强制重编译自检。工程密度远超一般"AI 优化器"。
2. **`events.jsonl` 单一事实源**，所有报告/清单/图表可离线重生成，"数字 verbatim、
   verdict 不二次评判"的纪律贯彻得很好。
3. **诚实文化**：`accepted ≠ should-merge`、no silent caps（预筛丢弃也记录成 outcome）、
   comprehension-debt 显式列出、README "What it won't do" 一节。
4. 组合决策正确：并行生成 / 串行判分、round 末折叠（兄弟公平竞争）、resume 失败
   fail-fast 不静默降级（`engine.py:104-113`）。

### 1.2 问题清单（按严重度）

**A. 正确性风险（不同产物可能给出不同结论）**

| # | 问题 | 证据 |
|---|---|---|
| A1 | `events.jsonl` 的"取最近一个 run 切片"有 **3 种不同实现**，chart/sweep 还各有一套不切片的读取 —— 同一份日志，不同产物可能读到不同的 run | `manifest.py:30` `tree.py:24` `trajectory.py:55` `chart.py:280` `sweep.py:954` |
| A2 | "挑 headline Δ" 的方向感知选择逻辑 **5 处各写一份且规则不一致** —— 同一 run 的头条数字随渲染入口而变 | `store.py:232` `manifest.py:48` `trajectory.py:89` `chart.py:294` `__main__.py:105` |
| A3 | SEARCH/REPLACE 补丁格式有 **2 个解析器**，且 `manifest`/`tree` 直接 import `store._parse_patch_file` 私有函数 —— 格式一变必错一处 | `store.py:255` vs `verify_patch.py:24`；`manifest.py:67` `tree.py:39` |

**B. 结构债务**

| # | 问题 | 证据 |
|---|---|---|
| B1 | `sweep.py` 1049 行上帝模块：v0 符号 demangle、owner 分类、lesson 索引、前沿分桶、profile 编排、L3 元循环、3 套 Markdown 渲染、SVG→PNG、手写 argv 解析 | `aro/sweep.py` 全文 |
| B2 | `run_backtest` 单函数 290 行、17 个参数；prescreen/折叠/reflect/停机全部内联 | `engine.py:36-327` |
| B3 | `SpecTarget` 上帝对象且"私有"边界虚设：5 个模块直接调 `_td_for/_env/_pkg_dir/_write_probe/_run_diff_probe` | `sweep.py:350,359,368,450` `plan.py:133` `generator.py:303` `find_hotpath.py:40` |
| B4 | `claude` 子进程调用 **5 处复制**（超时、cwd 各不相同）；git worktree 生命周期 **3 处复制** | `generator.py:180,308,383,420` `critic.py:140`；`target.py:72` `plan.py:143` `generator.py:163,286` |
| B5 | CLI：手写 `opt()` 解析 **8 处复制**、if-chain 分发、布尔旗标与取值旗标两套写法、未知旗标静默忽略 | `__main__.py:24` `chart.py:516` `serve.py:63` `plan.py:235` `sweep.py:973` `verify_patch.py:48` 等 |
| B6 | 两套平行图表栈：`trajectory.py + chart.svg/ascii`（仅 `aro chart` 用）与 `chart.perf_token_svg/explore_svg`（真报告用），各自从 events 推导复利曲线 | `trajectory.py` `chart.py` |

**C. 健壮性缺口**

| # | 问题 | 证据 |
|---|---|---|
| C1 | 所有 git 子进程 **无超时**（cargo/claude 都有）；credential 提示或锁竞争会挂死整个 harness | `target.py:75,92,154,226` `generator.py:165,288,293` `plan.py:147,155` `verify_patch.py:81,85` |
| C2 | 生成器 bare `except → return None` **静默吞掉候选**，不发事件 —— 系统性坏掉的生成器与"模型没提案"不可区分 | `generator.py:146,183,296,313,386,423` |
| C3 | spec 载入不校验：`bench["pkg"]` 等必填键缺失时在 `target.bench` 深处 KeyError | `spec.py:42` `target.py` 多处 |
| C4 | 事件是端到端无 schema 的裸 dict，消费侧靠字符串比对；emit 键打错静默失效 | `events.py:58` + 所有消费者 |
| C5 | profiler 写死共享路径 `/tmp/aro_sample.txt` / `/tmp/aro_perf.data` —— **并发跑两个 run 互相覆盖**（与无限流的并行诉求直接冲突） | `profile.py:119,144` |
| C6 | 无探针时 `differential` 跑了 `git status` 却不看结果直接 `return True`（死检查；严格模式由 eval 层挡住，仅 weak_oracle 路径可达，但代码本身是陷阱） | `target.py:146-158` |
| C7 | `prompts.load` 对核心模板（ralph/agentic/critic-*）无缺失保护；`serve` 默认绑 `0.0.0.0` 无鉴权 | `prompts.py:23` `serve.py:66-78` |

**D. 工程化缺失**

- D1 无 CI —— `selftest.py` 从不自动跑，无任何合并门。
- D2 无 `pyproject.toml`/打包 —— 不可安装；`REPO_ROOT = Path(__file__).parent.parent` 在
  4 个模块里各算一遍（`spec.py:23` `plan.py:30` `prompts.py:20` `lessons.py:17`），包不可搬迁。
- D3 无 ruff/mypy —— 全库类型标注齐全却没人检查（白写）。
- D4 selftest 是单个 799 行 `run()` + 裸 assert，第一个失败掩盖全部后续；真实 I/O 边界零覆盖。

**E. 仓库卫生 / 文档漂移**

- E1 `.gitignore` 仅 3 行；`.aro-report-8010/`（741KB 生成物）未忽略；`remote-readme.md`
  是一份与本仓库无关的主机清单（疑似误落）。
- E2 机器追加的 `memory/lessons.jsonl` 被 git 跟踪且长期带未提交 diff；512KB 构建产物
  `aro/decision_tree_template.html` 与 617KB PNG 入库。
- E3 文档漂移：`OPERATING.md:64` 的 `find_hotpath.py` 用法缺必填参数；
  `docs/explore-mode-design.md:136` 把已上线的 critic 标为"要建"；
  人类报告有三个名字（`RUN-REPORT.md`/`REPORT.md`/`DAILY-REPORT.md`）。
- E4 生成报告中英混排（`sweep.py` 的 进化了/能进化的/碰不得的、critic_context 中文），
  与近期 skill "English-only" 政策相悖。
- 死代码：`Candidate.parent`、`Report.log/rounds/floors`（只写不读）、critic 多评审
  `n>1` 路径无人用、`eval.py:272-273` 不可达分支。

---

## 2. 重构原则（Not building / 冻结区）

1. **judge 语义冻结**：`eval.py`/`stats.py`/`guard.py`/`critic.py` 的判定逻辑、阈值、
   统计口径一律不改；允许搬家和纯机械整理，任何语义改动出本方案范围。
2. **事件契约不破坏**：`events.jsonl` 现有字段只增不改不删（下游 skill/消费者依赖）。
3. **五条不变量照守**（infinite-flow 设计 §6）：bench 串行、写手不自评、正确性先于
   显著性、数字 verbatim、通用性走 cargo metadata。
4. **不重写 viz 前端**（1.2k 行 Svelte，状态良好）；不在本方案内做 infinite-flow 阶段 2
   （producer-consumer、对抗复核、多 workload）—— 但 P3 为它留好接缝。
5. **不引入运行时第三方依赖**（"pure-stdlib" 是产品承诺）；dev 工具（ruff）仅进 CI。
6. **目录保持扁平**：考虑过 `core/infra/judge/loop/report` 子包化，**否决** —— 24 个模块
   扁平放完全可管理，子包化只带来 git blame 断裂和全部文档路径失效，无实质收益。

---

## 3. 分阶段方案（每阶段独立可合并、独立可回滚）

### P0 — 卫生与文档（~0.5 天）

- `.gitignore` 增补：`.aro-report-*/`、`.aro-worktrees/`、`*.egg-info/`。
- 修 3 处文档漂移（E3）：OPERATING.md 的 find_hotpath 用法、explore-mode-design.md
  的 critic 状态表（或标注"已由 infinite-flow-design.md 取代"）、统一报告名词表。
- `remote-readme.md` 处置（开放问题 Q4，默认移出仓库）。
- 生成报告语言统一为英文（E4，开放问题 Q3）：只改 `sweep.py`/`critic_context` 里的
  用户可见字符串，中文设计文档不动。
- **验证**：`python3 selftest.py` 全绿；`git status` 干净。

### P1 — 安全网（~1.5 天）★ 后续一切的前置

- `pyproject.toml`：`requires-python = ">=3.9"`，`aro` 打包，`skill/prompts/*.md` 与
  `probes/` 声明为 package data（先声明，路径解析迁移放 P5）。
- `ruff` 最小规则集（E/F/W + isort），**不做全库重排版**，只挡新增违例。
- GitHub Actions：job① `python3 selftest.py`（3.9 与 3.12 矩阵）+ ruff；
  job② 装 Rust 工具链，跑新增的 **cargo fixture E2E**。CI 只做检查，不推送不发布。
- **cargo fixture E2E（本阶段核心）**：`fixtures/mini-target/` 放一个几十行的小 crate
  （含 bench example、差分探针、2 个测试）+ 对应 spec。用 `PlannedGenerator` 种一个
  已知补丁，走一遍完整真实链路：`make_worktree → build → test → differential →
  calibrate_floors → evaluate → manifest`。本地无 cargo 时自动 skip。
  这是唯一能兜住 `target.py`/judge 真实路径的网 —— **必须先于 P2/P3 落地**。
- **验证**：CI 两个 job 全绿；fixture E2E 在本机（有 cargo）跑通。

### P2 — 去重合并（~2.5 天，6 个独立提交）

1. **`aro/runlog.py`**：`load_events(dir)` + `latest_slice(events)`（唯一切片规则，
   以 `run_started`+`run_id` 为准）+ 事件名/字段名常量表。重指 `manifest`/`tree`/
   `trajectory`/`chart`/`sweep` 五处（灭 A1、C4 的一半）。
2. **`aro/patchfile.py`**：SEARCH/REPLACE 格式唯一 owner（dump/parse/safe-id）。
   重指 `store`/`manifest`/`tree`/`verify_patch`（灭 A3）。
3. **统一 headline-Δ 选择**：`types.py` 增 `best_improvement(deltas, obj_min)` 一个
   函数，5 处调用点全部重指（灭 A2）。
4. **`aro/llm.py`**：`run_claude(prompt, *, cwd, timeout, session_log=None) →
   (text, tokens, cost_usd)`，统一 5 处调用；失败**必发事件**（`generator_error`，
   新增事件，不破坏旧契约）而非静默 None（灭 B4 的 claude 半 + C2）。
5. **`aro/vcs.py`**：git worktree add/remove/status/rev-parse 带超时的薄封装，
   `target`/`plan`/`generator`/`verify_patch` 重指（灭 B4 的 git 半 + C1）。
6. **spec 载入即校验**：`spec.from_dict` 检查 `bench.pkg/example`、`differential.*`、
   `profile.*` 必填键，缺失时报"哪个 slot 缺哪个键"（灭 C3）。
- **验证**：selftest + fixture E2E 每个提交各跑一遍；对拍一次真实 spec 的
  `aro manifest`/`aro tree` 输出与重构前 byte-identical（同一 events.jsonl 输入）。

### P3 — 结构拆分（~4 天）

1. **拆 `sweep.py`（1049 → 4 个模块）**：
   - `aro/symbols.py`：v0 demangle、`_fn_name`、`classify_owner`、rustfilt 集成（~250 行，纯函数）。
   - `aro/frontier.py`：`bucket_functions`、lesson 索引、`_refill_queue`、headroom 计算（纯函数）。
   - `aro/attempt.py`：L3 元循环 `attempt()` + `_finalize_run`（编排层）。
   - `sweep.py` 只留 L1 frontier-map（profile_ranked + render_map）。
   - Markdown 渲染（`render_map/render_attempt_map/render_explore_report`）合并进
     `aro/report_md.py`；`_svg_to_png` 挪进 `chart.py`。
2. **`__main__.py` → argparse 子命令注册表**：每个子命令模块暴露
   `register(subparsers)`；删除 8 处 `opt()`；未知旗标报错而非静默。
   `verify_patch.py`/`find_hotpath.py` 收编为 `aro verify-patch`/`aro hotpath`
   子命令（根目录留一行 shim 保住 README 用法）。
3. **`run_backtest` 瘦身**：新增 `RunConfig` dataclass 收拢 11 个循环旋钮；函数体拆为
   `_freeze_baseline` / `_resume` / `_prescreen_round` / `_judge_round` / `_fold_round`
   / `_reflect_round`；行为与事件流 **byte-identical**（用 fixture E2E 的 events.jsonl
   对拍验证）。
4. **`SpecTarget` 边界正名**：被外部调用的 `_td_for/_env/_pkg_dir/_write_probe/
   _run_diff_probe` 去下划线转正并写 docstring；`cargo metadata` 查询挪到独立
   `aro/cargo.py`（`sweep._workspace_members` 现在把缓存塞进 `target.__dict__`，一并收编）。
5. **为 infinite-flow 阶段 2 留缝**：P3 完成后，生成侧（`generator.propose`）与判分侧
   （`eval.prescreen + evaluate`）之间只剩显式参数传递、无共享可变状态 ——
   阶段 2 的 producer-consumer 队列可以直接插在两者之间，不再需要先拆 sweep。
- **验证**：selftest + fixture E2E；同一 spec 干跑一轮 `--attempt`（PlannedGenerator）
  对拍 events.jsonl 事件序列一致。

### P4 — 测试升级 + 健壮性（~2.5 天）

- `selftest.py` 拆为 `tests/test_*.py`（**stdlib `unittest`**，不引 pytest，守零依赖
  承诺；`python3 selftest.py` 保留为 `unittest discover` 的一行包装，README 用法不破）。
- 健壮性修复：
  - profiler 临时文件 → 每 run 独立 `tempfile.mkdtemp`（灭 C5，为并行 run 解锁）；
  - `prompts.load` 缺模板时报可用模板列表（灭 C7 半）；
  - `serve` 默认绑 `127.0.0.1`，`--host 0.0.0.0` 显式开启（灭 C7 半）；
  - `target.differential` 无探针路径改为显式（weak_oracle 时直接 return True + 注释，
    删掉不看结果的 git status 死检查）（灭 C6）。
- 死代码清理：`Candidate.parent`、`Report.log/rounds/floors`、`eval.py:272-273`；
  critic `n>1` 多评审路径 —— 留（阶段 2 对抗复核会用），加注释说明。
- **验证**：`python3 -m unittest` 全绿 + fixture E2E + 并发跑两个 profile 不互踩。

### P5 — 可选项（单列，各自拍板后做）

- **删平行图表栈**（B6，开放问题 Q2）：删 `trajectory.py` + `chart.svg/ascii_chart`
  + `aro chart` 子命令（真报告走 `perf_token_svg`/`explore_svg`，不受影响）。约 -500 行。
- **prompts 路径资源化**：`importlib.resources` 优先、仓库布局回退，包真正可安装可搬迁（D2 收尾）。
- **typed events**：在 runlog 常量表基础上升级为轻量 dataclass（收益递减，最后做）。
- viz 构建产物：维持提交现状（零依赖分发的务实选择），仅在 CI 里加"template 与
  viz/src 同步"检查；不折腾 LFS。

---

## 4. ARO 自身的运行时优化机会（判分吞吐 = 你们自己认定的瓶颈）

infinite-flow 设计 §2.4 明确"串行 judge 吞吐是唯一瓶颈"。以下四项直接买回墙钟，
建议随 P3/P4 一起做或紧随其后：

1. **消灭 prescreen→evaluate 双构建**（最大件）：预筛通过的候选，`prescreen` 已在
   `pre-<id>` worktree 里完成 apply+build（`eval.py:110-122`），`evaluate` 却重开
   `cand-<id>` worktree 从头再建（`eval.py:170`）。Rust 全量构建分钟级 ——
   **每个存活候选白付一次构建**。改法：prescreen 成功时返回并保留 worktree，
   evaluate 复用（同一候选同一 td，不违反"不同代码不共享 td"不变量；
   强制重编译自检语义不变，因为该 worktree 本来就刚编译过本候选）。
2. **prescreen 的 baseline smoke bench 按轮缓存**：现在每个候选都重测一次基线
   （`eval.py:129`），一轮 N 个候选 = N 次冗余基线 bench；基线在轮内不变，测一次即可。
3. **noise floors 按 (scale, baseline 状态) 缓存**：auto-tighten 每次升 scale 都重跑
   `calibrate_floors`（2×aa_runs 次 bench）；同一 scale 在基线未推进期间可复用，
   基线 fold 后失效重校。
4. **sccache 实验**（可选）：`RUSTC_WRAPPER=sccache` 保持 per-worktree td 隔离
   （不变量不破），编译器层缓存砍重编译时间。上线前必须用 A/A 验证地板不变。

---

## 5. 风险与最脆弱假设

- **最脆弱假设**：「selftest 绿 = 行为未破坏」。它只覆盖纯逻辑；`target.py`/
  `generator.py` 的真实 cargo/git/claude 路径它一概看不见。**方案已为此变形**：
  P1 的 cargo fixture E2E 先行，P2/P3 每个提交都过它 + 关键产物对拍
  （同输入 events.jsonl → manifest/tree 输出 byte-identical）。
  若 fixture E2E 做不出来（如 CI 装 Rust 受限），则 P3 的 `target.py` 改动降级为
  只做转正改名、不动实现。
- **git 无超时的修复本身有风险**：给 worktree 操作加超时后，超时值过小可能在慢盘上
  误杀 —— 取 `spec.timeout` 同值，与 cargo 一致。
- **回滚**：全部为代码重构，无数据迁移、无外部状态；任何阶段 `git revert` 即回。
  事件契约只增不改，旧 run 目录永远可被新代码渲染。
- **规模坦白**：P2+P3 合计触碰约 20 个文件 —— 超过 8 文件阈值，靠"每阶段 6 个独立
  提交 + 每提交过全套验证"控制爆炸半径。
- **依赖清单**：无新增运行时依赖；CI 需要 GitHub Actions（仓库已在 GitHub）、
  Rust 工具链（job②内装）；无任何密钥/凭证需求；`claude` CLI 不进 CI。

---

## 6. 开放问题（要你拍板）

| # | 问题 | 我的建议 |
|---|---|---|
| Q1 | `memory/lessons.jsonl`：保持 git 跟踪（现状，每次 run 后有 diff 噪声）还是移出跟踪？ | **保持跟踪**（它是跨 run 记忆、产品的一部分），但改为 run 收尾时专门 commit，不让它长期挂 dirty |
| Q2 | 平行图表栈（`trajectory.py` + `aro chart`）删还是留？ | **删**（真报告不用它，-500 行） |
| Q3 | 生成报告语言：统一英文还是保持中英混排？ | **英文**（与 skill English-only 政策一致；中文留在设计文档） |
| Q4 | `remote-readme.md`（无关主机清单）如何处置？ | **移出仓库**（不 gitignore 掩盖，直接挪走） |
| Q5 | 要不要做成 pip-installable？ | **要**（P1 打基础、P5 收尾），同时保住 clone-and-run |
| Q6 | 运行时优化四项（§4）随 P3/P4 做，还是重构落定后单独一轮？ | **§4.1（双构建）随 P3 做**（收益最大且与 eval/target 接口改动同域）；其余单独一轮 |

---

## 7. 执行顺序与体量

```
P0 卫生(0.5d) → P1 安全网(1.5d) → P2 去重(2.5d) → P3 拆分(4d) → P4 测试+健壮(2.5d) → P5 可选
```

合计约 11 人日当量（agent 驱动实际更快）。P0/P1 无行为改动；P2 起每个提交都有
selftest + fixture E2E + 产物对拍三重门。任一阶段停下，仓库都处于比之前更好的
可用状态（阶段独立可合并）。

**最小方案**（如果只想做三分之一）：P0 + P1 + P2 —— 不动结构，只加安全网、灭掉
三类正确性风险（A1/A2/A3）和 git 超时缺口，约 4.5 人日。
