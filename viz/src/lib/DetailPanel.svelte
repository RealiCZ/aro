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
  <div class="kv">
    <div class="k">本函数贡献</div>
    <div>
      {#if n.accepted && typeof n.delta === 'number'}<b style="color:#16a34a"
          >快 {(-n.delta).toFixed(2)}%</b
        >{:else}<span class="muted">0%({n.status ?? '—'},未落地)</span>{/if}
    </div>
    {#if n.decision}
      <div class="k">探索器判定</div>
      <div>
        <b style:color={n.decision === 'STOP' ? '#dc2626' : '#16a34a'}
          >{n.decision}</b
        > — {n.reason ?? ''}
      </div>
    {/if}
    {#if n.files && n.files.length}
      <div class="k">编辑范围</div>
      <div>{#each n.files as f}<code>{f}</code><br />{/each}</div>
    {/if}
  </div>
  {#if c}
    <h3 class="ch">
      候选 <code
        >{#if (n.attempts?.length ?? 1) > 1}<span class="att">#{c._attempt}</span>
        {/if}{c.id}</code
      > ·
      <span style:color={col(c.verdict)}>{c.verdict}</span>
    </h3>
    <div class="hyp"><b>改了什么:</b> {c.hypothesis}</div>
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
  .kv {
    display: grid;
    grid-template-columns: 120px 1fr;
    gap: 4px 10px;
    font-size: 12.5px;
    margin: 10px 0;
  }
  .kv .k {
    color: #64748b;
  }
  .ch {
    margin-top: 18px;
    padding-top: 14px;
    border-top: 1px solid #eef2f8;
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
  }
</style>
