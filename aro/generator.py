"""Generators: the (deliberately thin) generation layer.

Per the design doc the loop driver is commodity; the engineering weight is the judge.
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
  multi-site refactors a one-shot patch can't express.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import lessons, prompts
from . import vcs
from .llm import LLMError, run_claude
from .types import Candidate, Edit, GenContext, Patch


def _emit(ctx, **fields):
    """Report a generation failure through the engine's event hook (traceable in
    events.jsonl) — a broken generator must never be indistinguishable from
    'the model proposed nothing'."""
    if getattr(ctx, "emit", None):
        try:
            ctx.emit("generator_error", **fields)
        except Exception:
            pass


# Optimization "lens" ladder — a fanned-out attempt tries several lenses in parallel,
# and successive ROUNDS on the same function climb the ladder (micro → layout → algorithm),
# so a function that resists a cheap fix gets a more structural attempt next round.
_LENS_LADDER = [
    ("micro-elimination",
     "Eliminate redundant work on the hot path: hoist loop-invariant computation out of "
     "loops, cache a repeated lookup, stop recomputing a value, drop a dead branch. The "
     "smallest, safest change — try this first."),
    ("data-layout / allocation",
     "Cut memory traffic: reuse a buffer instead of allocating, avoid clones/copies, prefer "
     "borrowing, tighten a hot struct's field layout for cache locality. NOTE a layout change "
     "that ENLARGES a cache-resident struct can add traffic that cancels a compute saving — weigh it."),
    ("algorithm",
     "Change the algorithm itself: lower the complexity, fuse or eliminate a pass, precompute "
     "an invariant table, replace a repeated O(n) scan with an O(1) lookup. The biggest, most "
     "structural wins — still keep behaviour byte-identical."),
]

def _lens_for(round_idx: int, k: int):
    """The lens for candidate k in round `round_idx`: the n parallel candidates SPREAD across
    the ladder starting at the round's tier, so one round fans micro→layout→algorithm AND later
    rounds climb. Returns (name, guidance)."""
    return _LENS_LADDER[min(round_idx + k, len(_LENS_LADDER) - 1)]

def _lens_text(lens) -> str:
    name, guidance = lens
    return (f"\nOptimization lens for THIS attempt (focus here first; other angles allowed if "
            f"they're the real win):\n  [{name}] {guidance}")


def _constraints_text(spec) -> str:
    """Format the spec's constraints into a prompt block so the generator actually
    SEES the hard rules (editable surface, no-new-deps, byte-identical, and any
    free-form notes like 'don't change the public API / this tuning constant')."""
    c = getattr(spec, "constraints", None) or {}
    lines = []
    ed = c.get("editable")
    if ed:
        lines.append(f"  - edit ONLY these files: {', '.join(ed)} (edits elsewhere are auto-rejected)")
    if c.get("no_new_deps", True):
        lines.append("  - add no dependencies; do not swap in a library")
    if c.get("byte_identical", True):
        lines.append("  - behaviour must stay byte-identical for every input")
    if c.get("notes"):
        lines.append(f"  - {c['notes']}")
    return ("\nConstraints (HARD — respect every one):\n" + "\n".join(lines)) if lines else ""


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
    yields no candidate this round.

    Reads the ADVANCED baseline: when prior rounds accepted patches (`ctx.base_edits`),
    it reads in a throwaway worktree with those edits applied+committed, so its
    SEARCH blocks come from the advanced code — otherwise a 2nd-round same-file patch
    would be searched against the original and mismatch when the judge re-applies the
    base patch first. Round 0 (no base edits) reads the repo directly (cheap)."""
    name = "ralph-claude"

    def __init__(self, target, timeout_secs: int = 600, gen_concurrency: int = 8):
        self.target = target
        self.repo = target.repo
        # A high hang-guard, not a work-cap — a thin `claude -p` patch lands fast;
        # this only stops a wedged call blocking the harness forever.
        self.timeout_secs = timeout_secs
        self.gen_concurrency = gen_concurrency

    def propose(self, ctx: GenContext, n: int):
        import concurrent.futures as _cf
        n = max(1, int(n))
        lenses = [_lens_for(ctx.round, k) for k in range(n)]
        if n == 1:
            c = self._one_candidate(ctx, 0, lenses[0], True)
            return [c] if c else []
        out = []
        with _cf.ThreadPoolExecutor(max_workers=min(n, self.gen_concurrency)) as ex:
            futs = [ex.submit(self._one_candidate, ctx, k, lenses[k], False) for k in range(n)]
            for f in _cf.as_completed(futs):
                try:
                    c = f.result()
                except Exception:
                    c = None
                if c:
                    out.append(c)
        out.sort(key=lambda c: c.id)   # deterministic order regardless of completion order
        return out

    def _one_candidate(self, ctx: GenContext, k: int, lens, single: bool):
        prompt = self._build_prompt(ctx, lens)
        scratch = None
        try:
            cwd = self.repo
            if ctx.base_edits:
                # Seed a throwaway worktree with the accepted patch so SEARCH blocks
                # come from the advanced baseline. Fail-fast (like the agentic driver):
                # a silent advance failure would make the patch unappliable in the judge.
                try:
                    scratch = self.target.make_worktree(f"ralph-r{ctx.round}-{k}")
                    self.target.apply(Patch(edits=list(ctx.base_edits)), scratch)
                    cm = vcs.commit_all(scratch, "aro: advanced baseline")
                    if cm.returncode != 0:
                        _emit(ctx, generator="ralph", stage="seed-commit", k=k,
                              detail=(cm.stderr or "")[-200:])
                        return None
                except Exception as e:
                    _emit(ctx, generator="ralph", stage="seed", k=k, detail=str(e)[:200])
                    return None
                cwd = scratch
            try:
                # Bare `claude` (read-only; maker-checker — the judge applies the patch
                # later in an isolated worktree). json output captures the token spend.
                text, toks, cost = run_claude(prompt, cwd=cwd, timeout=self.timeout_secs)
            except LLMError as e:
                _emit(ctx, generator="ralph", stage="claude", k=k, detail=str(e)[:200])
                return None
            parsed = parse_response(text)
            if not parsed or not parsed[1]:
                _emit(ctx, generator="ralph", stage="parse", k=k,
                      detail="no parseable block patch in reply")
                return None
            hyp, edits = parsed
            cid = f"ralph-r{ctx.round}" if single else f"ralph-r{ctx.round}-{k}"
            return Candidate(id=cid, hypothesis=hyp, patch=Patch(edits=edits),
                             lens=(lens[0] or None) if lens else None,
                             tokens=toks, cost_usd=cost)
        finally:
            if scratch is not None:
                self.target.remove_worktree(scratch)

    def _build_prompt(self, ctx: GenContext, lens=("", "")) -> str:
        # Template in skill/prompts/ralph.md. memory_summary carries the open agenda;
        # lessons.summary() adds cross-run dead-ends so even the thin loop doesn't
        # repeat a known regression. Objectives are direction-tagged so a `maximize`
        # metric isn't (mis)framed as "minimize".
        objectives = "\n".join(
            f"  - {o.metric} ({'minimize' if o.minimize else 'maximize'})"
            for o in ctx.objectives) or "  (none)"
        region = (f"\nProfiler hint (where the work is):\n{ctx.region_hint}"
                  if ctx.region_hint else "")
        lens_block = _lens_text(lens) if lens and lens[0] else ""
        return prompts.load("ralph", objectives=objectives,
                            memory=ctx.memory_summary.strip(),
                            lessons=lessons.summary(), region_hint=region,
                            constraints=_constraints_text(self.target.spec),
                            lens=lens_block)


class AgenticGenerator:
    """Write-compile-fix generator: gives claude a *writable* throwaway worktree
    where it edits, runs `cargo build/test`, and iterates until it builds and
    passes — then ARO takes the resulting git diff as the candidate. This is what
    a multi-site, type-changing refactor (e.g. changing a struct's layout in one
    place AND consuming it in another) needs — a one-shot text patch can't
    reliably express it. The diff is re-evaluated independently by the
    judge (maker-checker preserved); the agent's own build/test is just so it
    hands back something that compiles.

    Runs with `--dangerously-skip-permissions` so the agent can Edit/Bash, but
    ONLY inside the throwaway worktree (auto-removed after). Each changed `.rs`
    file becomes a whole-file Edit (search = the pre-agent content, anchored to the
    EXACT base-edit `replace` the judge applies — not a git blob — so it re-applies
    byte-exactly; replace = new content), which the guard screens and the judge
    re-applies on a clean baseline (see `_diff_to_edits`)."""
    name = "agentic-claude"

    def __init__(self, target, timeout_secs: int = 3600, gen_concurrency: int = 8):
        self.target = target          # provides make_worktree/remove_worktree/repo/target_dir
        # NOT a work cap — the agent stops itself when build+test pass (its goal in
        # the prompt). This is only a high hang-guard so a wedged `claude -p` can't
        # block the harness forever. A work-cap kills big refactors mid-edit (0
        # output); the judge — not the clock — is the real gate.
        self.timeout_secs = timeout_secs
        self.gen_concurrency = gen_concurrency

    def propose(self, ctx, n):
        import concurrent.futures as _cf
        n = max(1, int(n))
        lenses = [_lens_for(ctx.round, k) for k in range(n)]
        if n == 1:
            c = self._one_candidate(ctx, 0, lenses[0], True)
            return [c] if c else []
        out = []
        with _cf.ThreadPoolExecutor(max_workers=min(n, self.gen_concurrency)) as ex:
            futs = [ex.submit(self._one_candidate, ctx, k, lenses[k], False) for k in range(n)]
            for f in _cf.as_completed(futs):
                try:
                    c = f.result()
                except Exception:
                    c = None
                if c:
                    out.append(c)
        out.sort(key=lambda c: c.id)   # deterministic order regardless of completion order
        return out

    def _one_candidate(self, ctx: GenContext, k: int, lens, single: bool):
        t = self.target
        try:
            # Unique worktree name per k so concurrent candidates don't collide.
            scratch = t.make_worktree(f"agentic-r{ctx.round}-{k}")
        except Exception as e:
            _emit(ctx, generator="agentic", stage="worktree", k=k, detail=str(e)[:200])
            return None
        try:
            # Seed the scratch with the accepted patch so the agent edits — and we
            # diff — against the CURRENT advanced baseline, not the original. Commit
            # it so `git show HEAD:` is the advanced blob; without this a 2nd-round
            # edit to the same file can't apply on top of the 1st (the whole-file
            # search would be the original content, not the advanced).
            if ctx.base_edits:
                # Commit the accepted patch so `git show HEAD:` is the ADVANCED blob
                # the agent edits — and we diff — against. Pin an identity so a machine
                # with no git user.name/email configured doesn't fail here: a silent
                # failure would leave HEAD at the ORIGINAL blob, _diff_to_edits would
                # take the original as the whole-file SEARCH, and the judge (which
                # applies base_edits first) would then fail to match it. So a failed
                # advance must abort the candidate, not pass silently.
                try:
                    t.apply(Patch(edits=list(ctx.base_edits)), scratch)
                    cm = vcs.commit_all(scratch, "aro: advanced baseline")
                    dirty = vcs.status_porcelain(scratch).strip()
                except Exception as e:
                    _emit(ctx, generator="agentic", stage="seed", k=k, detail=str(e)[:200])
                    return None
                if cm.returncode != 0 or dirty:
                    # Baseline did not advance — emitting a candidate now would diff
                    # against the wrong base and mismatch in the judge. No candidate.
                    _emit(ctx, generator="agentic", stage="seed-commit", k=k,
                          detail=(cm.stderr or dirty or "")[-200:])
                    return None
            env = dict(os.environ)
            env["CARGO_TARGET_DIR"] = str(t.td_for(scratch))
            try:
                # allow_write: the agent edits/builds INSIDE the throwaway worktree only
                # (we take the git diff); json output captures its token usage.
                text, toks, cost = run_claude(self._prompt(ctx, lens), cwd=scratch,
                                              env=env, timeout=self.timeout_secs,
                                              allow_write=True)
            except LLMError as e:
                _emit(ctx, generator="agentic", stage="claude", k=k, detail=str(e)[:200])
                return None

            hypo = self._hypothesis(text)
            edits = self._diff_to_edits(scratch, ctx.base_edits)
            if not edits:
                _emit(ctx, generator="agentic", stage="diff", k=k,
                      detail="agent made no usable .rs edits")
                return None
            cid = f"agent-r{ctx.round}" if single else f"agent-r{ctx.round}-{k}"
            return Candidate(id=cid, hypothesis=hypo, patch=Patch(edits=edits),
                             lens=(lens[0] or None) if lens else None,
                             tokens=toks, cost_usd=cost)
        finally:
            t.remove_worktree(scratch)

    def _diff_to_edits(self, scratch, base_edits=None) -> list:
        """Each modified tracked `.rs` file -> a whole-file Edit (pre-agent content ->
        new content). New/untracked files are skipped (the judge would fail to build an
        incomplete patch; the target change needs no new files).

        The SEARCH (pre-agent content) is anchored to the EXACT string the judge will
        have on disk when it re-applies this candidate — NOT `git show HEAD:` — to kill
        the drift that made a 2nd-attempt edit to an already-accepted file fail to apply.
        The judge re-applies `base_edits` to a clean baseline by string replacement,
        leaving each base-touched file's content byte-equal to that base edit's `replace`.
        The scratch was seeded the same way, so the agent edited on top of that exact
        string. Anchoring SEARCH to `base_edits[path].replace` (the same object the judge
        applies) makes apply(base)+apply(candidate) chain byte-exactly. A `git show HEAD:`
        blob can round-trip through git's newline/EOL normalization and drift one byte,
        which is what broke the chain. For files no base edit touched, HEAD == the judge's
        pristine checkout, so `git show` matches and is the correct anchor."""
        base_latest = {}
        for e in (base_edits or []):
            base_latest[e.path] = e.replace        # last write wins (apply order)
        try:
            status = vcs.status_porcelain(scratch)
        except Exception:
            # A broken worktree (agent-left index.lock, corrupted index) must skip
            # THIS candidate, not abort the whole backtest — parity with the n>1
            # fan-out path, which maps any candidate failure to None.
            return []
        edits = []
        for line in status.splitlines():
            path = line[3:].strip().strip('"')
            if not path.endswith(".rs"):
                continue
            try:
                new = (Path(scratch) / path).read_text()
            except Exception:
                continue
            if path in base_latest:
                before = base_latest[path]         # exact judge-apply output; no git round-trip
            else:
                before = vcs.show_blob(scratch, f"HEAD:{path}")
                if before is None:
                    continue  # untracked / new file
            if before != new:
                edits.append(Edit(path=path, search=before, replace=new))
        return edits

    def understand(self, ctx: GenContext):
        """Read phase: a READ-ONLY claude analysis that returns a concrete plan
        (what to change + why it's safe + layout), WITHOUT implementing. Decouples
        deriving the change from executing it — grounds the implementation and
        keeps the expensive write-loop focused on a known plan. Returns
        `(plan_or_None, output_tokens)` — the tokens feed the cumulative-token chart."""
        mem = ctx.memory_summary.strip() if (
            ctx.memory_summary and "first round" not in ctx.memory_summary) else ""
        prior = ("\nPrior attempts (don't repeat these dead ends):\n" + mem) if mem else ""
        prompt = prompts.load("read", prior=prior, region_hint=ctx.region_hint or "",
                              agenda=self._agenda_text(ctx), lessons=self._lessons(),
                              constraints=_constraints_text(self.target.spec))
        try:
            text, toks, _ = run_claude(prompt, cwd=self.target.repo, timeout=600)
        except LLMError as e:
            _emit(ctx, generator="agentic", stage="read", detail=str(e)[:200])
            return (None, 0)
        plan = text.strip()[:4000] if text and text.strip() else None
        return (plan, toks)

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
                              agenda=agenda, region_hint=ctx.region_hint or "",
                              lessons=self._lessons())
        try:
            text, toks, _ = run_claude(prompt, cwd=self.target.repo, timeout=600)
        except LLMError as e:
            _emit(ctx, generator="agentic", stage="reflect", detail=str(e)[:200])
            return None
        upd = _parse_reflect(text)
        if upd is not None:
            upd["_tokens"] = toks   # carried so the engine can record reflect spend
        return upd

    @staticmethod
    def _agenda_text(ctx: GenContext) -> str:
        if not ctx.agenda:
            return ""
        items = "\n".join(f"  - [{d.id}] {d.direction} (why: {d.rationale})"
                          for d in ctx.agenda)
        return ("\nOpen research agenda (prefer the TOP item — it is the highest-"
                "leverage next step distilled from prior rounds):\n" + items)

    def _lessons(self) -> str:
        return lessons.summary(self.target.name)

    def _prompt(self, ctx: GenContext, lens=("", "")) -> str:
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
        # Inject the spec's EXACT build/test commands + the benchmark contract so the
        # agent doesn't have to guess them in an unfamiliar repo.
        spec = self.target.spec
        b = spec.bench
        objs = ", ".join(
            f"{o.get('metric')} ({'minimize' if o.get('minimize', True) else 'maximize'})"
            for o in spec.objectives) or b.get("metric", "latency")
        contract = (
            "What the judge measures (you do NOT run it): a microbench example "
            f"`{b.get('example')}` in package `{b.get('pkg')}` reports {objs}; the judge's "
            "paired A/B compares your change against the frozen baseline, and a random-input "
            "differential requires byte-identical output — so keep behaviour byte-identical.")
        lens_block = _lens_text(lens) if lens and lens[0] else ""
        return prompts.load("agentic", prior=prior, plan=plan,
                            region_hint=ctx.region_hint or "",
                            agenda=self._agenda_text(ctx), lessons=self._lessons(),
                            build_command=" ".join(spec.build),
                            test_command=" ".join(spec.test),
                            benchmark_contract=contract,
                            constraints=_constraints_text(spec),
                            lens=lens_block)

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
