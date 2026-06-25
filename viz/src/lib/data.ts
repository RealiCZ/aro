import sampleJson from './sample-tree.json';
import type { TreeData } from './types';

// Read Python-injected data if present (window.__ARO_DATA__), else fall back to the
// bundled sample so `npm run dev` works with real-looking data.
export const DATA: TreeData =
  (globalThis as typeof globalThis & { __ARO_DATA__?: TreeData }).__ARO_DATA__ ??
  (sampleJson as unknown as TreeData);
