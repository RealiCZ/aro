"""ARO — Auto-Research Optimizer (Python port).

A memory-driven, goal-directed optimization loop for code. Generation is thin (an
agentic write-compile-fix `claude` loop, or a seeded driver for tests); the
engineering weight is in the **judge** (`eval`): a separate evaluator —
reward-hacking guard, then correctness (build + test + regression + differential
vs a frozen baseline), then significance (paired A/B against an A/A-calibrated
noise floor + bootstrap CI). Generality is via a spec (`targets/*.json`); each
round's verdicts feed a forward-looking agenda (reflect). See the design doc.
"""

__all__ = [
    "types", "stats", "guard", "spec", "target", "eval", "store",
    "generator", "engine", "profile", "context", "prompts", "events",
]
