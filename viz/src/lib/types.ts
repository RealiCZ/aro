// Data contract — mirrors `build_tree`'s output in aro/tree.py.

export interface Metric {
  metric: string;
  delta_pct: number;
  ci_low_pct: number;
  ci_high_pct: number;
  floor_pct: number;
  improved: boolean;
  regressed?: boolean;
}

export interface CriticReason {
  rubric: string;
  finding: string;
  severity?: string;   // none | low | high
  example?: string;    // a matched known-bad example (e.g. PR#313), if any
}

export interface Critic {
  verdict: string;     // pass | pass-risk | reject
  reasons: CriticReason[];
}

export interface Candidate {
  id: string;
  hypothesis: string;
  verdict: string;
  metrics: Metric[];
  notes: string[];
  /** The SECOND judge's verdict + structured reasons (null if the critic was off). */
  critic?: Critic | null;
  /** Compact unified diff text (lines: `# ` file header, `@@` hunk, `+`/`-`/` `). */
  diff: string;
  /** Which attempt # this candidate came from (set when merging repeated attempts). */
  _attempt?: number;
}

export interface ReflectDir {
  id: string;
  text: string;
  tried: boolean;
  /** Which attempt # this direction came from (set when merging repeated attempts). */
  _attempt?: number;
}

export interface FnNode {
  type: 'fn';
  id: string;
  i: number;
  fn: string;
  regime?: string | null;
  pct?: number | null;
  files: string[];
  reflect: ReflectDir[];
  candidates: Candidate[];
  status?: string | null;
  delta?: number | null;
  accepted?: boolean | null;
  decision?: string | null;
  reason?: string | null;
  realized?: number | null;
  headroom?: number | null;
  /** Attempt indices merged into this node (a function can be attempted more than once). */
  attempts?: number[];
}

export interface SkippedNode {
  type: 'skipped';
  id: string;
  fn: string;
  reason?: string | null;
  // present so unified node access stays type-safe in shared rendering
  i?: undefined;
  pct?: undefined;
  status?: undefined;
  delta?: undefined;
}

export type TreeNode = FnNode | SkippedNode;

export interface CoverageSeg {
  key: string;
  label: string;
  pct: number;
  color: string;
  hatch?: boolean;
}

export interface FloorFrame {
  name: string;
  pct: number;
  owner: string;
  why: string;
}

export interface Summary {
  attempted: number;
  accepted: number;
  skipped: number;
  realized_pct: number;
  headroom_pct: number;
  floor_pct: number;
  unreachable_pct: number;
  decision: string;
  reason: string;
  frontier: string[];
  coverage: CoverageSeg[];
  floor_frames: FloorFrame[];
  tokens?: number;
  cost_usd?: number;
  critic_rejects?: number;
  apply_fails?: number;
  ceiling_pct?: number;
}

export interface TreeData {
  spec: string;
  summary: Summary;
  nodes: TreeNode[];
  /** Self-contained SVG of the perf-vs-cumulative-token figure (rendered by Python). */
  perf_svg?: string;
}

// The "current detail" state the right pane renders from.
export type Detail =
  | { kind: 'fn'; node: FnNode; ci: number }
  | { kind: 'skip'; node: SkippedNode }
  | { kind: 'reflect'; dir: ReflectDir; node: FnNode }
  | { kind: 'cov'; key: string }
  | null;
