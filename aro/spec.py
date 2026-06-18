"""TargetSpec — a declarative description of an optimization target.

This is how ARO generalizes: a new repo is a new spec file (in `targets/`), not
new Python. The spec carries the seven-slot shape (build/test/bench/regions/
objectives/...) PLUS an explicit **goal** and **stop condition** — so the loop
knows what it is aiming at and when it is done, instead of running a fixed N
rounds.

JSON (Python 3.9-safe; no tomllib). Paths inside the spec (probes, prompts) are
resolved relative to the aro-py repo root.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent  # aro-py/


@dataclass
class Goal:
    metric: str
    direction: str = "minimize"          # minimize | maximize
    target: Optional[float] = None       # absolute target value; None = open-ended/best-effort


@dataclass
class Stop:
    max_rounds: int = 3                  # hard cap on rounds
    dry_rounds: int = 2                  # stop after this many consecutive non-accepts (diminishing returns)


@dataclass
class TargetSpec:
    name: str
    repo: Path
    baseline_ref: str
    build: list                          # command token list, e.g. ["cargo","build","--release","-p","<crate>"]
    test: list
    bench: dict                          # {probe, example, pkg, sample_prefix, metric}
    profile: dict                        # {example, spin_secs, sample_secs}
    regions: list
    context: dict                        # {file, anchors:[[kind,name],...]}
    objectives: list                     # [{metric, minimize}]
    goal: Goal
    stop: Stop
    prompts: dict                        # {agentic, hint, hint_blind}
    generator: str = "agentic"           # "agentic" (heavy, default) | "ralph" (thin)
    differential: dict = field(default_factory=dict)  # {probe,pkg,example,prefix}; empty → stub
    timeout: int = 1800   # per build/test/bench/probe subprocess (s) — guards hangs
    read_phase: bool = True
    blind: bool = False
    raw: dict = field(default_factory=dict)

    def probe_src(self) -> str:
        return (REPO_ROOT / self.bench["probe"]).read_text()

    def diff_probe_src(self) -> str:
        return (REPO_ROOT / self.differential["probe"]).read_text()


def load(path) -> TargetSpec:
    d = json.loads(Path(path).read_text())
    repo = Path(d["repo"]).expanduser().resolve()
    g = d.get("goal", {})
    s = d.get("stop", {})
    return TargetSpec(
        name=d["name"],
        repo=repo,
        baseline_ref=d.get("baseline_ref", "HEAD"),
        build=d["build"],
        test=d["test"],
        bench=d["bench"],
        profile=d.get("profile", {}),
        regions=d.get("regions", []),
        context=d.get("context", {}),
        objectives=d.get("objectives", []),
        goal=Goal(metric=g.get("metric", d["bench"]["metric"]),
                  direction=g.get("direction", "minimize"),
                  target=g.get("target")),
        stop=Stop(max_rounds=s.get("max_rounds", 3),
                  dry_rounds=s.get("dry_rounds", 2)),
        prompts=d.get("prompts", {"agentic": "agentic", "hint": "hint",
                                  "hint_blind": "hint_blind"}),
        generator=d.get("generator", "agentic"),
        differential=d.get("differential", {}),
        timeout=d.get("timeout", 1800),
        read_phase=d.get("read_phase", True),
        blind=d.get("blind", False),
        raw=d,
    )
