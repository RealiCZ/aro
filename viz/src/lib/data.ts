import sampleJson from './sample-tree.json';
import type { TreeData, TreeNode, FnNode } from './types';

// Read Python-injected data if present (window.__ARO_DATA__), else fall back to the
// bundled sample so `npm run dev` works with real-looking data.
export const DATA: TreeData =
  (globalThis as typeof globalThis & { __ARO_DATA__?: TreeData }).__ARO_DATA__ ??
  (sampleJson as unknown as TreeData);

// Merge repeated attempts of the SAME function into ONE node — the explorer can attempt a
// function more than once (e.g. sstore twice), and showing it twice is confusing. Candidates
// from every attempt are kept (tagged with `_attempt` so the UI can disambiguate their ids);
// status/accepted/delta = the best across attempts; pct stays the function's self-time (not
// doubled). Skipped nodes pass through unchanged.
export function mergeNodes(nodes: TreeNode[]): TreeNode[] {
  const map = new Map<string, FnNode & { attempts: number[] }>();
  const out: TreeNode[] = [];
  for (const n of nodes) {
    if (n.type === 'skipped') {
      out.push(n);
      continue;
    }
    const cands = (n.candidates ?? []).map((c) => ({ ...c, _attempt: n.i }));
    const refls = (n.reflect ?? []).map((r) => ({ ...r, _attempt: n.i }));
    const existing = map.get(n.fn);
    if (!existing) {
      const m = {
        ...n,
        candidates: cands,
        reflect: refls,
        attempts: [n.i],
      } as FnNode & { attempts: number[] };
      map.set(n.fn, m);
      out.push(m);
    } else {
      existing.attempts.push(n.i);
      existing.candidates = existing.candidates.concat(cands);
      existing.reflect = (existing.reflect ?? []).concat(refls);
      if (n.accepted) {
        existing.accepted = true;
        existing.status = 'accepted';
        if (
          typeof n.delta === 'number' &&
          (typeof existing.delta !== 'number' ||
            Math.abs(n.delta) > Math.abs(existing.delta))
        )
          existing.delta = n.delta;
      }
    }
  }
  return out;
}

// The display node list (merged) — both the icicle and the detail-panel coverage lists use it.
export const NODES: TreeNode[] = mergeNodes(DATA.nodes);
