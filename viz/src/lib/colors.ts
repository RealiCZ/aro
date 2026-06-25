// Verdict/status -> color map. Copied verbatim from tree.py's COL/col/dpct.

export const COL: Record<string, string> = {
  accepted: '#16a34a',
  'within-noise': '#64748b',
  'noise-limited': '#ca8a04',
  regressed: '#dc2626',
  'verify-failed': '#dc2626',
  'build-failed': '#ea580c',
  rejected: '#dc2626',
  unlocated: '#ea580c',
  skipped: '#ea580c',
  running: '#94a3b8',
};

export const col = (s: string | null | undefined): string => COL[s ?? ''] || '#64748b';

export function dpct(d: unknown): string {
  return typeof d === 'number' ? (d >= 0 ? '+' : '') + d.toFixed(2) + '%' : '—';
}
