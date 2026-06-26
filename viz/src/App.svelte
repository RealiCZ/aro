<script lang="ts">
  // latin subset only — the page's Chinese falls back to the system CJK font, so we
  // don't embed those glyphs (keeps the self-contained file lean).
  import '@fontsource/space-grotesk/latin-500.css';
  import '@fontsource/space-grotesk/latin-600.css';
  import '@fontsource/space-grotesk/latin-700.css';
  import '@fontsource/ibm-plex-mono/latin-400.css';
  import '@fontsource/ibm-plex-mono/latin-600.css';
  import { DATA } from './lib/data';
  import type { Detail } from './lib/types';
  import CoverageBar from './lib/CoverageBar.svelte';
  import Icicle from './lib/Icicle.svelte';
  import Candidates from './lib/Candidates.svelte';
  import DetailPanel from './lib/DetailPanel.svelte';

  const s = DATA.summary;
  let detail = $state<Detail>(null);
  const setDetail = (d: Detail) => {
    detail = d;
  };

  const realized = -s.realized_pct; // % faster, positive
  const ceil = s.ceiling_pct ?? Math.max(0, 100 - s.floor_pct);
  const gaugePct = Math.min(100, ceil ? (realized / ceil) * 100 : 0);
  const tok = (n: number): string =>
    n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'k' : '' + n;
</script>

