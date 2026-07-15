"""chart: the run's SVG figures, stdlib-only (no matplotlib).

Two renderers remain after the P5 cleanup removed the stitched-trajectory stack:

- `explore_svg()`: the explorer's live figure (realized vs addressable headroom
  per attempt, floor line, decision cap), written as trajectory.svg each step.
- `perf_token_svg()`: the headline figure (running-best speedup vs cumulative
  LLM output tokens, every candidate placed, off-spec marks, the Amdahl ceiling).

Plus `svg_to_png()`, a best-effort rasterizer for embedding in markdown. All
figures read events verbatim; verdicts are never re-judged here.
"""
from __future__ import annotations

from pathlib import Path


# Distinct hues per policy (cycled); regime decides solid vs dashed.
_COLORS = ["#2563eb", "#ea580c", "#059669", "#7c3aed"]




def explore_svg(elog, floor_pct: float, decision: str, reason: str,
                spec_name: str) -> str:
    """The explorer figure: realized (climbing) vs addressable headroom (
    shrinking), with the floor as context and the continue/stop verdict. When the two
    lines meet near zero headroom, the search is at its limit — and says so."""
    W, H = 900, 525
    x0, y0, x1, y1 = 70, 56, 860, 400
    realized = [-e["realized_cum"] for e in elog]      # % faster (positive up)
    head = [e["headroom"] for e in elog]
    ymax = max(5.0, max(realized + head + [0.0]) * 1.2)
    xmax = max(4, len(elog))

    def Xc(i):
        return x0 + (i / xmax) * (x1 - x0)

    def Yc(v):
        return y1 - (v / ymax) * (y1 - y0)

    L = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
         f'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">']
    L.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
    L.append(f'<text x="{W/2}" y="26" text-anchor="middle" font-size="17" '
             f'font-weight="700" fill="#0f172a">aro explore — {_esc(spec_name)}: '
             f'realized vs addressable headroom</text>')
    L.append(f'<text x="{W/2}" y="44" text-anchor="middle" font-size="11.5" '
             f'fill="#64748b">untouchable floor ≈ {floor_pct:.0f}% (not-ours) · '
             f'step {len(elog)}</text>')

    for k in range(6):
        v = ymax * k / 5
        yy = Yc(v)
        L.append(f'<line x1="{x0}" y1="{yy:.1f}" x2="{x1}" y2="{yy:.1f}" '
                 f'stroke="#eef2f7"/>')
        L.append(f'<text x="{x0-8}" y="{yy+4:.1f}" text-anchor="end" font-size="11" '
                 f'fill="#94a3b8">{v:.0f}%</text>')
    for i in range(xmax + 1):
        L.append(f'<text x="{Xc(i):.1f}" y="{y1+18}" text-anchor="middle" '
                 f'font-size="11" fill="#64748b">{i}</text>')
    L.append(f'<line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y1}" stroke="#334155"/>')
    L.append(f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="#334155"/>')
    L.append(f'<text x="{(x0+x1)/2:.0f}" y="{H-8}" text-anchor="middle" font-size="12" '
             f'fill="#334155">attempt #</text>')
    L.append(f'<text x="22" y="{(y0+y1)/2:.0f}" font-size="12" fill="#334155" '
             f'transform="rotate(-90 22 {(y0+y1)/2:.0f})" text-anchor="middle">'
             f'% of total run time</text>')

    # realized — staircase, climbs only on accept
    rp = [(Xc(0), Yc(0.0))]
    prev = 0.0
    for e, r in zip(elog, realized):
        rp.append((Xc(e["i"]), Yc(prev)))
        rp.append((Xc(e["i"]), Yc(r)))
        prev = r
    L.append(f'<polyline points="{_pts(rp)}" fill="none" stroke="#2563eb" '
             f'stroke-width="2.5"/>')
    for e, r in zip(elog, realized):
        if not e["accepted"]:
            continue
        cx, cy = Xc(e["i"]), Yc(r)
        relaxed = bool(e.get("regime")) and e["regime"] != "byte-identical"
        d = f"{e['delta']:+.1f}%" if isinstance(e.get("delta"), (int, float)) else ""
        if relaxed:
            # a relaxed (should-not-merge) win is a DIFFERENT kind of claim — hollow dot
            L.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="#ffffff" '
                     f'stroke="#ea580c" stroke-width="2.5"/>')
            L.append(f'<text x="{cx+8:.1f}" y="{cy-7:.1f}" font-size="10.5" '
                     f'fill="#ea580c">{_esc(e["fn"])} {d} · relaxed · needs human call</text>')
        else:
            L.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" fill="#2563eb"/>')
            L.append(f'<text x="{cx+7:.1f}" y="{cy-7:.1f}" font-size="10.5" '
                     f'fill="#2563eb">{_esc(e["fn"])} {d}</text>')
    if elog:  # inline tag on the realized line itself
        L.append(f'<text x="{x1-6:.1f}" y="{Yc(realized[-1])-9:.1f}" text-anchor="end" '
                 f'font-size="11.5" font-weight="700" fill="#2563eb">↑ realized</text>')

    # headroom — drawn PER SEGMENT, each drop colored by WHY it fell, so a reader sees
    # that a drop is not "captured": a win shrank it = green solid;
    # tried with no provable win (ruled out at this power) = red dashed;
    # no drop (flat / a re-profile surfaced new fns) = neutral orange dashed.
    GREEN, RED, ORANGE = "#059669", "#dc2626", "#ea580c"
    hp = [(Xc(0), Yc(max(head[0] if head else 0.0, 0.0)))]
    for e, h in zip(elog, head):
        hp.append((Xc(e["i"]), Yc(h)))
    for j in range(len(hp) - 1):
        (xa, ya), (xb, yb) = hp[j], hp[j + 1]
        cause = elog[j] if 1 <= j <= len(elog) - 1 else None
        drop = yb > ya + 1.0                        # headroom fell (larger y = lower)
        if drop and cause is not None and cause["accepted"]:
            col, dash, mark = GREEN, "", "✓ captured"
        elif drop and cause is not None:
            col, dash, mark = RED, ' stroke-dasharray="5,3"', "✗ ruled out"
        else:
            col, dash, mark = ORANGE, ' stroke-dasharray="7,4"', None
        L.append(f'<line x1="{xa:.1f}" y1="{ya:.1f}" x2="{xb:.1f}" y2="{yb:.1f}" '
                 f'stroke="{col}" stroke-width="2.5"{dash}/>')
        if mark and cause is not None:
            L.append(f'<text x="{(xa+xb)/2:.1f}" y="{(ya+yb)/2-5:.1f}" text-anchor="middle" '
                     f'font-size="9.5" fill="{col}">{mark} {_esc(cause["fn"])}</text>')
    if elog:  # inline tag on the headroom line itself
        L.append(f'<text x="{x1-6:.1f}" y="{Yc(head[-1])+17:.1f}" text-anchor="end" '
                 f'font-size="11.5" font-weight="700" fill="#ea580c">↓ headroom left</text>')

    # legend + verdict
    L.append(f'<line x1="{x0+14}" y1="{y0+12}" x2="{x0+36}" y2="{y0+12}" '
             f'stroke="#2563eb" stroke-width="2.5"/>')
    L.append(f'<text x="{x0+42}" y="{y0+16}" font-size="11.5" fill="#0f172a">'
             f'realized: banked speedup, higher is faster ↑</text>')
    L.append(f'<line x1="{x0+14}" y1="{y0+30}" x2="{x0+36}" y2="{y0+30}" '
             f'stroke="#ea580c" stroke-width="2.5" stroke-dasharray="7,4"/>')
    L.append(f'<text x="{x0+42}" y="{y0+34}" font-size="11.5" fill="#0f172a">'
             f'addressable headroom: still optimizable, lower is less ↓</text>')
    vcol = "#dc2626" if decision == "STOP" else "#059669"
    L.append(f'<rect x="{x0}" y="{y1+30}" width="{x1-x0}" height="44" rx="6" '
             f'fill="{vcol}" opacity="0.08"/>')
    L.append(f'<text x="{x0+12}" y="{y1+49}" font-size="13" font-weight="700" '
             f'fill="{vcol}">decision {decision}</text>')
    L.append(f'<text x="{x0+12}" y="{y1+66}" font-size="11" fill="#334155">'
             f'{_esc(reason)}</text>')
    L.append(f'<text x="{x0+12}" y="{y1+94}" font-size="11" fill="#64748b">'
             f'headroom drops: <tspan fill="#059669" font-weight="600">✓ win (captured)</tspan>  '
             f'<tspan fill="#dc2626" font-weight="600">✗ tried, no win (ruled out)</tspan> '
             f'<tspan fill="#94a3b8">(ruled out = the time is still there; not extractable at this lens / measurement power)</tspan></text>')
    L.append("</svg>")
    return "\n".join(L)


