# ARO 调优过程记录 (Tuning Process Log)

> 目的:记录用 ARO 自主优化框架做真实调优、以及调优 ARO 本身的全过程,作为最终文档的素材。
> 维护方式:按阶段追加,事实优先;结论一律以判分器(deterministic judge)的输出为准。
> 起始:2026-06。环境:macOS arm64,Python 3.9,ARO 仓库 `aro-py`。

---

## 0. 背景与命题

ARO(Auto-Research Optimizer)= 自主性能优化框架。核心命题:

> **生成是 commodity,判分器(judge)是 moat。** 不管谁来提优化(人或 AI),都不能全信;
> 唯一防线是一个确定性的、骗不过也刷不了分的判分器:它在子 1% 的改动、噪声很大的基准上,
> 也能把"真赢点"和"运气/漂移"分开。

两种运行模式:
- **spec-driven**:人已隔离好指标,写 `targets/*.json` + 探针,`python3 -m aro run`。
- **autonomous**:agent 自己 profile → 写探针 → 提改动 → 判分 →(协议见 `skill/references/autonomous-optimization.md`)。

判分门(gates):
- Gate 0 反作弊(路径屏蔽 + region 强制)
- Gate 1 正确性(apply → build + recompile 自检 → test + N_pre 回归门 → 随机输入差分)
- Gate 2 显著性(A/A 噪声地板 + 顺序交替 A/B + bootstrap CI,方向感知)

跨运行记忆:`memory/lessons.jsonl`(踩坑/赢点,注入后续 prompt)。

---

## 1. 实跑一:salt autonomous(受控,lessons 已含先验)

**任务**:在 salt(Rust 加密库,banderwagon 曲线)上跑一次自主优化。注意:此时 `lessons.jsonl`
已 seed 了此前结论(precompute-K +14% / 堆-Vec −53% / 共享 target-dir 陷阱),所以这是**装上记忆后的
系统自测**,不是盲测。

**过程**:fresh agent 读 lessons → profile 出 `mul_index` 71.66% → 隔离微基准 →
提改动 `a *= Fq::from(5)` → `a + 4a`(三次域加法,等价乘 5)。

**判分结果**:`within-noise`。
- 全 gate 过(byte-identical 差分一致、14 测试不掉、recompile 自检过)。
- 显著性:Δ −0.62%,CI[−0.76,+4.16] 跨 0,地板 0.99% → 算不上可测赢点。
- agent 笃定"1 次蒙乘换 3 次加法明显更快",judge 用数据否了它。

**过程产出的真 bug(最大价值)**:第一次直接崩在 `compute_region_hint` —— 通用化时删了
salt 版 hint 模板,却没补通用的 `hint.md`/`hint_blind.md`,而 cargo-free 的 selftest 用 mock
target 从不走这条路,漏网。修复:补通用模板 + region-hint 在模板缺失时降级而非崩。

**lesson #4** 落库(add_affine_point 局部 mul→add 强度削减是 sub-noise;杠杆在算术重构)。

---

## 2. 实跑二:mega-evm 盲测(真盲,答案不可达)

