"""aro sweep — the frontier-map meta-loop (deterministic, terminating core).

Profile a workload, rank the hot functions, bucket each by OWNER (our crate vs an
external crate / crypto) and by what the cross-run lessons already recorded, and
emit a FRONTIER MAP: where the time goes, what is our lever vs untouchable, what has
been tried (and the judge's verdict), and the actionable frontier — the untried
in-crate functions, heaviest first.

This is the terminating, deterministic skeleton. Per-function OPTIMIZATION attempts
are the existing per-target loop (`aro run` / the autonomous protocol), which this
map surfaces and orders; an accepted change folds into the baseline, and re-running
the sweep re-profiles on top of it (compounding). The sweep terminates because the
hot-function set is finite — it converges to a map, it does not explore forever.

    python3 -m aro sweep <spec.json> [--out report.md] [--min-pct 1.5] [--top N]
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import spec as specmod
from .frontier import _workspace_tokens, bucket_functions, _lesson_index
from .symbols import _demangle_names
from .report_md import render_map, render_attempt_map
from .target import SpecTarget
from .types import Patch


# --- profiling (best-effort; the deterministic classification is what's tested) ---

def profile_ranked(spec, top: int = 40, our_token: str = "", extra_edits=None):
    """Build the spec's profile example in an isolated worktree, sample it, and return
    `[(name, pct, symbol)]` heaviest-first over the in-binary compute frames. Empty on
    any failure (the map then reports 'no profile').

    `extra_edits` (the cumulative accepted patch) is applied to the worktree before
    building, so a re-sweep inside `--attempt` re-profiles ON TOP OF the wins so far
    (the same compounding the per-run loop does) — best-effort: a failed apply falls
    back to the base profile rather than crashing the meta-loop."""
    target = SpecTarget(spec)
    work = target.make_worktree("sweep")
    try:
        b = spec.bench
        target.write_probe(work, b["pkg"], b["example"])
        if extra_edits:
            try:
                target.apply(Patch(edits=list(extra_edits)), work)
            except Exception:
                pass  # re-profile on top is best-effort; degrade to the base profile
        # build_example: symbol-rich env (env_for forces debug+no-strip), honors the
        # spec's cargo_args (feature-gated hot paths), and takes the executable path
        # from cargo's own artifact JSON (correct under custom target layouts).
        p = spec.profile
        binary = target.build_example(work)
        rows = _sample_with_symbols(binary, spin=p.get("spin_secs", 8),
                                    secs=p.get("sample_secs", 4), top=top,
                                    our_token=our_token)
        return rows
    except Exception:
        return []
    finally:
        target.remove_worktree(work)


def _sample_with_symbols(binary, spin, secs, top, our_token=""):
    """Like profile.top_functions but KEEPS the raw symbol (for owner classification) and
    extracts a reliable leaf function name (`_fn_name`, not the weak demangler). Sampling is
    cross-platform via profile._raw_samples (macOS `sample` / Linux `perf`)."""
    from . import profile as profmod
    binary = Path(binary)
    raw = profmod.spin_and_sample(binary, spin, secs)
    rows = [(sym, cnt) for sym, image, cnt in raw
            if not any(d in image for d in profmod._DROP_IMAGES)]
    total = sum(c for _, c in rows) or 1
    rows.sort(key=lambda r: r[1], reverse=True)
    bn = Path(binary).name
    top_rows = rows[:top]
    names = _demangle_names([s for s, _ in top_rows], our_token, bn)
    return [(names[i], 100.0 * c / total, s) for i, (s, c) in enumerate(top_rows)]



def cli(args) -> None:
    spec = specmod.load(args.spec)
    min_pct = args.min_pct
    top = args.top
    our_token = _workspace_tokens(SpecTarget(spec), spec.bench.get("pkg", spec.name))

    # L3: the unattended meta-loop. Walks the frontier, runs the full judge per
    # function, compounds accepts, re-profiles on top — overnight-scale; run it as
    # the foreground (harness-tracked) process, never a backgrounded subagent.
    if args.attempt:
        from .attempt import _finalize_run, attempt
        from .events import EventLog
        diverge = args.diverge
        # token-infinite infinite-flow defaults (design §8): the explorer (--diverge)
        # fans out per round, prescreens, walks the WHOLE frontier (exhaustive on), and
        # the budget is just a safety valve. The converge map keeps the lean single path.
        fanout = args.fanout if args.fanout is not None else (3 if diverge else 1)
        gen_conc = args.gen_concurrency
        exhaustive = args.exhaustive if args.exhaustive is not None else diverge
        prescreen = args.prescreen if args.prescreen is not None else (fanout > 1)
        # --critic turns on the SECOND judge (independent semantic reviewer) before the
        # serial deterministic judge: a reward-hack / gamed-bench / known-bad-pattern is
        # rejected (recorded + traceable) without spending the scarce serial bench.
        critic_fn = None
        if args.critic:
            from . import critic as criticmod
            critic_fn = criticmod.critique
        per_fn_dry = args.dry_rounds if args.dry_rounds is not None else (3 if diverge else 0)
        # L4a probe factory: on by default under --diverge (the infinite flow rescues
        # its noise-limited nodes), opt-in otherwise; --no-probe-factory disables.
        probe_factory = (args.probe_factory if args.probe_factory is not None
                         else diverge)
        max_attempts = (args.max_attempts if args.max_attempts is not None
                        else (10000 if diverge else 6))
        rounds_per_fn = (args.rounds_per_fn if args.rounds_per_fn is not None
                         else (4 if diverge else 2))
        max_tries = args.max_tries_per_fn
        suffix = "-diverge" if diverge else "-attempt"
        out_dir = Path(args.out_dir or f"./.aro-runs/{spec.name}{suffix}")
        out_dir.mkdir(parents=True, exist_ok=True)
        events = EventLog(out_dir / "events.jsonl", also_console=True)
        print(f"=== aro sweep --attempt{' --diverge' if diverge else ''}: {spec.name} ===")
        print(f"repo={spec.repo} baseline={spec.baseline_ref} policy="
              f"{'diverge (infinite-flow, run to exhaustion)' if diverge else 'converge (stop at map)'} "
              f"max_attempts={max_attempts} rounds_per_fn={rounds_per_fn}")
        print(f"infinite-flow: fanout={fanout} (parallel gen, cap {gen_conc}) · "
              f"prescreen={'on' if prescreen else 'off'} · "
              f"probe-factory={'on' if probe_factory else 'off'} · "
              f"critic={'on (2nd judge)' if critic_fn else 'off'} · "
              f"exhaustive={'on' if exhaustive else 'off'} · per_fn_dry={per_fn_dry or 'spec'} · "
              f"out_dir={out_dir}\nprofiling the frontier ...")
        akw = dict(max_attempts=max_attempts, rounds_per_fn=rounds_per_fn,
                   min_pct=min_pct, top=top, diverge=diverge,
                   max_tries_per_fn=max_tries, fanout=fanout,
                   gen_concurrency=gen_conc, exhaustive=exhaustive,
                   prescreen=prescreen, per_fn_dry_rounds=per_fn_dry,
                   critic=critic_fn, probe_factory=probe_factory)
        if args.workloads:
            from .attempt import campaign
            all_rows, wf_state = campaign(spec, out_dir=out_dir, events=events,
                                          workload_proposals=args.workloads, **akw)
            rows = [r for rs in all_rows.values() for r in rs]
            cumulative = []   # per-workload cumulative patches live in their own dirs
            print(f"campaign: {len(all_rows)} workload(s) · closure {wf_state}")
        else:
            rows, cumulative = attempt(spec, out_dir=out_dir, events=events, **akw)
        report = render_attempt_map(rows, spec.name, cumulative, max_attempts)
        if args.out:
            Path(args.out).write_text(report + "\n")
            print(f"attempt map → {args.out}")
        print("\n" + report)
        # --- closing step (§4.5): auto-generate the decision tree + chart PNG ------
        _finalize_run(out_dir, events)
        print(f"\ntruth source: {out_dir / 'events.jsonl'}  (verbatim run-log)")
        return

    print(f"=== aro sweep: {spec.name} ===\nprofiling (build + sample) ...")
    ranked = profile_ranked(spec, top=top, our_token=our_token)
    if not ranked:
        print("WARNING: no profile parsed (is the profile example spin-capable?) — "
              "emitting an empty map.")
    buckets = bucket_functions(ranked, our_token, _lesson_index(spec.name), min_pct)
    report = render_map(buckets, spec.name, spec.profile.get("example", spec.bench["example"]),
                        min_pct)

    if args.out:
        Path(args.out).write_text(report + "\n")
        print(f"frontier map → {args.out}")
    print("\n" + report)

