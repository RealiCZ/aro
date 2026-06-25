<script lang="ts">
  import { DATA, NODES } from './data';
  import { col, dpct } from './colors';
  import DiffView from './DiffView.svelte';
  import type { Detail, FnNode, TreeNode } from './types';

  let { detail, setDetail }: { detail: Detail; setDetail: (d: Detail) => void } =
    $props();

  const s = DATA.summary;
  // merged display nodes (repeated attempts of a function collapsed into one)
  const fns = NODES.filter((n): n is FnNode => n.type === 'fn');
  const skipped = NODES.filter((n) => n.type === 'skipped');

  const regimeCn = (r?: string | null): string =>
    r && r !== 'byte-identical'
      ? '需人工复核(动了结构,不建议直接合)'
      : '行为不变(可直接合)';
  // the second judge's verdict → colour + label
  const cvColor = (v: string): string =>
    v === 'reject' ? '#dc2626' : v === 'pass-risk' ? '#ca8a04' : '#16a34a';
  const cvLabel = (v: string): string =>
    v === 'reject'
      ? '否决(判分前拦下,省了那条串行 bench)'
      : v === 'pass-risk'
        ? '通过 · 有风险(要人复核)'
        : '通过(无异议)';
  const segPct = (key: string): number =>
    s.coverage.find((c) => c.key === key)?.pct ?? 0;

  // covList row click -> jump to that node's full detail (Icicle re-syncs via $effect).
  function jump(n: TreeNode) {
    if (n.type === 'skipped') setDetail({ kind: 'skip', node: n });
    else setDetail({ kind: 'fn', node: n, ci: 0 });
  }

  // 碰不得 floor: group not-ours frames owner -> why(crate) -> frames.
  const floorGroups = $derived.by(() => {
    const g: Record<string, Record<string, typeof s.floor_frames>> = {};
    for (const f of s.floor_frames ?? []) {
      const o = f.owner || '?';
      const w = f.why || '?';
      (g[o] ??= {});
      (g[o][w] ??= []).push(f);
    }
    return g;
  });
  const ownerName = (o: string): string =>
    o === 'crypto'
      ? 'crypto(密码学)'
      : o === 'runtime'
        ? 'runtime(运行时/框架)'
        : o;
  const ownerSum = (o: string): number => {
    let t = 0;
    for (const w of Object.values(floorGroups[o] ?? {}))
      for (const f of w) t += f.pct || 0;
    return t;
  };
  const headroomLeft = $derived(
    (s.frontier ?? []).filter((f) => !fns.some((n) => n.fn === f)),
  );
</script>

