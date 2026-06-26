// ARO dossier palette — a profiler/telemetry console. Color encodes DATA, not decoration:
// a heat ramp for hotness, a phosphor signal for the live trace, semantic verdict hues.

export const T = {
  ground: '#0E1419',
  panel: '#141C24',
  panel2: '#10171E',
  rule: '#222C36',
  rule2: '#2C3742',
  ink: '#CCD6E0',
  ink2: '#8A99A8',
  mute: '#5E6E7C',
  signal: '#3FE0C5', // phosphor — the live running-best trace
  accept: '#54D6A0',
  merge: '#8BE9C4', // byte-identical = directly mergeable
  regress: '#E8603F',
  noise: '#6C7C8A',
  critic: '#B98BFF', // the second judge
  steel: '#3B4A59', // cold = untouchable floor
  warm: '#C68A3C',
  hot: '#E8603F', // hot = the lever you can move
};

// verdict / status -> color (dark dossier)
export const COL: Record<string, string> = {
  accepted: T.accept,
  'within-noise': T.noise,
  'noise-limited': '#D9A23B',
  regressed: T.regress,
  'verify-failed': T.regress,
  'build-failed': '#C77C4A',
  rejected: T.regress,
  unlocated: '#C77C4A',
  skipped: '#C77C4A',
  running: T.ink2,
};

export const col = (s: string | null | undefined): string => COL[s ?? ''] || T.noise;

// --- hex lerp, for the heat ramp -------------------------------------------------
const _h = (n: number) => Math.round(Math.max(0, Math.min(255, n))).toString(16).padStart(2, '0');
function lerp(a: string, b: string, t: number): string {
  const pa = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16));
  const pb = [1, 3, 5].map((i) => parseInt(b.slice(i, i + 2), 16));
  return '#' + pa.map((v, i) => _h(v + (pb[i] - v) * t)).join('');
}

// Heat for a hot *lever* frame, keyed to self-time% (cool olive-amber -> hot ember).
// Cold steel is reserved for the untouchable floor / skipped frames.
export function heat(pct: number | null | undefined, maxPct: number): string {
  const t = Math.max(0, Math.min(1, (pct ?? 0) / (maxPct || 1.2)));
  return lerp('#8C7A46', T.hot, Math.pow(t, 0.85));
}
export function heatGrad(pct: number | null | undefined, maxPct: number): string {
  const c = heat(pct, maxPct);
  return `linear-gradient(95deg, ${c}, ${lerp(c, '#0E1419', 0.22)})`;
}

export function dpct(d: unknown): string {
  return typeof d === 'number' ? (d >= 0 ? '+' : '') + d.toFixed(2) + '%' : '—';
}
