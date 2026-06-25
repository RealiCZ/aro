<script lang="ts">
  import { DATA } from './lib/data';
  import type { Detail } from './lib/types';
  import CoverageBar from './lib/CoverageBar.svelte';
  import Icicle from './lib/Icicle.svelte';
  import DetailPanel from './lib/DetailPanel.svelte';

  const s = DATA.summary;

  // The "current detail" the right pane renders from.
  let detail = $state<Detail>(null);
  const setDetail = (d: Detail) => {
    detail = d;
  };

  // Header chips, mirroring build()'s chip list in tree.py.
  const chips: [string, string][] = [
    ['尝试', String(s.attempted)],
    ['优化成功', String(s.accepted)],
    ['跳过', String(s.skipped)],
    ['进化了', '快 ' + (-s.realized_pct).toFixed(1) + '%'],
    ['能进化的', s.headroom_pct.toFixed(1) + '%'],
    ['判定', s.decision],
  ];
</script>

<header>
  <h1 id="title">{DATA.spec} — 搜索图(覆盖 + icicle)</h1>
  <div class="chips" id="chips">
    {#each chips as [k, v], idx (k)}
      <span
        class="chip"
        style:background={idx === chips.length - 1 && s.decision
          ? s.decision === 'STOP'
            ? '#fee2e2'
            : '#dcfce7'
          : undefined}
      >
        {k} <b>{v}</b>
      </span>
    {/each}
  </div>
  <div class="legend">
    <span><i class="dot" style="background:#16a34a"></i>accepted</span>
    <span><i class="dot" style="background:#64748b"></i>within-noise</span>
    <span><i class="dot" style="background:#ca8a04"></i>noise-limited</span>
    <span><i class="dot" style="background:#dc2626"></i>regressed/verify/rejected</span>
    <span><i class="dot" style="background:#ea580c"></i>skipped(无 fn)</span>
    <span><i class="dot" style="background:#7c3aed"></i>reflect 提出·未试</span>
  </div>
</header>

{#if DATA.perf_svg}
  <details class="perf" open>
    <summary
      >进化轨迹 · 加速% vs 累计 token
      <span class="muted"
        >(阶梯 = running best,点 = 候选含回归,× = off-spec,虚线 = 碰不得 floor 的理论上界)</span
      ></summary
    >
    <div class="perf-svg">{@html DATA.perf_svg}</div>
  </details>
{/if}

<main>
  <div id="tree">
    <CoverageBar {setDetail} />
    <Icicle {detail} {setDetail} />
  </div>
  <div id="detail">
    <DetailPanel {detail} {setDetail} />
  </div>
</main>

<style>
  :global(*) {
    box-sizing: border-box;
  }
  :global(body) {
    margin: 0;
    font-family:
      -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, Helvetica, Arial,
      sans-serif;
    color: #0f172a;
    background: #f5f7fb;
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
  }
  .perf {
    flex: 0 0 auto;
    max-height: 52vh;
    overflow: auto;
    background: #fff;
    border-bottom: 1px solid #e8edf4;
    padding: 6px 24px 12px;
  }
  .perf > summary {
    cursor: pointer;
    user-select: none;
    font-size: 13px;
    font-weight: 600;
    color: #334155;
    padding: 6px 0;
  }
  .perf .muted {
    color: #94a3b8;
    font-weight: 400;
  }
  .perf-svg {
    text-align: center;
  }
  .perf-svg :global(svg) {
    display: block;
    margin: 2px auto 0;
    width: 100%;
    max-width: 980px;
    height: auto;
  }
  :global(code) {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  header {
    padding: 16px 24px 14px;
    background: linear-gradient(180deg, #ffffff, #fbfcfe);
    border-bottom: 1px solid #e8edf4;
    box-shadow: 0 1px 3px rgba(15, 23, 42, 0.03);
  }
  h1 {
    margin: 0 0 9px;
    font-size: 18px;
    font-weight: 700;
    letter-spacing: -0.01em;
  }
  .chips {
    display: flex;
    gap: 7px;
    flex-wrap: wrap;
    font-size: 12px;
  }
  .chip {
    padding: 4px 11px;
    border-radius: 999px;
    background: #f1f5fb;
    border: 1px solid #e7edf6;
    color: #475569;
  }
  .chip :global(b) {
    color: #0f172a;
    font-weight: 650;
  }
  .legend {
    display: flex;
    gap: 13px;
    flex-wrap: wrap;
    font-size: 11px;
    color: #64748b;
    margin-top: 10px;
  }
  .dot {
    display: inline-block;
    width: 9px;
    height: 9px;
    border-radius: 3px;
    margin-right: 5px;
    vertical-align: middle;
  }
  main {
    display: flex;
    gap: 0;
    flex: 1;
    min-height: 0;
  }
  #tree {
    flex: 1.1;
    overflow: auto;
    padding: 20px 24px;
    border-right: 1px solid #e8edf4;
  }
  #detail {
    flex: 1;
    overflow: auto;
    padding: 22px 26px;
    background: #fff;
  }
</style>
