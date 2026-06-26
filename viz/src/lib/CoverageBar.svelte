<script lang="ts">
  import { DATA } from './data';
  import type { Detail } from './types';

  let { setDetail }: { setDetail: (d: Detail) => void } = $props();

  const s = DATA.summary;
  const segs = (s.coverage ?? []).filter((seg) => seg.pct && seg.pct > 0);

  // the front-end owns the visual palette (heat-keyed), not Python's emitted seg.color.
  const FILL: Record<string, string> = {
    captured: 'linear-gradient(180deg,#5fdca2,#3aa97a)',
    tried: '#3a4753',
    headroom: '#2b3742',
    unreachable: 'repeating-linear-gradient(45deg,#243039 0 6px,#1b242d 6px 12px)',
    floor: 'linear-gradient(180deg,#3a4854,#252f3a)',
    other: '#1b242d',
  };
  const dark = (k: string) => k !== 'captured';
  const LEG: Record<string, string> = {
    captured: '#54d6a0',
    tried: '#3a4753',
    headroom: '#2b3742',
    unreachable: '#243039',
    floor: '#3a4854',
    other: '#1b242d',
  };
</script>

<div class="covbar">
  {#each segs as seg (seg.key)}
    <div
      class="covseg"
      style:flex-grow={seg.pct}
      style:background={FILL[seg.key] ?? seg.color}
      style:color={dark(seg.key) ? '#aebac6' : '#08120d'}
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
    <button onclick={() => setDetail({ kind: 'cov', key: seg.key })}>
      <i style:background={LEG[seg.key] ?? seg.color}></i>{seg.label} {seg.pct}% ▸
    </button>
  {/each}
</div>

<style>
  .covbar {
    display: flex;
    height: 36px;
    border-radius: 3px;
    overflow: hidden;
    border: 1px solid var(--rule2);
  }
  .covseg {
    display: flex;
    align-items: center;
    padding: 0 10px;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 500;
    min-width: 2px;
    overflow: hidden;
    white-space: nowrap;
    cursor: pointer;
    transition: filter 0.12s ease;
  }
  .covseg:hover {
    filter: brightness(1.15);
  }
  .cleg {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 16px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink2);
    margin-top: 11px;
  }
  .cleg button {
    background: none;
    border: none;
    color: var(--ink2);
    cursor: pointer;
    padding: 2px 0;
    font: inherit;
  }
  .cleg button:hover {
    color: var(--ink);
  }
  .cleg i {
    display: inline-block;
    width: 9px;
    height: 9px;
    border-radius: 2px;
    margin-right: 6px;
    vertical-align: middle;
  }
</style>
