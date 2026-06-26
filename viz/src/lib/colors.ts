// ARO dossier palette — a light "measurement sheet" (cool engineering paper, hairlines,
// mono data). Color encodes DATA: a heat ramp for hotness, a teal signal for the live
// trace, semantic verdict hues.

export const T = {
  ground: '#EAEEF3', // cool paper (not white, not cream)
  panel: '#FFFFFF',
  panel2: '#F3F6FA',
  rule: '#E2E8F0',
  rule2: '#D5DEE8',
  ink: '#1B2530',
  ink2: '#566472',
  mute: '#8693A1',
  signal: '#0E9F8C', // teal — the live running-best trace
  accept: '#15945F',
  merge: '#0E9E72', // byte-identical = directly mergeable
  regress: '#D4492C',
  noise: '#7E8C9A',
  critic: '#7A45D4', // the second judge
  steel: '#9FB1BF', // cold = untouchable floor
  warm: '#D49A4A',
  hot: '#DE5836', // hot = the lever you can move
};

// verdict / status -> color
export const COL: Record<string, string> = {
  accepted: T.accept,
  'within-noise': T.noise,
  'noise-limited': '#C2841E',
  regressed: T.regress,
  'verify-failed': T.regress,
  'build-failed': '#C76E2C',
  rejected: T.regress,
  unlocated: '#C76E2C',
  skipped: '#C76E2C',
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

// Heat for a hot *lever* frame, keyed to self-time% (cool amber -> hot ember).
export function heat(pct: number | null | undefined, maxPct: number): string {
  const t = Math.max(0, Math.min(1, (pct ?? 0) / (maxPct || 1.2)));
  return lerp('#E7C77E', T.hot, Math.pow(t, 0.85));
}
export function heatGrad(pct: number | null | undefined, maxPct: number): string {
  const c = heat(pct, maxPct);
  return `linear-gradient(95deg, ${c}, ${lerp(c, '#FFFFFF', 0.16)})`;
}

export function dpct(d: unknown): string {
  return typeof d === 'number' ? (d >= 0 ? '+' : '') + d.toFixed(2) + '%' : '—';
}
