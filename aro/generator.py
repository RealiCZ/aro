"""Generators: the (deliberately thin) generation layer.

Per ARO-eng.md the loop driver is commodity; the engineering weight is the judge.
Three generators implement `propose(ctx, n)`; the spec's `generator` slot picks
the live one:

- PlannedGenerator: returns a pre-authored candidate per round, so the full
  pipeline runs end-to-end against real cargo build/test/bench without a live
  model — the reproducible MVP driver (used by selftest / verify_patch).
- RalphGenerator (`generator: "ralph"`): the THIN live driver — one read-only
  `claude -p` per round returns a block-format patch (the pure Ralph loop, cf.
  `ralph.sh`). Cheap and fast; best for single-site micro-opts. No read/reflect.
- AgenticGenerator (`generator: "agentic"`, default): the HEAVY live driver — a
  writable throwaway worktree where `claude` edits→build→test→fix until it
  compiles; ARO takes the diff. Adds the read phase + reflect agenda. Best for
  multi-site refactors (e.g. precompute-K) a one-shot patch can't express.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from . import prompts
from .types import Candidate, Edit, GenContext, Patch


class PlannedGenerator:
    """Seeded MVP driver: yields `plans[ctx.round]` if present. Each plan is a
    tuple `(id_suffix, hypothesis, [Edit, ...])`; an empty edit list is a NoOp."""
    name = "planned"

    def __init__(self, plans):
        self.plans = plans

    def propose(self, ctx: GenContext, n: int):
        if ctx.round >= len(self.plans):
            return []
        suffix, hyp, edits = self.plans[ctx.round]
        return [Candidate(
            id=f"{suffix}-r{ctx.round}",
            hypothesis=hyp,
            patch=Patch(edits=list(edits)),
        )]


class RalphGenerator:
    """The thin live driver: a fresh read-only `claude -p` per round (the pure
    Ralph loop, cf. `ralph.sh`), parsing a block-format answer into a patch. Cheap
    and fast — no writable worktree, no read/reflect. Defensive: any failure
    yields no candidate this round."""
    name = "ralph-claude"

    def __init__(self, repo, timeout_secs: int = 600):
        self.repo = repo
        # A high hang-guard, not a work-cap — a thin `claude -p` patch lands fast;
        # this only stops a wedged call blocking the harness forever.
        self.timeout_secs = timeout_secs

    def propose(self, ctx: GenContext, n: int):
        prompt = self._build_prompt(ctx)
        try:
            # Bare `claude` (NOT --dangerously-skip-permissions): default perms
            # block writes, so it can only READ and return text. The patch is
            # applied later, in an isolated worktree, by the judge (maker-checker).
            out = subprocess.run(["claude", "-p", prompt], cwd=str(self.repo),
                                 capture_output=True, text=True,
                                 timeout=self.timeout_secs)
        except Exception:
            return []
        if out.returncode != 0:
            return []
        parsed = parse_response(out.stdout)
        if not parsed or not parsed[1]:
            return []
        hyp, edits = parsed
        return [Candidate(id=f"ralph-r{ctx.round}", hypothesis=hyp,
                          patch=Patch(edits=edits))]

    def _build_prompt(self, ctx: GenContext) -> str:
        # Template in skill/prompts/ralph.md. memory_summary already carries the
        # open agenda, so even the thin loop sees the forward-looking directions.
        objectives = "\n".join(f"  - {o.metric}" for o in ctx.objectives) or "  (none)"
        region = (f"\nProfiler hint (where the work is):\n{ctx.region_hint}"
                  if ctx.region_hint else "")
        return prompts.load("ralph", objectives=objectives,
                            memory=ctx.memory_summary.strip(), region_hint=region)


class AgenticGenerator:
    """Write-compile-fix generator: gives claude a *writable* throwaway worktree
    where it edits, runs `cargo build/test`, and iterates until it builds and
    passes — then ARO takes the resulting git diff as the candidate. This is what
    a multi-site, type-changing refactor (e.g. precompute-K: change the table
    layout in `new` AND consume it in `add_affine_point`) needs — a one-shot text
    patch can't reliably express it. The diff is re-evaluated independently by the
    judge (maker-checker preserved); the agent's own build/test is just so it
    hands back something that compiles.

    Runs with `--dangerously-skip-permissions` so the agent can Edit/Bash, but
    ONLY inside the throwaway worktree (auto-removed after). Each changed `.rs`
    file becomes a whole-file Edit (search = baseline blob, replace = new content),
    which the guard still screens and the judge re-applies on a clean baseline."""
    name = "agentic-claude"

    def __init__(self, target, timeout_secs: int = 3600):
        self.target = target          # provides make_worktree/remove_worktree/repo/target_dir
        # NOT a work cap — the agent stops itself when build+test pass (its goal in
        # the prompt). This is only a high hang-guard so a wedged `claude -p` can't
        # block the harness forever. A work-cap kills big refactors mid-edit (0
        # output); the judge — not the clock — is the real gate.
        self.timeout_secs = timeout_secs

    def propose(self, ctx: GenContext, n: int):
        t = self.target
        try:
            scratch = t.make_worktree(f"agentic-r{ctx.round}")
        except Exception:
            return []
        try:
            env = dict(os.environ)
            env["CARGO_TARGET_DIR"] = str(t.target_dir)
            try:
                out = subprocess.run(
                    ["claude", "--dangerously-skip-permissions", "-p", self._prompt(ctx)],
                    cwd=str(scratch), env=env, capture_output=True, text=True,
                    timeout=self.timeout_secs)
            except Exception:
                return []

            hypo = self._hypothesis(out.stdout)
            edits = self._diff_to_edits(scratch)
            if not edits:
                return []
            return [Candidate(id=f"agent-r{ctx.round}", hypothesis=hypo,
                              patch=Patch(edits=edits))]
        finally:
            t.remove_worktree(scratch)

    def _diff_to_edits(self, scratch) -> list:
        """Each modified tracked `.rs` file -> a whole-file Edit (baseline blob ->
        new content). New/untracked files are skipped (the judge would fail to
        build an incomplete patch; precompute-K needs no new files)."""
        st = subprocess.run(["git", "-C", str(scratch), "status", "--porcelain"],
                            capture_output=True, text=True)
        edits = []
        for line in st.stdout.splitlines():
            path = line[3:].strip().strip('"')
            if not path.endswith(".rs"):
                continue
            blob = subprocess.run(["git", "-C", str(scratch), "show", f"HEAD:{path}"],
                                  capture_output=True, text=True)
            if blob.returncode != 0:
                continue  # untracked / new file
            try:
                new = (Path(scratch) / path).read_text()
            except Exception:
                continue
            if blob.stdout != new:
                edits.append(Edit(path=path, search=blob.stdout, replace=new))
        return edits

    def understand(self, ctx: GenContext):
        """Read phase: a READ-ONLY claude analysis that returns a concrete plan
        (what to change + why it's safe + layout), WITHOUT implementing. Decouples
        deriving the change from executing it — grounds the implementation and
        keeps the expensive write-loop focused on a known plan. Returns None on
        failure (the loop then proceeds plan-less)."""
        mem = ctx.memory_summary.strip() if (
            ctx.memory_summary and "first round" not in ctx.memory_summary) else ""
        prior = ("\nPrior attempts (don't repeat these dead ends):\n" + mem) if mem else ""
        prompt = prompts.load("read", prior=prior, region_hint=ctx.region_hint or "",
                              agenda=self._agenda_text(ctx))
        try:
            out = subprocess.run(["claude", "-p", prompt], cwd=str(self.target.repo),
                                 capture_output=True, text=True, timeout=600)
        except Exception:
            return None
        return out.stdout.strip()[:4000] if out.returncode == 0 and out.stdout.strip() else None

    def reflect(self, ctx: GenContext, outcomes: list):
        """Reflect step (read-only): read this round's verdicts + the open agenda
        and return the forward-looking research directions for next round —
        {"resolve": [(id, status)...], "add": [{direction, rationale}...]}. This is
        what makes the loop accumulate *direction*, not just a list of dead ends:
        a within-noise result becomes a concrete "try this layout/variant next"
        item. Generation-side — the deterministic judge still decides whether a
        direction wins. Returns None on failure (the loop proceeds agenda-less).

        The prompt tells it autoresearch's Ideate priority ladder: fix crashes >
        exploit a success's variants > combine near-misses > change data-layout /
        try the opposite > radical rewrite."""
        results = []
        for cand, o in outcomes:
            ds = "; ".join(
                f"{d.metric} Δ{d.delta_pct:+.2f}% (floor {d.floor_pct:.2f}%, "
                f"{'improved' if d.improved else 'no'})" for d in o.deltas)
            results.append(f"- {cand.id} [{o.verdict.value}] "
                           f"{cand.hypothesis[:160]} | {ds or 'no metrics'}")
        agenda = "\n".join(f"- [{d.id}] {d.direction} (why: {d.rationale})"
                           for d in ctx.agenda) or "(empty)"
        prompt = prompts.load("reflect",
                              results="\n".join(results) or "(no candidates)",
                              agenda=agenda, region_hint=ctx.region_hint or "")
        try:
            out = subprocess.run(["claude", "-p", prompt], cwd=str(self.target.repo),
                                 capture_output=True, text=True, timeout=600)
        except Exception:
            return None
        return _parse_reflect(out.stdout) if out.returncode == 0 else None

    @staticmethod
    def _agenda_text(ctx: GenContext) -> str:
        if not ctx.agenda:
            return ""
        items = "\n".join(f"  - [{d.id}] {d.direction} (why: {d.rationale})"
                          for d in ctx.agenda)
        return ("\nOpen research agenda (prefer the TOP item — it is the highest-"
                "leverage next step distilled from prior rounds):\n" + items)

    def _prompt(self, ctx: GenContext) -> str:
        # Template lives in skill/prompts/agentic.md (auditable / swappable).
        mem = ctx.memory_summary.strip() if (
            ctx.memory_summary and "first round" not in ctx.memory_summary) else ""
        prior = ""
        if mem:
            prior = ("\nPrior attempts (build on them — do NOT just repeat a "
                     "within-noise approach unchanged; reflect on WHY it didn't win. A "
                     "data-layout change that enlarges a hot, cache-resident struct can "
                     "add memory traffic that cancels a compute saving — the same idea "
                     "with a cache-friendlier layout may win):\n" + mem)
        plan = (f"\nImplementation plan (from the read phase — follow it):\n{ctx.plan}"
                if ctx.plan else "")
        return prompts.load("agentic", prior=prior, plan=plan,
                            region_hint=ctx.region_hint or "",
                            agenda=self._agenda_text(ctx))

    @staticmethod
    def _hypothesis(stdout: str) -> str:
        for ln in reversed(stdout.splitlines()):
            s = ln.strip()
            if s.upper().startswith("SUMMARY:"):
                return s[len("SUMMARY:"):].strip()[:300] or "agentic optimization"
        for ln in stdout.splitlines():
            if ln.strip():
                return ln.strip()[:300]
        return "agentic optimization"


def parse_response(stdout: str):
    """Parse a block-format answer into `(hypothesis, [Edit, ...])` for the thin
    Ralph driver. Tolerant of surrounding prose. None if no hypothesis and no edit."""
    hypothesis = ""
    edits: list = []
    state = "idle"          # idle | search | replace
    cur_path = ""
    search_buf: list = []
    replace_buf: list = []

    def flush():
        nonlocal cur_path, search_buf, replace_buf
        if cur_path and search_buf:
            edits.append(Edit(path=cur_path,
                              search="\n".join(search_buf),
                              replace="\n".join(replace_buf)))
        cur_path = ""
        search_buf = []
        replace_buf = []

    for line in stdout.splitlines():
        t = line.lstrip()
        if t.startswith("@@HYPOTHESIS@@"):
            if not hypothesis:
                hypothesis = t[len("@@HYPOTHESIS@@"):].strip()
        elif t.startswith("@@FILE@@"):
            flush()
            cur_path = t[len("@@FILE@@"):].strip()
            state = "idle"
        elif t.startswith("@@SEARCH@@"):
            state = "search"
        elif t.startswith("@@REPLACE@@"):
            state = "replace"
        elif t.startswith("@@END@@"):
            flush()
            state = "idle"
        else:
            if state == "search":
                search_buf.append(line)
            elif state == "replace":
                replace_buf.append(line)
    flush()

    if not hypothesis and not edits:
        return None
    if not hypothesis:
        hypothesis = "(no hypothesis given)"
    return (hypothesis, edits)


def _parse_reflect(stdout: str):
    """Extract the reflect JSON {resolve:[{id,status}], add:[{direction,rationale}]}
    from claude's answer, tolerant of surrounding prose. None if nothing usable."""
    m = re.search(r"\{.*\}", stdout, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None
    resolve = [(r.get("id"), r.get("status", "done"))
               for r in obj.get("resolve", []) if isinstance(r, dict) and r.get("id")]
    add = [{"direction": (a.get("direction") or "").strip(),
            "rationale": (a.get("rationale") or "").strip()}
           for a in obj.get("add", [])
           if isinstance(a, dict) and (a.get("direction") or "").strip()]
    if not resolve and not add:
        return None
    return {"resolve": resolve, "add": add}
