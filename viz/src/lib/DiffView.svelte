<script lang="ts">
  // Compact unified diff, dark dossier theme.
  let { diff }: { diff: string } = $props();

  type Line = { text: string; color: string; bg: string };
  const lines = $derived(
    (diff || '').split('\n').map((l): Line => {
      const ch = l.charAt(0);
      let color = '#566472';
      let bg = 'transparent';
      if (l.indexOf('# ') === 0) {
        color = '#1B2530';
        bg = '#eef2f7';
      } else if (ch === '@') {
        color = '#7A45D4';
        bg = 'rgba(122,69,212,.05)';
      } else if (ch === '+') {
        color = '#15734a';
        bg = 'rgba(21,148,95,.1)';
      } else if (ch === '-') {
        color = '#b23c22';
        bg = 'rgba(212,73,44,.09)';
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
