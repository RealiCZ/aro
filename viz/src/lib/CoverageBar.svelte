<script lang="ts">
  import { DATA } from './data';
  import type { Detail } from './types';

  let { setDetail }: { setDetail: (d: Detail) => void } = $props();

  const s = DATA.summary;
  const segs = (s.coverage ?? []).filter((seg) => seg.pct && seg.pct > 0);
</script>

<!-- caption: 运行时覆盖 · 块宽 ∝ self-time% · 该负载净 快 X% -->
<div class="cap">
  <b>运行时覆盖</b> · 块宽 ∝ self-time% · 该负载净
  <b style="color:#16a34a">快 {(-s.realized_pct).toFixed(1)}%</b>
</div>

<div class="covbar">
  {#each segs as seg (seg.key)}
    <div
      class="covseg"
      class:hatch={seg.hatch}
      style:flex-grow={seg.pct}
      style:background={seg.color}
      style:color={seg.key === 'floor' || seg.key === 'captured' ? '#fff' : '#334155'}
      style:cursor="pointer"
      title={seg.label + ' ' + seg.pct + '% — 点开看详情'}
      onclick={() => setDetail({ kind: 'cov', key: seg.key })}
      role="button"
      tabindex="0"
      onkeydown={(e) => e.key === 'Enter' && setDetail({ kind: 'cov', key: seg.key })}
    >
      {#if seg.pct >= 7}{seg.pct}% ▸{/if}
    </div>
  {/each}
</div>

<div class="cleg">
  {#each segs as seg (seg.key)}
    <span
      style:cursor="pointer"
      onclick={() => setDetail({ kind: 'cov', key: seg.key })}
      role="button"
      tabindex="0"
      onkeydown={(e) => e.key === 'Enter' && setDetail({ kind: 'cov', key: seg.key })}
    >
      <i class="dot" style:background={seg.color}></i>{seg.label} {seg.pct}% ▸
    </span>
  {/each}
</div>

<style>
  .cap {
    font-size: 12px;
    color: #334155;
    margin: 0 0 6px;
  }
  .covbar {
    display: flex;
    height: 32px;
    border-radius: 6px;
    overflow: hidden;
    border: 1px solid #e2e8f0;
  }
  .covseg {
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 10.5px;
    min-width: 2px;
    overflow: hidden;
    white-space: nowrap;
    cursor: default;
  }
  .covseg.hatch {
    background-image: repeating-linear-gradient(
      45deg,
      #cbd5e1 0 4px,
      transparent 4px 8px
    ) !important;
  }
  .cleg {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    font-size: 11px;
    color: #64748b;
    margin: 6px 0 16px;
  }
  .dot {
    display: inline-block;
    width: 9px;
    height: 9px;
    border-radius: 2px;
    margin-right: 4px;
    vertical-align: middle;
  }
</style>
