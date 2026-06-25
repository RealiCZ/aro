<script lang="ts">
  // Port of diffHtml: colored compact unified diff.
  //  # <file>  -> dark text, light-blue bg (header)
  //  @@ hunk   -> purple
  //  + added   -> green bg
  //  - removed -> red bg
  let { diff }: { diff: string } = $props();

  type Line = { text: string; color: string; bg: string };
  const lines = $derived(
    (diff || '').split('\n').map((l): Line => {
      const ch = l.charAt(0);
      let color = '#475569';
      let bg = 'transparent';
      if (l.indexOf('# ') === 0) {
        color = '#0f172a';
        bg = '#eef2f7';
      } else if (ch === '@') {
        color = '#7c3aed';
        bg = '#faf5ff';
      } else if (ch === '+') {
        color = '#166534';
        bg = '#dcfce7';
      } else if (ch === '-') {
        color = '#991b1b';
        bg = '#fee2e2';
      }
      return { text: l, color, bg };
    }),
  );
</script>

<div class="diffbox">
  {#each lines as ln}
    <div style:color={ln.color} style:background={ln.bg} style="padding:1px 12px">
      {#if ln.text}{ln.text}{:else}{' '}{/if}
    </div>
  {/each}
</div>

<style>
  .diffbox {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 11.5px;
    line-height: 1.6;
    border: 1px solid #e8edf4;
    border-radius: 10px;
    overflow: auto;
    max-height: 62vh;
    white-space: pre;
    margin-top: 8px;
    padding: 4px 0;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
  }
</style>
