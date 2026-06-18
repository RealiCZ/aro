"""Prompt / hint templates, loaded from `skill/prompts/*.md` — NOT hardcoded.

These are *executed* templates (code feeds them to `claude`), kept separate from
`skill/references/*.md`, which are prose docs you read to understand the system.

Externalizing the prompts makes "what we tell the model" an auditable, swappable
artifact. Two payoffs:
  - integrity: an answer-guided hint vs a profiler-only `*_blind` hint
    are two files you can diff and switch, so a clean blind run is one flag away;
  - generality / self-maintenance: a target's prompts become files its spec points
    at, not strings buried in Python.

Templates use `$name` placeholders (string.Template, safe_substitute — literal
`{` braces in code examples are left untouched, unlike str.format)."""
from __future__ import annotations

from pathlib import Path
from string import Template

_DIR = Path(__file__).parent.parent / "skill" / "prompts"


def load(name: str, **kw) -> str:
    text = (_DIR / f"{name}.md").read_text()
    return Template(text).safe_substitute(**kw).rstrip() + "\n"
