<script lang="ts">
  import { DATA, NODES } from './data';
  import { col, dpct, T } from './colors';
  import DiffView from './DiffView.svelte';
  import type { Detail, FnNode, TreeNode } from './types';

  let { detail, setDetail }: { detail: Detail; setDetail: (d: Detail) => void } = $props();

  const s = DATA.summary;
  const fns = NODES.filter((n): n is FnNode => n.type === 'fn');
  const skipped = NODES.filter((n) => n.type === 'skipped');

  const isMerge = (n: FnNode): boolean =>
    !!n.accepted && (!n.regime || n.regime === 'byte-identical');
  const cvColor = (v: string): string =>
    v === 'reject' ? T.regress : v === 'pass-risk' ? '#D9A23B' : T.accept;
  const cvLabel = (v: string): string =>
    v === 'reject'
      ? '否决 · 判分前拦下,省了串行 bench'
      : v === 'pass-risk'
        ? '通过 · 有风险(要人复核)'
        : '通过 · 无异议';
  const segPct = (key: string): number => s.coverage.find((c) => c.key === key)?.pct ?? 0;

  function jump(n: TreeNode) {
    if (n.type === 'skipped') setDetail({ kind: 'skip', node: n });
    else setDetail({ kind: 'fn', node: n, ci: 0 });
  }

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
    o === 'crypto' ? 'crypto · 密码学' : o === 'runtime' ? 'runtime · 运行时/框架' : o;
  const ownerSum = (o: string): number => {
    let t = 0;
    for (const w of Object.values(floorGroups[o] ?? {})) for (const f of w) t += f.pct || 0;
    return t;
  };
  const headroomLeft = $derived((s.frontier ?? []).filter((f) => !fns.some((n) => n.fn === f)));
</script>

