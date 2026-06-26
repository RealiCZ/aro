<script lang="ts">
  import { col, T } from './colors';
  import type { Detail, FnNode } from './types';

  let { detail, setDetail }: { detail: Detail; setDetail: (d: Detail) => void } = $props();

  const cvColor = (v: string): string =>
    v === 'reject' ? T.regress : v === 'pass-risk' ? '#C2841E' : T.accept;
  function pick(n: FnNode, ci: number) {
    setDetail({ kind: 'fn', node: n, ci });
  }
</script>

{#if detail && detail.kind === 'fn'}
  {@const n = detail.node}
  {@const ci = detail.ci < (n.candidates?.length ?? 0) ? detail.ci : 0}
  <div class="col">
    <div class="lab2 mono">候选 · {(n.candidates ?? []).length} 个</div>
    <div class="list">
      {#each n.candidates ?? [] as c, i ((c._attempt ?? 0) + ':' + c.id)}
        <button
          class="crow"
          class:on={i === ci}
          style:border-left={'3px solid ' + col(c.verdict)}
          onclick={() => pick(n, i)}
        >
          <div class="cid mono">
            <code>{#if (n.attempts?.length ?? 1) > 1}#{c._attempt} {/if}{c.id}</code>
          </div>
          <div class="cmeta mono">
            <span style:color={col(c.verdict)}>{c.verdict}</span>
            {#if c.critic}<span class="sep">·</span><span style:color={cvColor(c.critic.verdict)}
                >评审 {c.critic.verdict}</span
              >{/if}
          </div>
          <div class="chyp">{(c.hypothesis ?? '').slice(0, 72)}…</div>
        </button>
      {/each}
    </div>

    {#if n.reflect && n.reflect.length}
      <div class="lab2 mono" style="color:var(--critic)">未试方向 · {n.reflect.length}</div>
      <div class="list">
        {#each n.reflect as r ((r._attempt ?? 0) + ':' + r.id)}
          <button class="rrow" onclick={() => setDetail({ kind: 'reflect', dir: r, node: n })}>
            <code>[{r.id}]</code> {(r.text ?? '').slice(0, 74)}…
          </button>
        {/each}
      </div>
    {/if}
  </div>
{:else if detail && detail.kind === 'cov'}
  <div class="hint2">覆盖分解 →<br /><span class="mute">右栏列出该段的函数,点函数回到火焰图</span></div>
{:else}
  <div class="hint2">点左边火焰图的帧<br /><span class="mute">这一列列出它试过的候选 + 未试方向</span></div>
{/if}

<style>
  .col {
    padding: 13px 13px;
  }
  .mono {
    font-family: var(--mono);
  }
  .mute {
    color: var(--mute);
  }
  .lab2 {
    font-size: 10.5px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--mute);
    margin: 2px 0 8px;
  }
  .lab2:not(:first-child) {
    margin-top: 16px;
  }
  .list {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .crow,
  .rrow {
    text-align: left;
    background: var(--panel);
    border: 1px solid var(--rule);
    border-radius: 3px;
    padding: 7px 9px;
    cursor: pointer;
    font: inherit;
    width: 100%;
  }
  .crow {
    border-left: 3px solid var(--noise);
  }
  .crow:hover,
  .rrow:hover {
    border-color: var(--mute);
  }
  .crow.on {
    background: #ecf7f3;
    border-color: var(--signal);
    box-shadow: inset 0 0 0 1px rgba(14, 159, 140, 0.25);
  }
  .cid code {
    font-size: 11.5px;
    color: var(--ink);
  }
  .cmeta {
    font-size: 11px;
    margin-top: 2px;
  }
  .sep {
    color: var(--mute);
    margin: 0 4px;
  }
  .chyp {
    font-size: 11px;
    color: var(--ink2);
    margin-top: 4px;
    line-height: 1.4;
  }
  .rrow {
    font-size: 11px;
    color: var(--ink2);
    border: 1px dashed rgba(122, 69, 212, 0.32);
    background: rgba(122, 69, 212, 0.04);
    line-height: 1.4;
  }
  .rrow code {
    color: var(--critic);
    font-family: var(--mono);
  }
  .hint2 {
    color: var(--mute);
    font-size: 12px;
    text-align: center;
    margin-top: 40px;
    line-height: 1.9;
    padding: 0 14px;
  }
</style>