def _pts(pairs) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in pairs)


# --- perf-vs-cumulative-token trajectory (the "TFLOPS vs token" style figure) --------

def _perf_data(events, minimize: bool = True) -> dict:
    """Walk events.jsonl chronologically → the data the perf/token figure needs:
    cumulative LLM output tokens (X), the running-best % faster (compounded accepts),
    every candidate placed at its WOULD-BE speedup (current cumulative ∘ its marginal Δ),
    off-spec (rejected / build-failed) marks, and the untouchable floor (Amdahl ceiling).

    X is cumulative `output_tokens` (gen + read + reflect + critic). When a run recorded
    no tokens (older run), `have_tokens` is False and the caller falls back to candidate #."""
    cum_tok = 0.0
    have_tokens = False
    cands, steps = [], [{"x": 0.0, "realized": 0.0}]
    factor = 1.0                 # cumulative time factor (Π of FOLDED (1+Δ/100))
    floor_pct = 0.0
    cur_fn, cur_regime = "", ""
    pending: dict = {}
    idx = 0
    # An ACCEPTED verdict is a measured win on the frozen base; the running-best may only
    # compound what was actually FOLDED into the baseline (a superseded sibling didn't).
    # Trust baseline_advanced when present; older logs without it fold every accept.
    folded_ids = {e.get("by") for e in events if e.get("event") == "baseline_advanced"}
    use_folded = bool(folded_ids)
    for e in events:
        ev = e.get("event")
        t = e.get("tokens")
        if isinstance(t, (int, float)) and t > 0:
            cum_tok += t
            have_tokens = True
        if ev == "attempt_started":
            cur_fn, cur_regime = e.get("fn", ""), e.get("regime", "")
        elif ev == "candidate_proposed":
            pending[e.get("id")] = {"fn": cur_fn, "lens": e.get("lens"), "regime": cur_regime}
        elif ev == "candidate_verdict":
            idx += 1
            meta = pending.pop(e.get("id"), {})
            ds = e.get("deltas") or []
            d0 = ds[0].get("delta_pct") if ds and isinstance(ds[0], dict) else None
            verdict = e.get("verdict")
            accepted = verdict in ("accepted", "accepted-ir")
            # folded = the win was actually compounded (not superseded by a better sibling)
            folded = accepted and (not use_folded or e.get("id") in folded_ids)
            # the candidate's WOULD-BE absolute speedup = current cumulative ∘ its marginal Δ
            wb = None
            if isinstance(d0, (int, float)):
                wb_factor = factor * (1 + d0 / 100.0)
                wb = (1 - wb_factor) * 100.0          # % faster vs the ORIGINAL baseline
            c = {"x": cum_tok, "idx": idx, "delta": d0, "verdict": verdict,
                 "accepted": accepted, "folded": folded, "wouldbe": wb,
                 "fn": meta.get("fn", cur_fn), "lens": meta.get("lens"),
                 "regime": meta.get("regime", cur_regime)}
            cands.append(c)
            if folded and isinstance(d0, (int, float)):
                factor *= (1 + d0 / 100.0)
                steps.append({"x": cum_tok, "realized": (1 - factor) * 100.0,
                              "fn": c["fn"], "lens": c["lens"], "delta": d0,
                              "regime": c["regime"]})
        elif ev == "explore_step":
            if e.get("floor_pct"):
                floor_pct = e["floor_pct"]
        elif ev == "profile_floor" and not floor_pct:
            floor_pct = sum(f.get("pct", 0) for f in e.get("frames", []) if isinstance(f, dict))
    realized = (1 - factor) * 100.0
    ceiling = max(0.0, 100.0 - floor_pct)   # Amdahl: drive everything-but-the-floor to 0
    return {"have_tokens": have_tokens, "cum_tok": cum_tok, "cands": cands,
            "steps": steps, "realized": realized, "floor_pct": floor_pct,
            "ceiling": ceiling, "n": idx}