{#snippet fnlist(items: TreeNode[])}
  {#if !items.length}
    <div class="muted mt">(本轮无)</div>
  {:else}
    {#each items as n (n.id)}
      {@const st = n.type === 'skipped' ? 'skipped' : (n.status ?? '')}
      <div
        class="covrow"
        onclick={() => jump(n)}
        role="button"
        tabindex="0"
        onkeydown={(e) => e.key === 'Enter' && jump(n)}
      >
        <code>{n.fn}</code>
        <span>
          {#if n.type === 'fn' && n.pct != null}{n.pct}% · {/if}<span
            style:color={col(st)}
            style="font-weight:600"
            >{st}{#if n.type === 'fn' && typeof n.delta === 'number'}{' ' +
                dpct(n.delta)}{/if}</span
          > ▸
        </span>
      </div>
    {/each}
  {/if}
{/snippet}

{#if !detail}
  <div class="hint">← 点左边任意节点,看当时的报告</div>
{:else if detail.kind === 'fn'}
  {@const n = detail.node}
  {@const ci = detail.ci < (n.candidates?.length ?? 0) ? detail.ci : 0}
  {@const c = n.candidates?.[ci]}
  <!-- FUNCTION-LEVEL summary (整体) — sticky + distinct card so it reads as an overall
       header, NOT as the selected candidate's detail (which scrolls under it below). -->
  <div class="fnhead">
    <span class="tag tag-fn">本函数 · 整体</span>
    <h2>
      {n.i}. <code>{n.fn}</code>
      <span style:color={col(n.status)}
        >· {n.status}{#if typeof n.delta === 'number'}{' ' + dpct(n.delta)}{/if}</span
      >
    </h2>
    <div class="sub">
      {(n.attempts?.length ?? 1) > 1 ? n.attempts?.length + ' 次尝试' : '第 ' + n.i + ' 个尝试'}
      · {regimeCn(n.regime)} · 占运行时 {n.pct != null ? n.pct + '%' : '-'}
    </div>
    <div class="fnstat">
      <span class="fk">本函数贡献</span>
      {#if n.accepted && typeof n.delta === 'number'}<b style="color:#16a34a"
          >快 {(-n.delta).toFixed(2)}%</b
        ><span class="fnote"
          >— 只算被采纳的那 1 次尝试;同函数其它尝试(build-failed / 没过)不计入</span
        >{:else}<span class="muted">0%(未落地 · {n.status ?? '—'})</span>{/if}
    </div>
    {#if n.decision}
      <div class="fnstat">
        <span class="fk">探索器判定</span>
        <b style:color={n.decision === 'STOP' ? '#dc2626' : '#16a34a'}>{n.decision}</b>
        <span class="fnote">— {n.reason ?? ''}</span>
      </div>
    {/if}
    {#if n.files && n.files.length}
      <div class="fnstat">
        <span class="fk">编辑范围</span>
        <span>{#each n.files as f}<code>{f}</code>{' '}{/each}</span>
      </div>
    {/if}
  </div>

  <!-- per-candidate detail (各自详情) — the part that changes as you click each attempt -->
  {#if c}
    <span class="tag tag-cand"
      >本次尝试 · 详情{#if (n.candidates?.length ?? 1) > 1} ({ci + 1}/{n.candidates?.length}){/if}</span
    >
    <h3 class="ch">
      候选 <code
        >{#if (n.attempts?.length ?? 1) > 1}<span class="att">#{c._attempt}</span>
        {/if}{c.id}</code
      > ·
      <span style:color={col(c.verdict)}>{c.verdict}</span>
    </h3>
    <div class="hyp"><b>改了什么:</b> {c.hypothesis}</div>
    {#if c.critic}
      <div class="critic">
        <div class="critic-h">
          语义评审(第二道 judge):<span
            style:color={cvColor(c.critic.verdict)}
            style="font-weight:700">{cvLabel(c.critic.verdict)}</span>
        </div>
        {#if c.critic.reasons && c.critic.reasons.length}
          {#each c.critic.reasons as r}
            <div class="creason" class:sev-high={r.severity === 'high'}>
              <span class="rb">[{r.rubric}]</span>
              {r.finding}{#if r.example}<span class="ex"> (cf. {r.example})</span>{/if}{#if r.severity && r.severity !== 'none'}<span
                  class="sev"> · {r.severity}</span>{/if}
            </div>
          {/each}
        {/if}
      </div>
    {/if}
    {#if c.metrics && c.metrics.length}
      <table class="m">
        <thead
          ><tr><th>metric</th><th>Δ</th><th>CI</th><th>floor</th><th>improved</th
            ></tr></thead
        >
        <tbody>
          {#each c.metrics as m}
            <tr
              ><td>{m.metric}</td><td>{dpct(m.delta_pct)}</td><td
                >[{(m.ci_low_pct ?? 0).toFixed(2)}, {(m.ci_high_pct ?? 0).toFixed(
                  2,
                )}]</td
              ><td>{(m.floor_pct ?? 0).toFixed(2)}%</td><td
                >{m.improved ? '✓' : '—'}</td
              ></tr
            >
          {/each}
        </tbody>
      </table>
    {/if}
    {#if c.diff}
      <details open class="diffdet">
        <summary>代码改动(diff)</summary>
        <DiffView diff={c.diff} />
      </details>
    {/if}
  {:else}
    <div class="muted mt">(无候选记录)</div>
  {/if}
{:else if detail.kind === 'skip'}
  <h2><code>{detail.node.fn}</code> · <span style="color:#ea580c">skipped</span></h2>
  <div class="sub">{detail.node.reason ?? 'source not located'}</div>
  <div class="muted mt">
    这个热帧在 workspace 源码里找不到对应的 <code>fn</code>(宏生成 / 内联 /
    demangler 残留)→ 无处下手,跳过。
  </div>
{:else if detail.kind === 'reflect'}
  <h2 style="color:#7c3aed">
    reflect 方向 [{detail.dir.id}] · <span class="muted">未试</span>
  </h2>
  <div class="rtext">{detail.dir.text}</div>
  <div class="muted mt">
    这是 agent 在该轮 reflect 阶段提出的下一步想法,但在停机前没轮到试。
  </div>
{:else if detail.kind === 'cov'}
  {@const key = detail.key}
  {#if key === 'captured'}
    <h2>已优化(accept) {segPct(key)}% — 落地的优化</h2>
    <div class="sub">
      判过完整 judge、确认真提速的函数;它们的 Δ 计入累计 realized。点进去看候选 +
      代码改动。
    </div>
    {@render fnlist(fns.filter((n) => n.accepted))}
  {:else if key === 'tried'}
    <h2>试过没过 {segPct(key)}% — 打了但没赢</h2>
    <div class="sub">
      judge 判过但没过(噪声内 / noise-limited / 变慢)。诚实记录:这些不计入
      realized。
    </div>
    {@render fnlist(fns.filter((n) => !n.accepted))}
  {:else if key === 'unreachable'}
    <h2>够不着 {segPct(key)}% — 无 fn 可定位</h2>
    <div class="sub">热帧在 workspace 找不到对应 fn(内联/宏/demangler 残留),无处下手。</div>
    {@render fnlist(skipped)}
  {:else if key === 'headroom'}
    <h2>未试(headroom) {segPct(key)}%</h2>
    <div class="sub">
      还能定位、本轮预算没轮到打的我方函数(Amdahl 上界)。再跑一轮可继续挖。
    </div>
    {#if headroomLeft.length}
      {#each headroomLeft as f}
        <div class="covrow"><code>{f}</code><span class="muted">未试</span></div>
      {/each}
    {:else}
      <div class="muted mt">
        本轮前沿基本打完;剩余 headroom 来自 re-profile 后才浮现的小函数,没有固定名单。
      </div>
    {/if}
  {:else if key === 'floor'}
    <h2>碰不得 — 动不了的底座 {s.floor_pct.toFixed(1)}%</h2>
    <div class="sub">
      这些热帧不在我方代码里(上游 crypto / 运行时库)—— ARO 不能改。按所属 crate
      归组,占运行时越多越靠前。
    </div>
    {#if !(s.floor_frames && s.floor_frames.length)}
      <div class="muted mt">
        本轮没记明细(旧 run)。新一轮探索会记下每个碰不得的热帧 + 它属于哪个上游
        crate,这里就能展开成树。
      </div>
    {:else}
      {#each Object.keys(floorGroups) as owner}
        <div class="own">
          {ownerName(owner)} <span class="muted">≈{ownerSum(owner).toFixed(1)}%</span>
        </div>
        {#each Object.keys(floorGroups[owner]) as why}
          <div class="why">▸ <code>{why}</code></div>
          {#each [...floorGroups[owner][why]].sort((a, b) => (b.pct || 0) - (a.pct || 0)) as f}
            <div class="frame">
              <code>{f.name}</code><span class="muted">{(f.pct || 0).toFixed(1)}%</span>
            </div>
          {/each}
        {/each}
      {/each}
    {/if}
  {:else}
    <h2>其它/未归类 {segPct(key)}%</h2>
    <div class="sub">
      bench 里未归入上述类别的零散帧(测量误差 + 未分类的小帧),没有可下钻的函数。
    </div>
  {/if}
{/if}

<style>
  h2 {
    font-size: 16px;
    font-weight: 700;
    letter-spacing: -0.01em;
    margin: 0 0 5px;
  }
  .sub {
    color: #64748b;
    font-size: 12px;
    margin-bottom: 14px;
    line-height: 1.5;
  }
  /* function-level summary: sticky card, bleeds over #detail's 22/26px padding so it
     sits flush at the top and stays put while the candidate detail scrolls under it. */
  .fnhead {
    position: sticky;
    top: 0;
    z-index: 5;
    margin: -22px -26px 16px;
    padding: 14px 26px 13px;
    background: linear-gradient(180deg, #ffffff, #f6f9ff);
    border-bottom: 1px solid #e3e9f2;
    box-shadow: 0 6px 12px -8px rgba(15, 23, 42, 0.18);
  }
  .fnhead h2 {
    margin: 0 0 4px;
  }
  .fnhead .sub {
    margin-bottom: 9px;
  }
  .tag {
    display: inline-block;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.05em;
    padding: 1px 8px;
    border-radius: 999px;
  }
  .tag-fn {
    color: #2563eb;
    background: #eaf1ff;
    border: 1px solid #d6e4ff;
    margin-bottom: 8px;
  }
  .tag-cand {
    color: #64748b;
    background: #f1f5f9;
    border: 1px solid #e2e8f0;
  }
  .fnstat {
    font-size: 12.5px;
    line-height: 1.55;
    margin: 4px 0;
  }
  .fnstat .fk {
    display: inline-block;
    min-width: 72px;
    color: #64748b;
    margin-right: 8px;
  }
  .fnstat .fnote {
    color: #94a3b8;
    margin-left: 6px;
  }
  .ch {
    margin-top: 7px;
    font-size: 13px;
  }
  .hyp {
    font-size: 12.5px;
    line-height: 1.65;
    margin: 8px 0;
    padding: 10px 12px;
    background: #f7f9fc;
    border: 1px solid #eef2f8;
    border-radius: 8px;
  }
  .rtext {
    font-size: 13px;
    line-height: 1.7;
    margin-top: 10px;
  }
  .hint {
    color: #94a3b8;
    font-size: 13px;
    margin-top: 40px;
    text-align: center;
  }
  .muted {
    color: #94a3b8;
  }
  .mt {
    margin-top: 14px;
  }
  table.m {
    border-collapse: collapse;
    font-size: 12px;
    margin: 10px 0;
    border-radius: 8px;
    overflow: hidden;
    box-shadow: 0 0 0 1px #e8edf4;
  }
  table.m :global(td),
  table.m :global(th) {
    border: 1px solid #eef2f8;
    padding: 5px 10px;
  }
  table.m :global(th) {
    background: #f7f9fc;
    font-weight: 600;
    color: #475569;
  }
  .diffdet {
    margin-top: 12px;
  }
  .diffdet > summary {
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    color: #334155;
    user-select: none;
  }
  .covrow {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    font-size: 12.5px;
    padding: 9px 12px;
    margin: 5px 0;
    border: 1px solid #e8edf4;
    border-radius: 9px;
    background: #fff;
    cursor: pointer;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    transition:
      transform 0.12s ease,
      box-shadow 0.12s ease,
      border-color 0.12s ease;
  }
  .covrow:hover {
    border-color: #c3ccda;
    box-shadow: 0 4px 12px rgba(15, 23, 42, 0.09);
    transform: translateY(-1px);
  }
  .own {
    margin-top: 14px;
    font-weight: 700;
    font-size: 13px;
  }
  .why {
    margin: 5px 0 2px 8px;
    color: #64748b;
    font-size: 12px;
  }
  .frame {
    margin-left: 22px;
    font-size: 12px;
    display: flex;
    justify-content: space-between;
    max-width: 440px;
    padding: 1px 0;
  }
  .att {
    color: #94a3b8;
    font-weight: 400;
    margin-right: 5px;
  }
  .critic {
    margin: 10px 0;
    padding: 10px 12px;
    border: 1px solid #e8edf4;
    border-left: 3px solid #94a3b8;
    border-radius: 8px;
    background: #fbfcfe;
  }
  .critic-h {
    font-size: 12.5px;
    font-weight: 600;
    color: #334155;
    margin-bottom: 6px;
  }
  .creason {
    font-size: 11.5px;
    line-height: 1.5;
    color: #475569;
    padding: 5px 9px;
    border-radius: 7px;
    background: #f7f9fc;
    border: 1px solid #eef2f8;
    margin: 4px 0;
  }
  .creason.sev-high {
    background: #fef2f2;
    border-color: #fecaca;
    color: #7f1d1d;
  }
  .creason .rb {
    font-weight: 700;
    color: #334155;
  }
  .creason .ex {
    color: #b91c1c;
    font-weight: 600;
  }
  .creason .sev {
    color: #94a3b8;
  }
</style>
