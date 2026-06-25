<script lang="ts">
  import { hierarchy, partition } from 'd3-hierarchy';
  import { DATA } from './data';
  import { col } from './colors';
  import { dpct } from './colors';
  import type { Detail, TreeNode, FnNode } from './types';

  let { detail, setDetail }: { detail: Detail; setDetail: (d: Detail) => void } =
    $props();

  const nodes = DATA.nodes;

  // ---- proportional heights via d3-hierarchy partition() ----
  // 2-level hierarchy [root -> fn nodes valued by pct]; partition gives each node a
  // [y0,y1] band whose size ∝ its value. We mirror tree.py's floor of 1.2 so tiny
  // frames stay visible/clickable.
  const VALUE = (n: TreeNode) => Math.max(n.pct ?? 1.2, 1.2);
  const root = hierarchy<{ children: TreeNode[] } | TreeNode>(
    { children: nodes },
    (d) => ('children' in d ? d.children : null),
  ).sum((d) => ('children' in d ? 0 : VALUE(d as TreeNode)));
  // partition over a unit square; we only use the vertical (x) extent as the height share.
  partition<{ children: TreeNode[] } | TreeNode>().size([100, 1])(root);
  const bands = new Map<string, number>();
  for (const leaf of root.children ?? []) {
    bands.set((leaf.data as TreeNode).id, (leaf.x1 ?? 0) - (leaf.x0 ?? 0));
  }

  // Which fn block is selected drives the right candidate column.
  let selectedNode = $state<TreeNode | null>(null);

  const stat = (n: TreeNode): string =>
    n.type === 'skipped' ? 'skipped' : (n.status ?? '');

  function clickFn(n: TreeNode) {
    selectedNode = n;
    if (n.type === 'skipped') setDetail({ kind: 'skip', node: n });
    else setDetail({ kind: 'fn', node: n, ci: 0 });
  }

  // When detail jumps to a fn via covList JUMP(), keep the candidate column in sync
  // and scroll the block into view.
  let colfEl: HTMLDivElement;
  $effect(() => {
    if (detail && detail.kind === 'fn') {
      const target = nodes.find((n) => n.type === 'fn' && n.i === detail.node.i);
      if (target && target !== selectedNode) {
        selectedNode = target;
        const blk = colfEl?.querySelector<HTMLElement>(
          `.fnblock[data-i="${detail.node.i}"]`,
        );
        blk?.scrollIntoView({ block: 'center' });
      }
    }
  });

  function clickCand(n: FnNode, ci: number) {
    selectedNode = n;
    setDetail({ kind: 'fn', node: n, ci });
  }

  const selectedFn = $derived(
    selectedNode && selectedNode.type === 'fn' ? selectedNode : null,
  );
  const selectedSkipped = $derived(
    selectedNode && selectedNode.type === 'skipped' ? selectedNode : null,
  );
  let reflectOpen = $state(false);
</script>