# verdict → dot style for the scatter (accepted is drawn as the staircase node separately)
_DOT = {
    "within-noise": ("#A9B6C2", "tried · within noise"),
    "noise-limited": ("#CBA255", "noise-limited (directional)"),
    "regressed": ("#DD9580", "regressed"),
    # defensive completeness: retroactive/backfill verdict live per-run consumers normally never see
    "refuted-by-icount": ("#A9B6C2", "refuted by Ir / CodSpeed"),
    "neutral-ir": ("#A9B6C2", "neutral Ir (compiler already)"),
    "TERMINAL_UNTOUCHED": ("#A9B6C2", "terminal Ir untouched (block PR)"),
    "TERMINAL_REGRESSED": ("#DD9580", "terminal Ir regressed"),
    "TERMINAL_MIXED": ("#CBA255", "terminal Ir mixed"),
    "TERMINAL_CONFIRMED": ("#6A9F6A", "terminal Ir confirmed"),
    "TERMINAL_TEST_FAILED": ("#DD9580", "terminal full-suite test failed"),
}
_OFFSPEC = {"rejected", "build-failed", "verify-failed"}


def perf_token_svg(events, spec_name: str = "", minimize: bool = True) -> str:
    """The decisive figure, image-style: running-best % faster (staircase) over cumulative
    LLM output tokens, every candidate as a dot at its would-be speedup, rejected/
    build-failed as off-spec ×, a baseline line at 0 and the untouchable-floor Amdahl
    ceiling. Pure stdlib SVG. Falls back to candidate # on X when no tokens were recorded."""
    d = _perf_data(events, minimize)
    cands, steps = d["cands"], d["steps"]
    W, H = 980, 540
    x0, y0, x1, y1 = 78, 56, 858, 430

    xs = [c["x"] for c in cands] + [s["x"] for s in steps]
    if d["have_tokens"]:
        xmax = max(xs + [1.0]); xlabel = "cumulative output tokens"
        xfmt = lambda v: (f"{v/1000:.0f}k" if v >= 1000 else f"{v:.0f}")
    else:
        # no token data → X = candidate ordinal (still a faithful effort axis, just coarser)
        xmax = max(d["n"], 1); xlabel = "candidate #  (no token data — older run)"
        xfmt = lambda v: f"{v:.0f}"
        for i, c in enumerate(cands, 1):
            c["x"] = i
        # re-place the staircase nodes at the ordinal of their folded candidate
        acc_idx = [c["idx"] for c in cands if c.get("folded")]
        steps = [{"x": 0.0, "realized": 0.0}] + [
            {**s, "x": acc_idx[k]} for k, s in enumerate(steps[1:])] if acc_idx else steps

    ys = [c["wouldbe"] for c in cands if isinstance(c.get("wouldbe"), (int, float))]
    _top = max(ys + [s["realized"] for s in steps] + [d["ceiling"], 5.0])
    ymax = max(5.0, _top * 1.18)
    # let regressions (negative would-be) show inside the plot, but cap the band so one
    # wild regression can't squash the rest.
    ymin = max(min([0.0] + ys) * 1.1, -0.42 * ymax)

    def X(v):
        return x0 + (v / xmax) * (x1 - x0) if xmax else x0

    def Y(v):
        return y1 - ((v - ymin) / (ymax - ymin)) * (y1 - y0)

    L = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
         f'font-family="IBM Plex Mono,ui-monospace,Menlo,monospace">']
    L.append('<defs><filter id="glow" x="-20%" y="-20%" width="140%" height="140%">'
             '<feGaussianBlur stdDeviation="2.4" result="b"/><feMerge>'
             '<feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
             '<linearGradient id="ph" x1="0" y1="0" x2="0" y2="1">'
             '<stop offset="0" stop-color="#0E9F8C" stop-opacity=".14"/>'
             '<stop offset="1" stop-color="#0E9F8C" stop-opacity="0"/></linearGradient></defs>')
    L.append(f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>')
    L.append(f'<text x="{W/2}" y="28" text-anchor="middle" font-size="15" font-weight="600" '
             f'font-family="Space Grotesk,system-ui" fill="#1B2530">{_esc(spec_name)} · speedup % vs cumulative tokens</text>')

    # Y grid + labels (span ymin..ymax)
    for k in range(6):
        v = ymin + (ymax - ymin) * k / 5
        yy = Y(v)
        L.append(f'<line x1="{x0}" y1="{yy:.1f}" x2="{x1}" y2="{yy:.1f}" stroke="#EAEEF3"/>')
        L.append(f'<text x="{x0-8}" y="{yy+4:.1f}" text-anchor="end" font-size="11" '
                 f'fill="#8693A1">{v:.0f}%</text>')
    # X ticks — even sixths over tokens; integer ordinals in the candidate-# fallback
    if d["have_tokens"]:
        xticks = [xmax * k / 6 for k in range(7)]
    else:
        st = max(1, int(xmax) // 8)
        xticks = list(range(0, int(xmax) + 1, st))
    for v in xticks:
        xx = X(v)
        L.append(f'<line x1="{xx:.1f}" y1="{y1}" x2="{xx:.1f}" y2="{y1+5}" stroke="#C5CFDA"/>')
        L.append(f'<text x="{xx:.1f}" y="{y1+19}" text-anchor="middle" font-size="11" '
                 f'fill="#566472">{xfmt(v)}</text>')
    L.append(f'<line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y1}" stroke="#C5CFDA"/>')
    L.append(f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="#C5CFDA"/>')
    L.append(f'<text x="22" y="{(y0+y1)/2:.0f}" font-size="11.5" fill="#566472" '
             f'transform="rotate(-90 22 {(y0+y1)/2:.0f})" text-anchor="middle">speedup (% faster)</text>')
    L.append(f'<text x="{(x0+x1)/2:.0f}" y="{H-10}" text-anchor="middle" font-size="11.5" '
             f'fill="#566472">{_esc(xlabel)}</text>')

    # reference lines: baseline 0% + Amdahl floor-ceiling
    L.append(f'<line x1="{x0}" y1="{Y(0):.1f}" x2="{x1}" y2="{Y(0):.1f}" stroke="#D5DEE8" '
             f'stroke-width="1.4"/>')
    if d["ceiling"] <= ymax:
        cy = Y(d["ceiling"])
        L.append(f'<line x1="{x0}" y1="{cy:.1f}" x2="{x1}" y2="{cy:.1f}" stroke="#C5CFDA" '
                 f'stroke-width="1.4" stroke-dasharray="8,5"/>')
        L.append(f'<text x="{x1-4}" y="{cy-6:.1f}" text-anchor="end" font-size="11" '
                 f'fill="#8693A1">theoretical ceiling ~{d["ceiling"]:.0f}% (everything outside the {d["floor_pct"]:.0f}% untouchable floor)</text>')

    # candidate dots (incl. regressions + accepted-but-superseded); folded wins are the
    # staircase nodes drawn separately, so skip them here.
    for c in cands:
        if c.get("folded"):
            continue
        cx = X(c["x"])
        if c["verdict"] in _OFFSPEC:
            yy = Y(0.0)
            L.append(f'<path d="M{cx-3.5:.1f},{yy-3.5:.1f} l7,7 M{cx+3.5:.1f},{yy-3.5:.1f} l-7,7" '
                     f'stroke="#B7C2CE" stroke-width="1.6"/>')
        elif isinstance(c.get("wouldbe"), (int, float)):
            col = _DOT.get(c["verdict"], ("#A9B6C2", ""))[0]
            L.append(f'<circle cx="{cx:.1f}" cy="{Y(c["wouldbe"]):.1f}" r="3.6" fill="{col}"/>')

    # running-best staircase — phosphor trace with a glow + a filled area beneath it
    pts = [(X(steps[0]["x"]), Y(0.0))]
    prev = 0.0
    for s in steps[1:]:
        pts.append((X(s["x"]), Y(prev)))
        pts.append((X(s["x"]), Y(s["realized"])))
        prev = s["realized"]
    if len(pts) > 1:
        area = _pts(pts) + f' {pts[-1][0]:.1f},{Y(0):.1f} {pts[0][0]:.1f},{Y(0):.1f}'
        L.append(f'<polygon points="{area}" fill="url(#ph)"/>')
        L.append(f'<polyline points="{_pts(pts)}" fill="none" stroke="#0E9F8C" '
                 f'stroke-width="2.4"/>')
    for s in steps[1:]:
        cx, cy = X(s["x"]), Y(s["realized"])
        merge = not (s.get("regime") and s["regime"] != "byte-identical")  # byte-identical = mergeable
        L.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" '
                 f'fill="{"#0E9E72" if merge else "#FFFFFF"}" stroke="#0E9F8C" stroke-width="2"/>')
        lab = _esc(s.get("fn", ""))
        if s.get("lens"):
            lab += f' · {_esc(s["lens"].split("/")[0].split("-")[0])}'
        dd = f' {s["delta"]:+.1f}%' if isinstance(s.get("delta"), (int, float)) else ""
        L.append(f'<text x="{cx+8:.1f}" y="{cy-7:.1f}" font-size="10.5" '
                 f'fill="{"#0E9E72" if merge else "#566472"}">{lab}{dd}</text>')

    # final running-best tag
    if steps and len(steps) > 1:
        ex, ey = X(steps[-1]["x"]), Y(steps[-1]["realized"])
        L.append(f'<text x="{ex+8:.1f}" y="{ey+4:.1f}" font-size="12" font-weight="600" '
                 f'font-family="Space Grotesk,system-ui" fill="#0E9F8C">running best · {d["realized"]:.1f}%↑</text>')

    # legend (top-left; staircase starts low so that corner is free)
    lx, ly = x0 + 14, y0 + 12
    leg = [("line", "#0E9F8C", "running best (compounded accepts)"),
           ("dot", "#8693A1", f"candidates (incl. regressions) · {d['n']}"),
           ("x", "#B7C2CE", "off-spec: apply/build/verify failed (not scored)"),
           ("dash", "#C5CFDA", "theoretical ceiling (outside the untouchable floor)")]
    # opaque backing so the ceiling dashed line / gridlines don't bleed through the text
    L.append(f'<rect x="{lx-9}" y="{ly-12}" width="306" height="{len(leg)*17+8}" rx="3" '
             f'fill="#FFFFFF" opacity="0.94" stroke="#E2E8F0"/>')
    for i, (kind, col, txt) in enumerate(leg):
        yy = ly + i * 17
        if kind == "line":
            L.append(f'<line x1="{lx}" y1="{yy}" x2="{lx+20}" y2="{yy}" stroke="{col}" stroke-width="2.6"/>')
        elif kind == "dot":
            L.append(f'<circle cx="{lx+10}" cy="{yy}" r="3.6" fill="{col}"/>')
        elif kind == "x":
            L.append(f'<path d="M{lx+6},{yy-3.5} l7,7 M{lx+13},{yy-3.5} l-7,7" stroke="{col}" stroke-width="1.6"/>')
        else:
            L.append(f'<line x1="{lx}" y1="{yy}" x2="{lx+20}" y2="{yy}" stroke="{col}" stroke-width="1.5" stroke-dasharray="8,5"/>')
        L.append(f'<text x="{lx+28}" y="{yy+4}" font-size="11" fill="#1B2530">{_esc(txt)}</text>')

    L.append("</svg>")
    return "\n".join(L)



def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))



