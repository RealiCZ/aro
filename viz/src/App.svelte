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
    font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif;
    color: #0f172a;
    background: #f8fafc;
  }
  header {
    padding: 14px 20px;
    background: #fff;
    border-bottom: 1px solid #e2e8f0;
  }
  h1 {
    margin: 0 0 6px;
    font-size: 17px;
  }
  .chips {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    font-size: 12px;
  }
  .chip {
    padding: 3px 9px;
    border-radius: 12px;
    background: #f1f5f9;
    color: #334155;
  }
  .chip :global(b) {
    color: #0f172a;
  }
  .legend {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    font-size: 11px;
    color: #64748b;
    margin-top: 6px;
  }
  .dot {
    display: inline-block;
    width: 9px;
    height: 9px;
    border-radius: 2px;
    margin-right: 4px;
    vertical-align: middle;
  }
  main {
    display: flex;
    gap: 0;
    height: calc(100vh - 92px);
  }
  #tree {
    flex: 1.1;
    overflow: auto;
    padding: 16px 20px;
    border-right: 1px solid #e2e8f0;
  }
  #detail {
    flex: 1;
    overflow: auto;
    padding: 18px 22px;
    background: #fff;
  }
</style>
