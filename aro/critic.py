"""aro critic — the SECOND judge: an independent semantic reviewer.

The deterministic judge (`eval.py`) proves the NUMBERS — faster + correct. It cannot
judge whether a bench is gamed, whether code is a reward-hack, or whether the 思路 is
sound. This module adds that: an independent adversarial LLM reviewer that critiques
each new artifact (plan / bench / code) and must PASS it. **Two judges, AND not OR** —
a candidate proceeds only if BOTH the critic and the deterministic judge pass it.

Invariants (the moat, extended to the semantic layer):
- **maker-checker**: the critic is a SEPARATE `claude` call from the generator, and is
  prompted to be skeptical (default-reject on doubt / unparseable output).
- **runs before the serial judge**: critique is a (parallelisable) LLM call; only
  critic-passed candidates enter the scarce serial A/A+A/B bench — so it doubles as a
  smart prescreen and saves judge throughput.
- **can't launder the numbers**: the critic gates, it never edits a Δ/verdict.
- **verbatim recording**: every verdict + structured reasons are returned for the caller
  to record to events.jsonl, so each leaf shows a traceable why-chain (audit trail).

Single reviewer now; `n>1` is a hook (call N times, take majority; ties → reject).
The LLM call is injectable (`runner=`) so the gate logic is unit-testable without a model.
"""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess

from . import prompts

# the three artifact kinds the critic reviews → its rubric prompt template
_RUBRIC = {"plan": "critic-plan", "bench": "critic-bench", "code": "critic-code"}
_VERDICTS = ("pass", "pass-risk", "reject")


@dataclasses.dataclass
class Reason:
    """One structured finding (kept structured, NOT free text, so the tree can show it
    and a human can cite it verbatim when adding a counter-example)."""
    rubric: str          # which rubric item fired, e.g. "layer-dissolve" / "reward-hack"
    finding: str         # the concrete finding
    severity: str = "low"  # none | low | high
    example: str = ""    # a matched known-bad example (e.g. "PR#313"), if any


@dataclasses.dataclass
class Critique:
    verdict: str         # pass | pass-risk | reject
    reasons: list        # [Reason]
    raw: str = ""        # the model's raw answer (kept for audit / debugging)
    tokens: int = 0      # the reviewer's output tokens (feeds the cumulative-token chart)

    @property
    def passed(self) -> bool:
        # pass-risk still PASSES the gate (the risk is flagged + recorded for the human),
        # only an outright reject stops the candidate before the serial judge.
        return self.verdict in ("pass", "pass-risk")

    def as_event(self) -> dict:
        return {"verdict": self.verdict,
                "reasons": [dataclasses.asdict(r) for r in self.reasons]}


def critique(kind: str, artifact: str, context: str = "", *, n: int = 1,
             runner=None) -> Critique:
    """Critique an `artifact` of `kind` ∈ {plan, bench, code}. Returns a `Critique`.

    `runner(prompt) -> str` is the LLM call (injected for tests; defaults to a read-only
    `claude -p`). `n>1` runs N independent reviewers and takes the majority (the multi-
    agent hook — single reviewer is enough for now)."""
    if kind not in _RUBRIC:
        raise ValueError(f"unknown critique kind: {kind!r}")
    runner = runner or _claude_runner
    votes = [_one_critique(kind, artifact, context, runner) for _ in range(max(1, n))]
    return _aggregate(votes)


def _one_critique(kind, artifact, context, runner) -> Critique:
    prompt = prompts.load(_RUBRIC[kind], artifact=artifact, context=context or "(none)")
    try:
        res = runner(prompt)
    except Exception as e:
        # the reviewer is the checker — if it can't run, default to REJECT (skeptical),
        # never silently pass an un-reviewed artifact.
        return Critique("reject", [Reason("critic-unavailable", str(e)[:160], "high")], "")
    # a runner may return the raw text, or (text, output_tokens) when it tracks spend.
    raw, toks = res if isinstance(res, tuple) else (res, 0)
    c = _parse(raw)
    c.tokens = toks
    return c


def _parse(raw: str) -> Critique:
    """Parse the reviewer's answer → Critique. Expected JSON:
    {"verdict": "pass|pass-risk|reject", "reasons": [{"rubric","finding","severity","example"}]}.
    Default-REJECT when there's no parseable verdict (an un-gradeable review is not a pass)."""
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        return Critique("reject", [Reason("unparseable", "no JSON verdict in review", "high")], raw)
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return Critique("reject", [Reason("unparseable", "review JSON did not parse", "high")], raw)
    verdict = str(obj.get("verdict", "")).strip().lower()
    if verdict not in _VERDICTS:
        return Critique("reject", [Reason("unparseable", f"verdict {verdict!r} not in {_VERDICTS}", "high")], raw)
    reasons = []
    for r in obj.get("reasons", []):
        if not isinstance(r, dict):
            continue
        reasons.append(Reason(rubric=str(r.get("rubric", "") or "")[:60],
                              finding=str(r.get("finding", "") or "")[:400],
                              severity=str(r.get("severity", "low") or "low"),
                              example=str(r.get("example", "") or "")[:60]))
    return Critique(verdict, reasons, raw)


def _aggregate(votes: list) -> Critique:
    """N-vote aggregation (n=1 → the single vote). Adversarial: a `reject` majority (or
    tie) rejects; otherwise the worst surviving verdict wins (pass-risk over pass). All
    reasons are unioned so the human sees every objection."""
    if len(votes) == 1:
        return votes[0]
    toks = sum(getattr(v, "tokens", 0) or 0 for v in votes)
    rejects = sum(1 for v in votes if v.verdict == "reject")
    reasons = [r for v in votes for r in v.reasons]
    if rejects * 2 >= len(votes):                      # tie → reject (skeptical)
        c = Critique("reject", reasons, "")
    else:
        verdict = "pass-risk" if any(v.verdict == "pass-risk" for v in votes) else "pass"
        c = Critique(verdict, reasons, "")
    c.tokens = toks
    return c


def _claude_runner(prompt: str):
    """Default reviewer: a read-only `claude -p` (no --dangerously-skip-permissions — the
    critic only READS and returns a verdict; it never edits). --output-format json so the
    review's token spend is captured. Returns (result_text, output_tokens)."""
    out = subprocess.run(["claude", "--output-format", "json", "-p", prompt],
                         capture_output=True, text=True, timeout=600)
    if out.returncode != 0:
        raise RuntimeError(f"critic claude exited {out.returncode}")
    from .generator import claude_json
    text, toks, _ = claude_json(out.stdout)
    return (text, toks)
