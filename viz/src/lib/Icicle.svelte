<script lang="ts">
  import { hierarchy, partition } from 'd3-hierarchy';
  import { NODES } from './data';
  import { col, heatGrad, dpct, T } from './colors';
  import type { Detail, TreeNode } from './types';

  let { detail, setDetail }: { detail: Detail; setDetail: (d: Detail) => void } = $props();

  const nodes = NODES;
  const maxPct = Math.max(1.2, ...nodes.map((n) => (n.type === 'fn' ? (n.pct ?? 0) : 0)));

  // proportional band heights — self-time as vertical space (a real flamegraph).
  const VALUE = (n: TreeNode) => Math.max(n.pct ?? 1.2, 1.2);
  const root = hierarchy<{ children: TreeNode[] } | TreeNode>(
    { children: nodes },
    (d) => ('children' in d ? d.children : null),
  ).sum((d) => ('children' in d ? 0 : VALUE(d as TreeNode)));
  partition<{ children: TreeNode[] } | TreeNode>().size([100, 1])(root);
  const bands = new Map<string, number>();
  for (const leaf of root.children ?? [])
    bands.set((leaf.data as TreeNode).id, (leaf.x1 ?? 0) - (leaf.x0 ?? 0));

  const selectedNode = $derived(
    detail && (detail.kind === 'fn' || detail.kind === 'skip' || detail.kind === 'reflect')
      ? detail.node
      : null,
  );
  const isMerge = (n: TreeNode): boolean =>
    n.type === 'fn' && !!n.accepted && (!n.regime || n.regime === 'byte-identical');

  function glyph(n: TreeNode): string {
    if (n.type === 'skipped') return 'skip · 无 fn';
    if (n.accepted) return '✓ ' + dpct(n.delta) + (isMerge(n) ? ' 可合' : '');
    const st = n.status ?? '';
    return st === 'within-noise'
      ? '~ noise'
      : st === 'noise-limited'
        ? '~ noise-limited'
        : st === 'regressed'
          ? '▲ ' + dpct(n.delta)
          : st || 'running';
  }
  function clickFn(n: TreeNode) {
    if (n.type === 'skipped') setDetail({ kind: 'skip', node: n });
    else setDetail({ kind: 'fn', node: n, ci: 0 });
  }
</script>

<div class="flamebox">
  {#each nodes as n (n.id)}
    {@const cold = n.type === 'skipped'}
    {@const vc = cold ? T.mute : n.accepted ? (isMerge(n) ? T.merge : T.accept) : col(n.status)}
    <div
      class="frame"
      class:sel={selectedNode === n}
      style:flex-grow={bands.get(n.id) ?? Math.max(n.pct ?? 1.2, 1.2)}
      onclick={() => clickFn(n)}
      role="button"
      tabindex="0"
      onkeydown={(e) => e.key === 'Enter' && clickFn(n)}
    >
      {#if n.type === 'fn' && n.accepted}<span
          class="spine"
          style:background={isMerge(n) ? T.merge : T.accept}
        ></span>{/if}
      <div
        class="bar"
        class:cold
        style:background={cold ? 'linear-gradient(95deg,#3a4956,#2c3742)' : heatGrad(n.pct, maxPct)}
      >
        <code>{n.fn}</code>
        <span class="vd" style:color={vc}>{glyph(n)}</span>
        {#if n.type === 'fn' && n.pct != null}<span class="pct">{n.pct}%</span>{/if}
      </div>
    </div>
  {/each}
</div>

<style>
  .flamebox {
    display: flex;
    flex-direction: column;
    gap: 5px;
    padding: 13px 14px 13px 18px;
    min-height: 100%;
  }
  .frame {
    position: relative;
    display: flex;
    cursor: pointer;
    min-height: 30px;
  }
  .bar {
    flex: 1;
    border-radius: 2px;
    display: flex;
    align-items: center;
    padding: 0 10px;
    gap: 9px;
    border: 1px solid rgba(255, 255, 255, 0.06);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.09);
    transition: filter 0.12s ease;
    overflow: hidden;
  }
  .frame:hover .bar {
    filter: brightness(1.13);
  }
  .frame.sel .bar {
    outline: 1.5px solid var(--signal);
    outline-offset: 1px;
  }
  .bar code {
    font-size: 12.5px;
    color: #0b0f14;
    font-weight: 600;
  }
  .bar.cold code {
    color: var(--ink2);
    font-weight: 500;
  }
  .vd {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 600;
    padding: 1px 7px;
    border-radius: 2px;
    background: rgba(10, 14, 18, 0.78);
    white-space: nowrap;
  }
  .bar.cold .vd {
    background: rgba(0, 0, 0, 0.22);
  }
  .pct {
    font-family: var(--mono);
    font-size: 11px;
    color: rgba(10, 14, 18, 0.62);
    margin-left: auto;
  }
  .bar.cold .pct {
    color: var(--mute);
  }
  .spine {
    position: absolute;
    left: -14px;
    top: 50%;
    width: 12px;
    height: 1.5px;
  }
  .spine::before {
    content: '';
    position: absolute;
    left: -3px;
    top: -2.5px;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: inherit;
  }
</style>