# --- SVG -> PNG rasterizer (moved from sweep.py in the P3 split) ---------------

def svg_to_png(svg: Path, png: Path, size: int = 1400) -> bool:
    """Best-effort SVG -> PNG across platforms — macOS `qlmanage`, or `rsvg-convert` /
    `cairosvg` / `inkscape` on Linux. The SVG is the real artifact (the HTML embeds the SVG
    directly); the PNG is only a convenience for embedding in markdown. True on success."""
    import shutil
    import subprocess
    try:
        if shutil.which("qlmanage"):
            subprocess.run(["qlmanage", "-t", "-s", str(size), "-o", str(png.parent), str(svg)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            produced = png.parent / (svg.name + ".png")   # qlmanage names it <file>.png
            if produced.exists():
                produced.replace(png)
                return True
        if shutil.which("rsvg-convert"):
            subprocess.run(["rsvg-convert", "-w", str(size), "-o", str(png), str(svg)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            return png.exists()
        if shutil.which("cairosvg"):
            subprocess.run(["cairosvg", str(svg), "-o", str(png), "-W", str(size)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            return png.exists()
        if shutil.which("inkscape"):
            subprocess.run(["inkscape", str(svg), "--export-type=png",
                            f"--export-filename={png}", f"--export-width={size}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            return png.exists()
    except Exception:
        pass
    return False