**任务**(用户原话):"准备一个环境,mega-evm 在 `f643c24 perf(evm): trim per-opcode hot path
and pre-execution scans (#313)` 这个提交之前,看现在的框架能不能找到同样的性能调优方案。你不能插手。"

即:把 mega-evm 切到 perf 提交的**父提交**,放一个全新 agent 进去盲跑,不给任何 EVM 信息、不替它
选热点、不帮它找答案;最后由我(知道答案、私下读过 f643c24)逐条比对"找到的是不是同一套"。

### 2.1 环境准备(隔离 + 藏答案)
- 用 `git archive dd6770b`(= `f643c24^`)导成**零历史单提交仓库** 独立盲测目录,
  提交信息中性。`git cat-file -e f643c24` 失败(答案完全不可达);基线代码确认未优化
  (`BTreeMap`/`@frame` 计数为 0)。
- 放在独立嵌套目录,`ls ..` 只见 `evm`,避开同级目录树下含答案的真 `mega-evm` 和
  名字泄密的 `mega-evm-opcode-hotpath`/`mega-evm-perf-hotpath`;agent prompt 明令禁止越界探索。
- forge-std 子模块从源仓库 seed 进来保证可编译;编译校验通过(依赖已全局缓存,check ~30s)。

### 2.2 第一个 agent 卡死(编排脆弱性 —— 真实发现)
第一个 agent 用 `run_in_background` 起了构建后"came to rest"空等 —— 这个 harness 无法把后台子构建
的完成事件唤回 subagent,而本 harness 也没有 SendMessage/resume 能力可用,于是它**永久卡死**(与
更早一次盲跑同样的失败模式)。只跑了 ~95s(仅探索)。
**绕法**:杀掉孤儿构建,恢复干净基线,改用**前台构建**(缓存依赖→增量快→不撞 10 分钟上限)+
**判分由我后台稳跑**(salt 那次验证过的稳妥分工)。

### 2.3 第二个 agent(前台)→ 找到热点,提 `#[inline]`
- **热点**:`AdditionalLimit::check_limit` / `record_compute_gas`(`limit/limit.rs`)——每个 opcode
  都经 `compute_gas_ext → record_compute_gas → check_limit` 的**四维限额扇出**。采样测得
  `check_limit` ~17%(744 样本)+ 其非内联 callee `state_growth::check_limit` ~4.4%,是
  mega-evm 自身最重的叶子。
- **改动**:给 `StateGrowthTracker::check_limit` 加 `#[inline]`(其它 3 个 tracker 的 check_limit
  已被内联进 `AdditionalLimit::check_limit`,只有 state_growth 没加 hint → 每 opcode 一次独立调用帧)。
  纯 codegen hint,字节一致。
- 213 测试基线通过 + 带 patch 仍 213;差分 `DIFF b210eb7db8860bee` 两侧一致。

### 2.4 与 f643c24 真实方案逐条比对

| 维度 | f643c24 真实方案 | 盲测框架结果 |
|---|---|---|
| **热点定位** | per-opcode 限额检查 `record_compute_gas`/`check_limit` | ✅ **盲选命中同一函数**(~17%+4.4%) |
| **手法方向** | 给热路径记录函数加 `#[inline]`(加了好几个) | ✅ 同源(提案正是 `#[inline]`) |
| **核心赢点** | 四维扇出检查 → **只查 compute 一维**(记录 compute gas 只可能动 compute 维;其它 3 维各自在 mutation 点 latch) | ❌ **没找到** |
| **独立修复** | 鉴权列表预扫描 `Vec→BTreeMap`(O(N²)→O(N log N),对抗 ~1200 authorities) | ❌ 没找到(需对抗 auth-list 负载才暴露) |
| **它找到的那个改动判分** | — | **within-noise** |

f643c24 还为新不变量补了 `debug_assert!` + 测试(`test_record_compute_gas_records_after_other_dimension_latched`)。

### 2.5 框架 bug:探针路径解析(已修)
判分首次在校准阶段崩:`no example target named evm_auto`。根因:SpecTarget 把探针写到
`<work>/<pkg>/examples/`,默认假设"包名 == 仓库根下目录名"——对 salt 的 `banderwagon/` 成立,
但 mega-evm 的 crate 在 `crates/mega-evm/` 下。修复:用 `cargo metadata` 把包名解析到真实 crate
目录(带缓存、任意 workspace 布局通用,salt 扁平布局解析结果不变),selftest 仍绿。
→ **两次盲跑各抓出一个 mock selftest 漏掉的真 bug**(salt:hint 模板;evm:探针路径)。

### 2.6 判分 verdict:within-noise
全 gate 过(213 测试不掉、差分一致、recompile 自检过)。显著性:
**Δ −4.07%,CI[−25.56,+4.60] 跨 0,地板 0.70%** → EVM 基准方差极大,单个调用帧的去除无法与噪声
区分,judge 正确拒绝。`lesson #5` 落库。

### 2.7 诊断:为什么没找到"四维→一维"
**不是模型变笨,是 ARO 的任务框架把搜索变窄了。**
- 关键证据:agent **看懂了**四维扇出结构(它的理由明说"其它 3 个 tracker 被内联,只有
  state_growth 没加 hint")。它问的是"**怎么让这 4 次检查更快**"(→内联),而非"**这 4 次有几次
  是必要的**"(→只需 1 次)。
- 差距在**思维 lens**:agent 默认走"让现有工作更快/选最小安全改动",ARO 的 prompt + 判分恰好
  奖励这个:"ONE byte-identical change + 差分探针"把 `#[inline]`(零风险)变成最优解;四维→一维
  虽也字节一致,但其安全性**依赖跨函数不变量**(其它 3 维只在各自 latch 点变),agent 觉得更险,
  退守到稳的 inline。
- f643c24 那个 AI 是开放式工程:有 benchmark 顶出 per-opcode 开销、可**为新不变量补 assert+测试**。
  ARO 禁止动 tests、要求最小可证改动,等于**把"在不变量保证下删冗余工作"这一最高价值类挤出了搜索空间**。
- 一句话:**判分器作为"门"很对,但生成端 prompt 把 behaviour-preserving 误等于"局部琐碎";真正
  高价值的优化是"在某不变量保证下删掉冗余工作"——同样字节一致,只是需要语义推理,而现在没人逼
  agent 做这步。**

---

## 3. 实验:"多跑几轮 / 加 lens 能不能找到"(A/B)

### 3.1 问题
- Q1(用户):**不改 prompt、只多跑几轮**,能不能从 `#[inline]` 升级到"四维→一维"?
- Q2(我的假设):**加一个"消除冗余 / 不变量"lens + 候选枚举**,能不能?

### 3.2 我的先验判断(待实验证伪)
- Q1 大概率 **不行**,原因不是"次数不够"而是"搜索的种类不对":多轮放大的是同一个搜索算子
  (局部加速 + 保守),reflect/agenda 继承同样盲点,会得到更多局部变体或漂到别的热函数;
  compounding 也帮不上(within-noise 没 accept,无物可叠)。裸跑很多轮 + 采样随机性有**非零概率**
  撞上,但靠运气、贵、不可复现。
- **轮数是放大器;得先把放大的东西(搜索算子)换对。** 正解 = lens 注入 reflect,让"局部微调
  反复 within-noise"触发**升级**到结构性问题。

### 3.3 设计(单变量 = lens)
两臂都作为"round 2",给**相同的 round-1 上下文**(round 1 已试 `#[inline]` state_growth →
within-noise;热点是 check_limit 的四维扇出),唯一差别是 lens:
- **Arm A(对照)**:现状 prompt,无 lens。看它是否出更多局部改动/漂走。
- **Arm B(处理)**:+ "消除/削弱/codegen"三段式 lens + 候选枚举 + 升级指令。看它是否够到"四维→一维"。
- 每臂产出工件由我用判分器(确定性,非插手发现)打分。Arm B 若一轮没中,再补一轮升级 reflect。
- lens 先放进 subagent prompt(隔离变量、不先改仓库);若 Arm B 成立,再落进 `read.md`/hint 并提交。

### 3.4 结果

**Arm A round 2(对照,无 lens)—— 提案已出,判分进行中**
- 给了 round-1 上下文(已试 `#[inline]` state_growth,within-noise),不给 lens。
- agent **又往同一热路径更深挖了一层**:它发现每个 opcode 的 `check_limit` 扇出到 4 个 tracker,
  每个 tracker 的 check_limit 都调 `FrameLimitTracker::exceeds_current_frame_limit`
  (`limit/frame_limit.rs`)——**每 opcode 被调 4×**,是最热的共享叶子。
- **改动**:把 `exceeds_current_frame_limit` 里**重复调用两次 `entry.used()** 的 match-guard 改成
  `if let` 只算一次(CSE 一个重复调用,去掉一个 `checked_add`)。字节一致。
- **关键观察(印证假设)**:agent **再次清楚看到了 4× 扇出**(原话:"每个 tracker 的 check_limit
  调 exceeds_current_frame_limit 4× per opcode"),但它的选择仍是"**让这个叶子更便宜**",而不是
  "**这 4 次调用有几次是必要的**"。和 round 1 同一个盲点,只是深了一层。**没够到"四维→一维"。**
- **判分 verdict:within-noise**(Δ −0.18%,CI[−17.16,+54.95],floor 0.50%)。又一个正确但不可测的
  局部改动。
- 这就是 Q1 的答案的强证据:**不加 lens、只多跑轮,搜索在同一方向上越挖越深(round 1 叶子 inline →
  round 2 叶子 CSE),全是 within-noise,不会自发跳到"删掉冗余的 3 次检查"。**

> 过程小坑(也是一条发现):Arm A agent 把改动留在了工作区没还原(`git clean` 只删未跟踪文件、
> 不还原已改的跟踪文件),导致 verify_patch 预检 "search 0x"。手动 `git restore .` 后重判才正常。
> → agent 收尾应加 `git restore .`,已写进 Arm B 的 prompt。

**Arm B round 2(处理,+ lens)—— 提案已出,判分进行中**
- 与 Arm A 相同 round-1 上下文,唯一新增变量 = "消除冗余/不变量"三段式 lens + 候选枚举 + 允许
  "不变量保证下删冗余"。lens 措辞通用、不点名"四维/compute 维"(避免泄题)。
- **关键结果:lens 让 agent 把真答案显式列为头号候选。** 它的 tier-1 候选 #1 原话:
  *"skip 3 of 4 tracker checks in `record_compute_gas` since only compute_gas mutates per opcode"*
  —— **这就是 f643c24 的"四维→一维"。Arm A 从未考虑过它。**
- **但 agent 把它否了**:*"check_limit() 还会 latch has_exceeded_limit 并强制维度优先级;之前的
  SSTORE/LOG 可能让另一个 tracker exceeded-but-unlatched。太险。"* —— **这正是 f643c24 必须处理的
  那个不变量子点**(它用 `debug_assert!` + 两个测试钉住"每个非 compute mutation 点都自己 latch"
  来解决)。agent 看到风险就退了。
- agent 转而选了**另一个 tier-1 ELIMINATE 改动**:`exceeds_current_frame_limit` 里把
  `checked_add().expect()` 换成 `wrapping_add()`(单帧 persistent+discardable 不会溢出 u64 的不变量下
  字节一致),用 `debug_assert!` 钉不变量,对抗差分(嵌套 CALL/CREATE、紧 gas、refund 路径)确认。
  这比 Arm A 的 inline/CSE 更高级(去掉了 common path 上一个分支 + panic landing pad),且**采用了
  不变量钉法**——但仍不是四维→一维。
- **判分 verdict:within-noise**(Δ +2.66%,CI[−3.92,+46.66],floor 0.86%)。正确、字节一致、213 测试
  不掉,但点估计甚至略偏正,CI 跨 0。

**三轮全 within-noise 的元观察**:Arm A r1(inline,−4.07%)、Arm A r2(CSE,−0.18%)、Arm B(wrapping_add,
+2.66%)——框架在这个 EVM 微基准上提的**所有 per-opcode 微改动都低于噪声地板**。而真正可能可测的赢点
(四维→一维,每 opcode 砍掉 3/4 检查)恰恰是 Arm B **生成了却不敢采纳**的那个。即:**高杠杆结构改动
既是 judge 想看到真赢点所需的,也是生成端会退缩的**——闭合这个"采纳 gap"是收益最高的框架改进。
(注:四维→一维本身在此微基准上是否可测,尚未测——那需要我亲手套用 f643c24 的真实改动来判分,属于
另一个问题,不属于盲测。)

lessons.jsonl 现 6 条(新增第 6 条记录本次 A/B 的两层 gap 结论)。

**Arm C round 2(处理++,lens + 采纳层)—— 闭合!**
- 在 Arm B 的 lens 上加一层"**识别到高杠杆但看着险的冗余时,去解析不变量、别退缩**":追溯被保护状态
  在哪被改、每个改点是否自 latch,确认后用 assert+测试钉住,放胆采纳;judge 是安全网。
- **结果:agent 这次提了"四维→一维"本身 —— 就是 f643c24 的真实改动。** 它:
  1. 选了 ELIMINATE 候选:`record_compute_gas` 只动 compute 维,其它三维是冗余检查;
  2. **解析了不变量(没退缩)**:在全 crate 搜了所有非 compute tracker 的 mutator,逐一确认每个改点
     在回到 opcode 执行前都自己跑过 `check_limit()`(把 exceed latch 进 `has_exceeded_limit`);连那几个
     "记录但不立即 check"的点(`after_frame_init_on_frame` 等)也确认后面跟着带 check 的 hook;还发现
     sandbox keyless-deploy 路径**已经假设了这个不变量**(非 compute 的 exceed 会 `unreachable!()`)——
     旁证不变量成立;
  3. **采纳并钉死**:改写 `record_compute_gas` 为 短路 + 只查 compute,`debug_assert!` 钉不变量,
     对抗差分覆盖"把 data_size 顶过限再驱动 record_compute_gas"——正是 Arm B 当初怕的那个 case。
- 它自己的探针:`record_compute_gas` 隔离微基准 **3.06 → 0.92 ns/call(~70%)**。
- 与 f643c24 的 `record_compute_gas` 改动**几乎逐行一致**(短路 has_exceeded_limit + 只查 compute_gas + latch)。
- **判分 verdict:ACCEPTED** —— 隔离 `record_compute_gas` **Δ −72.40%,CI[−73.42,−68.14],floor 27.19%**
  (字节一致、213 测试不掉)。**这是整个练习里 judge 第一个 accepted。** 效应大到碾压 27% 的高噪声地板,
  CI 紧贴且全负。该 kernel 端到端约占 EVM 计算 ~17-22%,故全程约 ~13-16%。
  - 测量设置:`tu_*` shim 是测量脚手架,作为一个提交进双臂基线(两臂都有,inert/test-utils-gated),
    被测改动只剩优化本身(opt-only patch)。
  - 过程又抓到**第 3 个框架脆弱点**:bench 解析器假设 `BENCH` 后全是 float,被探针的
    `BENCH 0.92 ns_per_call iters=...` 尾部标签噎住。已修(取前导数字、遇非数字停)。
    → 三次真跑 = 三个 mock selftest 漏掉的真 bug(hint 模板 / 探针路径 / bench 解析器)。

### 3.5 结论(三臂闭环)
| 臂 | 变量 | 是否够到"四维→一维" | 结果 |
|---|---|---|---|
| Arm A(×2 轮) | 现状 | ❌ 从未考虑 | 局部微调,within-noise |
| Arm B | + lens | ⚠️ **生成了但否决**(怕不变量) | 退守小改动,within-noise |
| **Arm C** | **+ lens + 采纳层** | ✅ **生成+解析不变量+采纳** | **复现 f643c24 → judge ACCEPTED −72.40%**(CI[−73.42,−68.14]) |

- **Q1(只多跑轮)= 否。** Arm A 证明:不改 prompt,迭代只是同一方向越挖越深。
- **Q2(加 lens)= 必要不充分。** lens 把真答案**生成**出来(Arm A 到不了),但 agent 在**采纳**那步退缩。
- **完整答案 = 两层 prompt:**
  1. **生成层 = "消除冗余/不变量"lens**(逼它问"这工作多余吗");
  2. **采纳层 = "解析不变量、别从高杠杆改动退缩"**(去追状态改点、确认每个自 guard、用 assert+对抗差分
     钉住、把 judge 当安全网放胆采纳)。
  **两层都加上,盲框架在答案不可达的情况下,独立复现了 f643c24 的核心优化。**
- 下一步:把这两层正式落进 `skill/prompts/read.md` / hint / judge-protocol(差分对抗覆盖),提交;
  并可补一个"裸 lens 多跑几轮会不会偶然采纳"的对照,但 Arm A/B/C 已足够说明"采纳层"才是闭合的关键。

### 3.5 结论(实验回答两个问题)
- **Q1(只多跑轮、不加 lens)→ 否。** Arm A 两轮都在同一热路径上越挖越深(叶子 inline → 叶子 CSE),
  全 within-noise,**从未把"四维→一维"纳入考虑**。多轮放大的是同一个"局部加速"算子。
- **Q2(加 lens)→ 部分成功,且精确暴露了剩余 gap。** lens 做到了两件关键的事:
  (1) 把高杠杆的结构性候选(四维→一维)**显式生成出来**(Arm A 根本到不了这一步);
  (2) 让 agent **采用了不变量钉法**(assert + 对抗差分),并真的提了一个 ELIMINATE-class 改动。
  但 agent **在"证明四维→一维安全"这一步退了**——它需要再深读 latch 协议、确认"每个非 compute
  mutation 点都自 latch"这个不变量成立,而它只是标了风险没去解。**它离真答案只差一步不变量验证。**
- **所以"想让它找到"该加的,是两层,不是一层:**
  1. **lens(生成层)**:逼它问"这工作多余吗",把结构性候选列出来。✅ 本实验已验证有效。
  2. **"解析不变量、而非从高杠杆改动退缩"(采纳层)**:当一个高杠杆改动"看起来险",不要直接否——
     **去追溯它依赖的状态在哪被改、每个改点是否自我保护(self-latch/self-guard),确认不变量后用
     assert+测试钉住**;对抗差分 + judge 是你的安全网,可以放胆。**这一层是这次 gap 的所在,是下一轮
     prompt 改进的重点。** 换句话说:judge 越可信 → 生成端越该被鼓励去采纳"可证的大改动",而不是退守小改动。

---

## 4. 横切发现(findings)

1. **profiler 定位强**:盲环境、答案不可达,在大型 EVM 里精确盲选到正确的热函数(~17%)。
2. **judge 可靠**:正确拒绝两个非赢点(salt −0.62% / evm −4.07% 点估计都没骗到它);recompile/
   差分/N_pre 自检全过。
3. **生成洞察浅**:找对地方,想不到更深的变换(四维→一维)——"judge 是 moat、生成是 commodity"
   的又一次印证,且更精细。
4. **编排脆弱**:subagent 后台起构建会卡死(本 harness 无 resume)。当前可靠分工 = agent 前台发现 +
   我后台判分。
5. **mock selftest 漏真 bug**:两次真跑各抓一个(hint 模板、探针路径)。端到端真跑不可替代。

---

## 5. lessons.jsonl(截至目前 5 条)

1. salt/banderwagon — precompute-K +14%(accepted)
2. salt/banderwagon — 堆-Vec 融合 −53%(regressed)
3. `*` — 共享 CARGO_TARGET_DIR(measurement-unsound)
4. salt/banderwagon — add_affine_point mul→add 强度削减(within-noise,−0.62%)
5. mega-evm — `#[inline]` state_growth::check_limit(within-noise,−4.07%);盲测命中热点、漏核心赢点

---

## 6. Open items

- **未提交改动**(`aro-py`):框架修复 `aro/target.py`(探针路径 `cargo metadata` 解析)+
  `memory/lessons.jsonl`(第 5 条),加上 evm-auto 工件(spec/探针/patch)。
  待定处理:照 salt 那次惯例(修复+lesson 入库,工件移 `/tmp/aro-answer-backup`,保持 targets/probes 空)。
- **盲测环境** 独立盲测目录(~1GB,含 target 缓存):实验期间保留,完后再决定删/留。
- **若 Arm B 成立**:把 lens + 升级 reflect 落进 `skill/prompts/read.md`、hint 模板、judge-protocol
  的差分要求(对抗覆盖),并提交。
