<script lang="ts">
  import { DATA } from './data';
  import type { Detail } from './types';

  let { setDetail }: { setDetail: (d: Detail) => void } = $props();

  const s = DATA.summary;
  const segs = (s.coverage ?? []).filter((seg) => seg.pct && seg.pct > 0);

  // the front-end owns the visual palette (heat-keyed), not Python's emitted seg.color.
  const FILL: Record<string, string> = {
    captured: 'linear-gradient(180deg,#36b47c,#2a9a69)',
    tried: '#b9c6d3',
    headroom: '#cfd9e3',
    unreachable: 'repeating-linear-gradient(45deg,#c4cfda 0 6px,#d6dfe8 6px 12px)',
    floor: 'linear-gradient(180deg,#90a2b2,#7d93a4)',
    other: '#dde4ec',
  };
  // these (darker) bands take light text; the rest take dark text
  const lightTxt = (k: string) => k === 'captured' || k === 'floor';
  const LEG: Record<string, string> = {
    captured: '#2a9a69',
    tried: '#b9c6d3',
    headroom: '#cfd9e3',
    unreachable: '#c4cfda',
    floor: '#7d93a4',
    other: '#dde4ec',
  };
</script>

<div class="covbar">
  {#each segs as seg (seg.key)}
    <div
      class="covseg"
      style:flex-grow={seg.pct}
      style:background={FILL[seg.key] ?? seg.color}
      style:color={lightTxt(seg.key) ? '#f4faf7' : '#2a3845'}
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