{#snippet fnlist(items: TreeNode[])}
  {#if !items.length}
    <div class="mute mt">(本轮无)</div>
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
        <span class="mono"
          >{#if n.type === 'fn' && n.pct != null}{n.pct}% · {/if}<span style:color={col(st)}
            >{st}{#if n.type === 'fn' && typeof n.delta === 'number'}{' ' + dpct(n.delta)}{/if}</span
          > ▸</span
        >
      </div>
    {/each}
  {/if}
{/snippet}

<div class="pad">
  {#if !detail}
    <div class="hint">← 点左边火焰图任一帧,看它的 dossier<br /><span class="mute">条长 = 自时间 · 填色 = 热度 · 字形 = 判定</span></div>
  {:else if detail.kind === 'fn'}
    {@const n = detail.node}
    {@const ci = detail.ci < (n.candidates?.length ?? 0) ? detail.ci : 0}
    {@const c = n.candidates?.[ci]}
    <div class="dtag">dossier · selected frame</div>
    <div class="dh">
      <code>{n.fn}</code>
      <span class="pill" class:merge={isMerge(n)} class:rev={n.accepted && !isMerge(n)}
        >{n.accepted ? (isMerge(n) ? 'byte-identical · 可合' : 'relaxed · 需复核') : (n.status ?? '—')}</span
      >
    </div>
    <div class="dsub mono">
      本函数贡献 {#if n.accepted && typeof n.delta === 'number'}<b>快 {(-n.delta).toFixed(2)}%</b
        >{:else}<span class="mute">0% · 未落地</span>{/if} · 占运行时 {n.pct ?? '—'}% ·
      {(n.attempts?.length ?? 1) > 1 ? n.attempts?.length + ' 次尝试' : '1 次'}
    </div>

    {#if n.decision}
      <div class="row">
        <k>探索器判定</k>
        <div>
          <b style:color={n.decision === 'STOP' ? T.regress : T.signal}>{n.decision}</b>
          <span class="mute">{n.reason ?? ''}</span>
        </div>
      </div>
    {/if}
    {#if n.files && n.files.length}
      <div class="row">
        <k>编辑范围</k>
        <div class="files">{#each n.files as f}<code>{f}</code>{/each}</div>
      </div>
    {/if}

    {#if c}
      <div class="candhdr mono">
        选中候选 <code>{#if (n.attempts?.length ?? 1) > 1}#{c._attempt} {/if}{c.id}</code> ·
        <span style:color={col(c.verdict)}>{c.verdict}</span>
        <span class="mute">— 在中栏点别的候选切换</span>
      </div>
      <div class="change"><b>改了什么</b> {c.hypothesis}</div>
      {#if c.critic}
        <div class="critic">
          <div class="ch">
            语义评审 · 第二道 judge — <span style:color={cvColor(c.critic.verdict)} style="font-weight:600"
              >{cvLabel(c.critic.verdict)}</span
            >
          </div>
          {#each c.critic.reasons ?? [] as r}
            <div class="cr" class:hi={r.severity === 'high'}>
              <span class="rb">[{r.rubric}]</span>
              {r.finding}{#if r.example}<span class="ex"> (cf. {r.example})</span>{/if}
            </div>
          {/each}
        </div>
      {/if}
      {#if c.metrics && c.metrics.length}
        <table class="m">
          <thead
            ><tr><th>metric</th><th>Δ</th><th>CI</th><th>floor</th><th>✓</th></tr></thead
          >
          <tbody>
            {#each c.metrics as m}
              <tr
                ><td>{m.metric}</td><td>{dpct(m.delta_pct)}</td><td
                  >[{(m.ci_low_pct ?? 0).toFixed(2)}, {(m.ci_high_pct ?? 0).toFixed(2)}]</td
                ><td>{(m.floor_pct ?? 0).toFixed(2)}%</td><td>{m.improved ? '✓' : '—'}</td></tr
              >
            {/each}
          </tbody>
        </table>
      {/if}
      {#if c.diff}
        <details open class="dd">
          <summary>代码改动 · diff</summary>
          <DiffView diff={c.diff} />
        </details>
      {/if}
    {:else}
      <div class="mute mt">(无候选记录)</div>
    {/if}
  {:else if detail.kind === 'skip'}
    <div class="dtag">dossier · skipped frame</div>
    <div class="dh"><code>{detail.node.fn}</code><span class="pill rev">skipped</span></div>
    <div class="dsub mono">{detail.node.reason ?? 'source not located'}</div>
    <div class="mute mt">
      这个热帧在 workspace 源码里找不到对应 <code>fn</code>(宏生成 / 内联 / demangler 残留)→ 无处下手,跳过。
    </div>
  {:else if detail.kind === 'reflect'}
    <div class="dtag" style="color:var(--critic)">dossier · reflect 未试方向</div>
    <div class="dh"><code>[{detail.dir.id}]</code><span class="pill rev">未试</span></div>
    <div class="rtext">{detail.dir.text}</div>
  {:else if detail.kind === 'cov'}
    {@const key = detail.key}
    {#if key === 'captured'}
      <div class="dtag">已优化 · accept {segPct(key)}%</div>
      <div class="dsub mono">判过完整 judge、确认真提速的函数 — Δ 计入累计 realized。</div>
      {@render fnlist(fns.filter((n) => n.accepted))}
    {:else if key === 'tried'}
      <div class="dtag">试过没过 {segPct(key)}%</div>
      <div class="dsub mono">judge 判过但没过(噪声内 / noise-limited / 变慢)— 不计入 realized。</div>
      {@render fnlist(fns.filter((n) => !n.accepted))}
    {:else if key === 'unreachable'}
      <div class="dtag">够不着 {segPct(key)}%</div>
      <div class="dsub mono">热帧在 workspace 找不到对应 fn(内联/宏/demangler 残留),无处下手。</div>
      {@render fnlist(skipped)}
    {:else if key === 'headroom'}
      <div class="dtag">未试 · headroom {segPct(key)}%</div>
      <div class="dsub mono">还能定位、本轮预算没轮到打的我方函数(Amdahl 上界)。再跑可继续挖。</div>
      {#if headroomLeft.length}
        {#each headroomLeft as f}
          <div class="covrow"><code>{f}</code><span class="mute mono">未试</span></div>
        {/each}
      {:else}
        <div class="mute mt">本轮前沿基本打完;剩余 headroom 来自 re-profile 后浮现的小函数。</div>
      {/if}
    {:else if key === 'floor'}
      <div class="dtag" style="color:var(--ink2)">碰不得 · 动不了的底座 {s.floor_pct.toFixed(1)}%</div>
      <div class="dsub mono">不在我方代码里(上游 crypto / 运行时库)— ARO 不能改。按 crate 归组。</div>
      {#if !(s.floor_frames && s.floor_frames.length)}
        <div class="mute mt">本轮没记明细(旧 run)。</div>
      {:else}
        {#each Object.keys(floorGroups) as owner}
          <div class="own">{ownerName(owner)} <span class="mute mono">≈{ownerSum(owner).toFixed(1)}%</span></div>
          {#each Object.keys(floorGroups[owner]) as why}
            <div class="why mono">▸ <code>{why}</code></div>
            {#each [...floorGroups[owner][why]].sort((a, b) => (b.pct || 0) - (a.pct || 0)) as f}
              <div class="frameln mono"><code>{f.name}</code><span class="mute">{(f.pct || 0).toFixed(1)}%</span></div>
            {/each}
          {/each}
        {/each}
      {/if}
    {:else}
      <div class="dtag">其它/未归类 {segPct(key)}%</div>
      <div class="dsub mono">bench 里未归入上述类别的零散帧(测量误差 + 未分类小帧)。</div>
    {/if}
  {/if}
</div>

<style>
  .pad {
    padding: 15px 17px;
  }
  .mono {
    font-family: var(--mono);
  }
  .mute {
    color: var(--mute);
  }
  .mt {
    margin-top: 12px;
    font-size: 12.5px;
    line-height: 1.55;
  }
  .hint {
    color: var(--mute);
    font-size: 13px;
    text-align: center;
    margin-top: 60px;
    line-height: 1.9;
  }
  .dtag {
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.16em;
    color: var(--signal);
    text-transform: uppercase;
  }
  .dh {
    font-family: var(--disp);
    font-weight: 600;
    font-size: 21px;
    margin: 7px 0 3px;
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .dh code {
    font-size: 18px;
    color: var(--ink);
  }
  .pill {
    font-family: var(--mono);
    font-size: 10.5px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 2px;
    color: var(--ink2);
    border: 1px solid var(--rule2);
  }
  .pill.merge {
    background: rgba(139, 233, 196, 0.13);
    color: var(--merge);
    border-color: rgba(139, 233, 196, 0.3);
  }
  .pill.rev {
    background: rgba(232, 96, 63, 0.1);
    color: #f0a08c;
    border-color: rgba(232, 96, 63, 0.3);
  }
  .dsub {
    font-size: 12px;
    color: var(--ink2);
    margin-bottom: 13px;
    line-height: 1.5;
  }
  .dsub b {
    color: var(--accept);
  }
  .row {
    display: grid;
    grid-template-columns: 80px 1fr;
    gap: 4px 12px;
    font-size: 12.5px;
    padding: 8px 0;
    border-top: 1px solid var(--rule);
  }
  .row k {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--mute);
  }
  .files {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .files code {
    font-size: 11.5px;
    color: var(--ink2);
    word-break: break-all;
  }

  .striplab {
    font-size: 10.5px;
    letter-spacing: 0.1em;
    color: var(--mute);
    text-transform: uppercase;
    margin: 16px 0 7px;
  }
  .cstrip {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .chip {
    background: var(--panel2);
    border: 1px solid var(--rule2);
    border-radius: 3px;
    padding: 5px 9px;
    cursor: pointer;
    font-size: 11px;
    color: var(--ink2);
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-family: var(--mono);
  }
  .chip:hover {
    border-color: var(--mute);
  }
  .chip.on {
    background: #1a2731;
    border-color: var(--signal);
    box-shadow: 0 0 0 1px rgba(63, 224, 197, 0.25);
  }
  .chip code {
    font-size: 11px;
    color: var(--ink);
  }
  .chip i {
    font-style: normal;
  }

  .candhdr {
    font-size: 11.5px;
    color: var(--ink2);
    margin: 16px 0 0;
    padding-top: 12px;
    border-top: 1px solid var(--rule);
  }
  .candhdr code {
    color: var(--ink);
  }
  .change {
    font-size: 12.5px;
    line-height: 1.6;
    margin: 10px 0 0;
    padding: 11px 12px;
    background: var(--panel2);
    border: 1px solid var(--rule);
    border-left: 2px solid var(--signal);
    border-radius: 3px;
    color: var(--ink);
  }
  .change b {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--signal);
    margin-right: 8px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .critic {
    margin: 12px 0;
    padding: 11px 12px;
    border: 1px solid var(--rule2);
    border-left: 2px solid var(--critic);
    border-radius: 3px;
    background: rgba(185, 139, 255, 0.05);
  }
  .ch {
    font-family: var(--mono);
    font-size: 11.5px;
    color: var(--ink);
    margin-bottom: 7px;
  }
  .cr {
    font-size: 11.5px;
    line-height: 1.5;
    color: var(--ink2);
    padding: 6px 9px;
    border-radius: 3px;
    background: var(--panel2);
    border: 1px solid var(--rule);
    margin: 4px 0;
  }
  .cr.hi {
    border-color: rgba(232, 96, 63, 0.35);
    background: rgba(232, 96, 63, 0.07);
    color: #e7c0b4;
  }
  .cr .rb {
    font-family: var(--mono);
    font-weight: 600;
    color: var(--ink);
  }
  .cr .ex {
    color: #f0a08c;
    font-weight: 600;
  }
  table.m {
    border-collapse: collapse;
    font-family: var(--mono);
    font-size: 11.5px;
    margin: 12px 0;
    width: 100%;
  }
  table.m :global(td),
  table.m :global(th) {
    border: 1px solid var(--rule);
    padding: 5px 9px;
    text-align: left;
  }
  table.m :global(th) {
    background: var(--panel2);
    color: var(--ink2);
    font-weight: 600;
  }
  table.m :global(td) {
    color: var(--ink2);
  }
  .dd {
    margin-top: 12px;
  }
  .dd > summary {
    font-family: var(--mono);
    font-size: 11.5px;
    color: var(--ink);
    cursor: pointer;
    user-select: none;
    letter-spacing: 0.04em;
  }
  .reflect {
    margin-top: 16px;
    border-top: 1px solid var(--rule);
    padding-top: 12px;
  }
  .rdir {
    font-size: 11.5px;
    color: var(--ink2);
    border: 1px dashed rgba(185, 139, 255, 0.35);
    background: rgba(185, 139, 255, 0.05);
    border-radius: 3px;
    padding: 6px 9px;
    margin: 4px 0;
    cursor: pointer;
    line-height: 1.45;
  }
  .rdir b {
    color: var(--critic);
    font-family: var(--mono);
  }
  .rtext {
    font-size: 13px;
    line-height: 1.7;
    margin-top: 12px;
    color: var(--ink);
  }
  .covrow {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    font-size: 12px;
    padding: 8px 11px;
    margin: 5px 0;
    border: 1px solid var(--rule2);
    border-radius: 3px;
    background: var(--panel2);
    cursor: pointer;
  }
  .covrow:hover {
    border-color: var(--mute);
  }
  .covrow code {
    color: var(--ink);
  }
  .own {
    margin-top: 13px;
    font-family: var(--disp);
    font-weight: 600;
    font-size: 13px;
    color: var(--ink);
  }
  .why {
    margin: 5px 0 2px 8px;
    color: var(--ink2);
    font-size: 11.5px;
  }
  .frameln {
    margin-left: 20px;
    font-size: 11.5px;
    display: flex;
    justify-content: space-between;
    max-width: 440px;
    padding: 1px 0;
    color: var(--ink2);
  }
</style>
