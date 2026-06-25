# Optimization examples — what is a BAD optimization even when it is faster

The judge proves two things: **byte-identical behaviour** and **a real speedup**. It does
**not** — and cannot — measure whether the change keeps the code easy to *read* and *change*.
A human reviewer weighs that, and **will reject a faster change that worsens it.** So
"byte-identical + faster" is *necessary, not sufficient*. Before proposing a change, run it
past the maintainability filter below. When a speedup can only be had by paying a structural
cost, **prefer the variant that keeps the structure — or don't make the change.**

This file is the catalogue the generator/planner prompts distill. Every entry is a real
pattern; the anchor case is a genuine reviewer rejection.

---

## The maintainability filter (ask these BEFORE proposing)

1. **Layer / cross-cutting invariant** — does the file or module document a uniform pattern
   ("every X-opcode routes its gas charge through `storage_gas_ext`", "all errors go through
   `Error::from`")? Does my change make ONE case the **sole exception** to it, or **delete a
   layer** that the pattern relies on? If yes → almost always not worth it.
2. **Single responsibility** — after my change, does a function now do a SECOND unrelated job
   (limit-tracking *and* gas-pricing) just to reuse a value it already had? That conflation is
   a cost a reader pays forever.
3. **Discoverability** — would a reader asking a basic question ("where is SSTORE storage gas
   charged?") now have to **discover a special case**? Did I have to **edit a doc / convention
   table** to call out my exception? Editing the table to explain a hack is a red flag, not a
   fix.
4. **Magnitude vs cost** — what does the win actually buy? A few **in-memory HashMap probes on
   a warm path** (no I/O saved), with **no dedicated benchmark isolating the effect**, is a
   *small, unproven* win. Small wins do not justify structural costs; only a large, *measured,
   IO- or algorithm-level* win can even be weighed against one.
5. **Provably-dead?** — if I'm deleting "dead"/"redundant" code, can I PROVE it's dead (name
   the invariant, trace every mutator), or am I assuming? Deleting code that wasn't actually
   dead is both a correctness risk and, if it regresses, pure loss.

If a change trips 1–3, look for the **layer-preserving variant**: thread the already-computed
value *through the existing interface* instead of dissolving the interface.

---

## Anchor case — PR #313, the rejected SSTORE inlining (this is why we did NOT do it)

**The change (faster, byte-identical, still rejected):** eliminate the redundant second
`inspect_storage` per SSTORE by **inlining `storage_gas_ext::sstore`'s charge into
`additional_limit_ext::sstore` and deleting `storage_gas_ext::sstore`**.

**Why the reviewer rejected it (verbatim reasoning):**
- `instructions.rs` documents a convention table: *every* storage-gas-charging opcode — LOG,
  CALL, CREATE, SELFDESTRUCT — routes its storage-gas charge through `storage_gas_ext`. The
  change makes **SSTORE the sole exception**, and the convention table even had to be **edited
  to call out the special case**.
- `additional_limit_ext::sstore` now **conflates limit-tracking with storage-gas pricing**
  (two responsibilities in one place).
- Anyone asking *"where is SSTORE storage gas charged?"* must now **discover the special case**.
- The payoff: removing a second `inspect_storage` that **hits the warm path and short-circuits
  before any DB access — ~5-6 in-memory HashMap probes per SSTORE, no I/O saved, and no
  dedicated benchmark isolating the effect.** → *"That's not worth giving up a clean
  cross-cutting invariant for."*

**The accepted alternative (same win, structure intact):**
> "If avoiding the redundant inspection is genuinely worth it, **thread the already-loaded slot
> down into `storage_gas_ext::sstore`** rather than dissolving the layer."

**Lesson:** the redundant-load elimination was a *good idea*; **dissolving the layer to get it
was the bad practice.** When the same win is reachable by passing the loaded value through the
existing boundary, take that path. A speedup that forces a special-case exception to a
documented, uniform convention is a should-not-merge change even when the judge says
"accepted".

---

## General anti-patterns (faster, but bad practice)

| Anti-pattern | Smell | Better |
|---|---|---|
| **Dissolve a layer for a micro-win** | you delete a function that's one of a uniform set, or inline across a module boundary | thread the already-computed value through the existing interface; keep the layer |
| **Special-case a documented convention** | you had to edit a convention table / comment to explain why one case is different | keep the case on the common path; if it truly can't be, that's a design discussion, not a silent inlining |
| **Conflate two responsibilities** | a function named for X now also does Y to reuse a value | pass the value in; let each function keep its one job |
| **Hurt discoverability** | a basic "where does X happen?" now needs a reader to find a special case | keep the obvious place obvious |
| **Delete "dead" code on a hunch** | "this guard is always false / this block is unreachable" without a traced invariant | prove it (name + check every mutator) and pin with `debug_assert!`, or leave it |
| **Copy-paste a library internal to skip a call** | you reproduce `revm`'s/`std`'s function body inline to save a call | only if the call is a *measured* hotspot AND you pin the equivalence; otherwise the duplicated body silently rots when upstream changes |
| **Trade clarity for an unmeasured win** | the change makes the code harder to follow and no benchmark isolates the gain | don't — an unmeasured micro-win is worth ~nothing against a real readability cost |

---

## How to act on this (generation + planning)

- **Prefer** changes that are byte-identical, faster, **and** leave layering / responsibilities /
  discoverability intact. That is a clean win.
- When the only speedup available trips the filter, **propose the layer-preserving variant**
  (pass the value through, don't dissolve the boundary). If even that isn't possible, prefer a
  different lever on the hot path.
- If you still believe a structure-trading change is worth it, **say so explicitly in your
  SUMMARY** — name the layer/convention it breaks and why the win justifies it — so it surfaces
  as a *should-not-merge (relaxed)* change for a human to weigh, NOT as a clean accept. Never
  present a maintainability regression as a free win.
