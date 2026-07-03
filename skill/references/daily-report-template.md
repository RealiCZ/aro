<!--
ARO 优化日报 — 填充模板。占位符 {{...}} 由 daily-report-protocol.md 从一轮 explore 的
产物(events.jsonl / a{N}/patches / a{N}/records.jsonl / trajectory.svg)填实。
所有数字 VERBATIM 从 events.jsonl 抄,不得二次判分。结构照 PR#313 优化方案文档:
TL;DR callout → 一、改了什么(含代码)→ 二、提升多少 → 三、后续方向。
四个必答:改了什么 / 提升多少 / 改了什么代码 / 后续做什么。
-->

# ARO 优化日报 · {{target}} · {{date}}

**本轮负载**:{{workload_description}} _(探针文件 `{{workload_probe}}`)_

> 🕵️ **TL;DR**
> - **目的**:在上述负载上,自动找 byte-identical 的优化,并用确定性 judge 证明不是噪声。
> - **做了什么**:自动尝试 {{n_attempts}} 个热函数 → **{{n_accepts}} 个落地**({{accept_one_liner}}),{{n_within_noise}} 个在噪声内未过。
> - **结果**:该负载整体 **快 {{realized}}%**,judge 证明(A/A 地板 + 配对 A/B + differential)。{{relaxed_note}}
> - **判定**:**{{decision}}** —— {{decision_reason}}

## 一、改了什么(含代码)

**落地 {{n_accepts}} 项**(judge 判 accepted):

| 改了什么 | 为什么是浪费 | 文件 | Δ |
|---|---|---|---|
{{#each accept}}| {{what}} | {{why_waste}} | `{{file}}` | **{{delta}}%** |
{{/each}}

{{#each accept}}
> **代码({{fn}})**:{{code_summary}}。完整 patch:`{{patch_path}}`。
{{/each}}

**试了但没过**(诚实记录 —— 像消融把"Vec→字段零变化"那条记下来关闭方向,不只报成功的优化):

| 函数 | Δ | 结论 |
|---|---|---|
{{#each within_noise}}| `{{fn}}` | {{delta}}% | {{note}} |
{{/each}}

**跳过(够不着)**:{{skipped_fns}} —— 宏生成 / 内联,无 `fn` 可定位(占 {{unreachable}}%,见后续 D3)。

## 二、性能提升了多少

![trajectory](trajectory.png)

_图:realized(蓝实线↑,已优化 % faster) vs addressable headroom(橙虚线↓,剩余可优化);空心橙点 = relaxed 档的优化(要人定);末端框 = decision {{decision}}。(图内文字为英文)_
<!-- 必须用 PNG,不要用 .svg:markdown 预览基本不渲染 SVG。先 `qlmanage -t -s 1100 -o DIR DIR/trajectory.svg && mv DIR/trajectory.svg.png DIR/trajectory.png`。 -->


| 量 | 值 | 含义 |
|---|---|---|
| **进化了 (realized)** | 快 {{realized}}% | 复利累计,bench 实测,{{n_accepts}} 个 accept |
| **能进化的 (addressable)** | {{addressable}}% | 还能定位、还没打的自家函数(Amdahl 上界) |
| **够不着的 (unreachable)** | {{unreachable}}% | 宏生成 / 内联,暂无法命名 |
| **碰不得的 (floor)** | ≈{{floor}}% | not-ours({{floor_owners}}) |

> 测量:A/A 噪声地板标定 → 配对 A/B(顺序随机)→ bootstrap CI 排除 0 → 随机输入 differential 证字节相同。Δ 全部 VERBATIM 自 events.jsonl,非二次判分。

## 三、后续需要做什么

由本轮末态**确定性合成**(不靠猜),挑一个作下一轮:

| 方向 | 解锁什么 | 代价 | 谁决定 |
|---|---|---|---|
{{#each direction}}| **{{id}}** {{title}} | {{unlocks}} | {{cost}} | {{owner}} |
{{/each}}

> 诚实提示:同负载、同约束档,明天再跑大概率开场即 STOP(本轮可达的都标 tried 了)。
> 让下一轮有产出的,是换约束档的决策(换负载 / 放宽规则 / 升级优化手法)—— judge 不替你做。

**需要你现在拍板**
{{#each decision_needed}}{{n}}. {{text}}
{{/each}}

---

> **术语**:**约束档(regime)** = 找这处优化时守的规则。**字节相同** = 行为完全不变,可直接合;**放宽(relaxed)** = 动了结构、不该直接合,要人定夺(should-not-merge)。**优化/优化成功** = 候选被确定性 judge 判过、确认真提速。