<div class="icicle">
  <!-- left root box -->
  <div class="col col-root">
    <div class="rootbox">
      <b>{DATA.spec}</b><br /><span class="muted">测试负载(整体 100%)</span>
    </div>
  </div>

  <!-- middle: function column, height ∝ self-time% -->
  <div class="col col-fns" bind:this={colfEl}>
    {#each nodes as n (n.id)}
      <div
        class="fnblock"
        class:sel={selectedNode === n}
        class:accepted={n.type === 'fn' && n.accepted}
        style:flex-grow={bands.get(n.id) ?? Math.max(n.pct ?? 1.2, 1.2)}
        style:border-left={'5px solid ' + col(stat(n))}
        data-i={n.type === 'fn' ? n.i : undefined}
        onclick={() => clickFn(n)}
        role="button"
        tabindex="0"
        onkeydown={(e) => e.key === 'Enter' && clickFn(n)}
      >
        <div class="fnname"><code>{n.fn}</code></div>
        <div class="fnmeta">
          {#if n.type === 'fn' && n.pct != null}{n.pct}% · {/if}<span
            style:color={col(stat(n))}
            style:font-weight="600">{stat(n)}{#if n.type === 'fn' && typeof n.delta === 'number'}{' ' + dpct(n.delta)}{/if}</span
          >{#if n.type === 'fn' && n.accepted}{' ✓'}{/if}{#if n.type === 'fn' && n.regime && n.regime !== 'byte-identical'}
            · <span style="color:#c2410c" title="动了结构,不建议直接合">需复核</span
            >{/if}
        </div>
      </div>
    {/each}
  </div>

  <!-- right: candidates of the selected fn -->
  <div class="col col-cands">
    {#if !selectedNode}
      <div class="muted hint-pad">← 点左边函数,看它试过的候选</div>
    {:else if selectedSkipped}
      <div class="muted hint-pad">跳过 — 无 fn 可定位(内联/宏/demangler 残留)</div>
    {:else if selectedFn}
      <div class="candhdr">
        {(selectedFn.candidates ?? []).length} 候选 · {(selectedFn.reflect ?? [])
          .length} 未试方向
      </div>
      {#each selectedFn.candidates ?? [] as c, ci (c.id)}
        <div
          class="candblock"
          class:sel={detail && detail.kind === 'fn' && detail.ci === ci}
          style:border-left={'4px solid ' + col(c.verdict)}
          onclick={() => clickCand(selectedFn, ci)}
          role="button"
          tabindex="0"
          onkeydown={(e) => e.key === 'Enter' && clickCand(selectedFn, ci)}
        >
          <code>{c.id}</code>
          <span style:color={col(c.verdict)} style="font-size:11px;font-weight:600"
            >{c.verdict}</span
          >
          <div class="muted candhyp">{(c.hypothesis ?? '').slice(0, 64)}…</div>
        </div>
      {/each}
      {#if selectedFn.reflect && selectedFn.reflect.length}
        <details class="reflect-grp" bind:open={reflectOpen}>
          <summary>⟳ {selectedFn.reflect.length} 条 reflect 未试方向</summary>
          {#each selectedFn.reflect as r (r.id)}
            <div
              class="reflect-item"
              onclick={() => setDetail({ kind: 'reflect', dir: r })}
              role="button"
              tabindex="0"
              onkeydown={(e) =>
                e.key === 'Enter' && setDetail({ kind: 'reflect', dir: r })}
            >
              <b>[{r.id}] 未试</b> {(r.text ?? '').slice(0, 72)}…
            </div>
          {/each}
        </details>
      {/if}
    {/if}
  </div>
</div>

<style>
  .muted {
    color: #94a3b8;
  }
  .icicle {
    display: flex;
    gap: 14px;
    align-items: stretch;
    min-height: 440px;
    height: calc(100% - 130px);
  }
  .col {
    display: flex;
    flex-direction: column;
  }
  .col-root {
    justify-content: center;
    flex: 0 0 100px;
  }
  .rootbox {
    padding: 10px;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    background: #fff;
    font-size: 12px;
    text-align: center;
  }
  .col-fns {
    flex: 0 0 220px;
    gap: 4px;
  }
  .col-cands {
    flex: 1;
    overflow: auto;
    border-left: 2px dashed #e2e8f0;
    padding-left: 14px;
  }
  .hint-pad {
    font-size: 12px;
    padding: 8px;
  }
  .candhdr {
    font-size: 11px;
    color: #64748b;
    margin: 2px 0 6px;
  }
  .fnblock {
    border: 1px solid #e2e8f0;
    border-radius: 7px;
    background: #fff;
    padding: 5px 9px;
    cursor: pointer;
    display: flex;
    flex-direction: column;
    justify-content: center;
    min-height: 30px;
    overflow: hidden;
    transition: 0.1s;
  }
  .fnblock:hover {
    border-color: #94a3b8;
  }
  .fnblock.sel {
    outline: 2px solid #2563eb;
    outline-offset: -1px;
  }
  .fnblock.accepted {
    background: #f0fdf4;
  }
  .fnname {
    font-size: 12.5px;
    font-weight: 600;
  }
  .fnmeta {
    font-size: 10.5px;
    color: #64748b;
    margin-top: 1px;
  }
  .candblock {
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    background: #f8fafc;
    padding: 6px 9px;
    margin: 3px 0;
    cursor: pointer;
  }
  .candblock:hover {
    border-color: #94a3b8;
  }
  .candblock.sel {
    outline: 2px solid #2563eb;
    outline-offset: -1px;
  }
  .candhyp {
    font-size: 11px;
    margin-top: 2px;
  }
  .reflect-grp {
    margin-top: 8px;
  }
  .reflect-grp > summary {
    font-size: 12px;
    color: #7c3aed;
    cursor: pointer;
    user-select: none;
  }
  .reflect-item {
    font-size: 11px;
    border: 1px dashed #c4b5fd;
    background: #faf5ff;
    border-radius: 5px;
    padding: 5px 8px;
    margin: 3px 0;
    cursor: pointer;
  }
</style>
