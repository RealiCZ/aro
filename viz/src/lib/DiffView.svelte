<script lang="ts">
  // Compact unified diff, dark dossier theme.
  let { diff }: { diff: string } = $props();

  type Line = { text: string; color: string; bg: string };
  const lines = $derived(
    (diff || '').split('\n').map((l): Line => {
      const ch = l.charAt(0);
      let color = '#8A99A8';
      let bg = 'transparent';
      if (l.indexOf('# ') === 0) {
        color = '#CCD6E0';
        bg = '#10171E';
      } else if (ch === '@') {
        color = '#B98BFF';
        bg = 'rgba(185,139,255,.06)';
      } else if (ch === '+') {
        color = '#7fe3b6';
        bg = 'rgba(84,214,160,.09)';
      } else if (ch === '-') {
        color = '#f0a08c';
        bg = 'rgba(232,96,63,.09)';
      }
      return { text: l, color, bg };
    }),
  );
</script>

<div class="diffbox">
  {#each lines as ln}
    <div style:color={ln.color} style:background={ln.bg} style="padding:1px 12px">
      {#if ln.text}{ln.text}{:else}{' '}{/if}
    </div>
  {/each}
</div>

<style>
  .diffbox {
    font-family: var(--mono);
    font-size: 11.5px;
    line-height: 1.6;
    border: 1px solid var(--rule2);
    border-radius: 3px;
    overflow: auto;
    max-height: 50vh;
    white-space: pre;
    margin-top: 8px;
    padding: 4px 0;
    background: var(--panel2);
  }
</style>