<header>
  <div class="wrap">
    <div class="mast">
      <span class="brand">AR<b>O</b></span>
      <span class="kicker">run dossier · 自动性能优化</span>
      <span class="spec mono"
        >{DATA.spec} · <b>{s.attempted}</b> tried · <b>{s.accepted}</b> won · {s.skipped} skipped
        · <b class="dec" class:stop={s.decision === 'STOP'}>{s.decision}</b></span
      >
    </div>
    <div class="tele">
      <div class="hero">
        <div class="big">快 <i>{realized.toFixed(2)}%</i></div>
        <div class="biglab">realized · compounded over {s.accepted} accepts</div>
      </div>
      <div class="gauge">
        <div class="gtrack">
          <i style:width={gaugePct + '%'} class="fill"></i><i
            style:width={100 - gaugePct + '%'}
            class="rest"
          ></i>
        </div>
        <div class="greadout">
          <span>0%</span><span>已达理论上界的 <b>{gaugePct.toFixed(0)}%</b></span><span
            >ceiling ~{ceil.toFixed(0)}% · floor 碰不得 {s.floor_pct.toFixed(0)}%</span
          >
        </div>
      </div>
      {#if s.tokens}<div class="stat">
          <div class="v">{tok(s.tokens)}</div>
          <div class="l">output tok</div>
        </div>{/if}
      {#if s.cost_usd}<div class="stat">
          <div class="v">${s.cost_usd.toFixed(2)}</div>
          <div class="l">cost</div>
        </div>{/if}
      <div class="stat">
        <div class="v">{s.critic_rejects ?? 0}·{s.apply_fails ?? 0}</div>
        <div class="l">critic毙·sibling</div>
      </div>
    </div>
  </div>
</header>

<div class="wrap">
  <section>
    <div class="lab">where the time goes <s>运行时自时间分解</s></div>
    <CoverageBar {setDetail} />
  </section>

  <section>
    <div class="lab">hot-path flame · 热路径 <s>条长=自时间 · 填色=热度 · 字形=判定</s></div>
    <div class="grid">
      <div class="flame"><Icicle {detail} {setDetail} /></div>
      <div class="cands"><Candidates {detail} {setDetail} /></div>
      <div class="doss"><DetailPanel {detail} {setDetail} /></div>
    </div>
  </section>

  {#if DATA.perf_svg}
    <section class="last">
      <div class="lab">trajectory · 进化轨迹 <s>加速 vs 累计 token · phosphor trace</s></div>
      <div class="perf-svg">{@html DATA.perf_svg}</div>
    </section>
  {/if}

  <div class="foot">
    <span>truth source · events.jsonl</span>
    <span>ARO · the loop is commodity · the deterministic judge is the moat</span>
  </div>
</div>

<style>
  :global(:root) {
    --ground: #eaeef3;
    --panel: #ffffff;
    --panel2: #f3f6fa;
    --rule: #e2e8f0;
    --rule2: #d5dee8;
    --ink: #1b2530;
    --ink2: #566472;
    --mute: #8693a1;
    --signal: #0e9f8c;
    --accept: #15945f;
    --merge: #0e9e72;
    --regress: #d4492c;
    --noise: #7e8c9a;
    --critic: #7a45d4;
    --mono: 'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
    --disp: 'Space Grotesk', system-ui, sans-serif;
  }
  :global(*) {
    box-sizing: border-box;
  }
  :global(body) {
    margin: 0;
    background: var(--ground);
    color: var(--ink);
    font-family: 'IBM Plex Sans', system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
    letter-spacing: 0.1px;
  }
  :global(code),
  .mono {
    font-family: var(--mono);
    font-feature-settings: 'tnum' 1;
  }
  .wrap {
    max-width: 1180px;
    margin: 0 auto;
    padding: 0 22px;
  }

  /* masthead */
  header {
    border-bottom: 1px solid var(--rule2);
    background: radial-gradient(130% 160% at 0% 0%, #f5f8fc 0%, var(--ground) 62%);
  }
  .mast {
    display: flex;
    align-items: baseline;
    gap: 14px;
    padding: 18px 0 4px;
    flex-wrap: wrap;
  }
  .brand {
    font-family: var(--disp);
    font-weight: 700;
    font-size: 15px;
    letter-spacing: 0.34em;
  }
  .brand :global(b) {
    color: var(--signal);
  }
  .kicker {
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.2em;
    color: var(--mute);
    text-transform: uppercase;
  }
  .spec {
    margin-left: auto;
    font-size: 12px;
    color: var(--ink2);
  }
  .spec :global(b) {
    color: var(--ink);
  }
  .dec {
    color: var(--signal);
  }
  .dec.stop {
    color: var(--regress);
  }
  .tele {
    display: flex;
    align-items: center;
    gap: 26px;
    padding: 8px 0 20px;
    flex-wrap: wrap;
  }
  .big {
    font-family: var(--disp);
    font-weight: 600;
    font-size: 44px;
    line-height: 0.9;
    letter-spacing: -0.02em;
  }
  .big i {
    font-style: normal;
    color: var(--signal);
  }
  .biglab {
    font-family: var(--mono);
    font-size: 10.5px;
    letter-spacing: 0.18em;
    color: var(--mute);
    text-transform: uppercase;
    margin-top: 7px;
  }
  .gauge {
    flex: 1;
    min-width: 230px;
  }
  .gtrack {
    height: 10px;
    border: 1px solid var(--rule2);
    border-radius: 2px;
    background: var(--panel2);
    display: flex;
    overflow: hidden;
  }
  .gtrack .fill {
    background: linear-gradient(90deg, #15945f, var(--signal));
  }
  .gtrack .rest {
    background: repeating-linear-gradient(45deg, #dbe3ec 0 5px, #e8edf3 5px 10px);
  }
  .greadout {
    display: flex;
    justify-content: space-between;
    margin-top: 7px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink2);
  }
  .greadout b {
    color: var(--ink);
  }
  .stat {
    text-align: right;
  }
  .stat .v {
    font-family: var(--mono);
    font-size: 18px;
    color: var(--ink);
  }
  .stat .l {
    font-family: var(--mono);
    font-size: 9.5px;
    letter-spacing: 0.12em;
    color: var(--mute);
    text-transform: uppercase;
  }

  section {
    padding: 22px 0;
    border-bottom: 1px solid var(--rule);
  }
  section.last {
    border-bottom: none;
  }
  .lab {
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.22em;
    color: var(--ink2);
    text-transform: uppercase;
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 14px;
  }
  .lab :global(s) {
    color: var(--mute);
    text-decoration: none;
    letter-spacing: 0.08em;
  }
  .lab::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--rule);
  }

  .grid {
    display: grid;
    grid-template-columns: 0.92fr 0.78fr 1.05fr;
    border: 1px solid var(--rule2);
    border-radius: 3px;
    overflow: hidden;
    background: var(--panel);
  }
  .flame {
    background: linear-gradient(180deg, #f6f8fb, #eef2f7);
    max-height: 72vh;
    overflow: auto;
  }
  .cands {
    border-left: 1px solid var(--rule);
    background: var(--panel2);
    max-height: 72vh;
    overflow: auto;
  }
  .doss {
    border-left: 1px solid var(--rule2);
    background: var(--panel);
    max-height: 72vh;
    overflow: auto;
  }
  .perf-svg {
    border: 1px solid var(--rule2);
    border-radius: 3px;
    background: var(--panel);
    padding: 6px;
  }
  .perf-svg :global(svg) {
    display: block;
    width: 100%;
    height: auto;
  }
  .foot {
    display: flex;
    justify-content: space-between;
    padding: 18px 0 30px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--mute);
  }
  @media (max-width: 900px) {
    .grid {
      grid-template-columns: 1fr;
    }
    .cands,
    .doss {
      border-left: none;
      border-top: 1px solid var(--rule2);
    }
  }
</style>
