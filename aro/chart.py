"""chart — render search trajectories as an overlaid staircase plot.

The decisive autoresearch figure: cumulative speedup (% faster) vs attempt #, one
staircase per search policy. A convergent run STOPS (a ■ cap); a divergent run runs
to the budget (a → cap). Steps under a relaxed oracle are drawn dashed — a win there
is a different KIND of claim than a byte-identical one, so the eye must not blend them.

Two renderers, both stdlib-only (no matplotlib): `svg()` for the saved artifact,
`ascii_chart()` for an immediately-visible console view.

    python3 -m aro chart --series "convergent|byte-identical|converged|DIR1,DIR2" \\
                         [--series "divergent|...|budget|DIR3"] [--out chart.svg]
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import trajectory as trajmod

# Distinct hues per policy (cycled); regime decides solid vs dashed.
_COLORS = ["#2563eb", "#ea580c", "#059669", "#7c3aed"]


def _ymax(trajs) -> float:
    top = max((s.speedup_pct for t in trajs for s in t.steps), default=0.0)
    return max(5.0, top * 1.25)


def _xmax(trajs) -> int:
    return max(4, max((len(t.steps) for t in trajs), default=0))


def svg(trajs, title: str = "ARO search trajectory — cumulative speedup vs attempts") -> str:
    W, H = 880, 480
    x0, y0, x1, y1 = 70, 48, 840, 410        # plot box
    ymax, xmax = _ymax(trajs), _xmax(trajs)

    def X(i):
        return x0 + (i / xmax) * (x1 - x0)

    def Y(v):
        return y1 - (v / ymax) * (y1 - y0)

    L = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
         f'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">']
    L.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
    L.append(f'<text x="{W/2}" y="26" text-anchor="middle" font-size="17" '
             f'font-weight="700" fill="#0f172a">{_esc(title)}</text>')

    # Y gridlines + labels (% faster)
    for k in range(6):
        v = ymax * k / 5
        yy = Y(v)
        L.append(f'<line x1="{x0}" y1="{yy:.1f}" x2="{x1}" y2="{yy:.1f}" '
                 f'stroke="#e2e8f0" stroke-width="1"/>')
        L.append(f'<text x="{x0-8}" y="{yy+4:.1f}" text-anchor="end" font-size="11" '
                 f'fill="#64748b">{v:.0f}%</text>')
    # X ticks (attempts)
    for i in range(xmax + 1):
        xx = X(i)
        L.append(f'<line x1="{xx:.1f}" y1="{y1}" x2="{xx:.1f}" y2="{y1+5}" stroke="#94a3b8"/>')
        L.append(f'<text x="{xx:.1f}" y="{y1+19}" text-anchor="middle" font-size="11" '
                 f'fill="#64748b">{i}</text>')
    # axes
    L.append(f'<line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y1}" stroke="#334155" stroke-width="1.5"/>')
    L.append(f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="#334155" stroke-width="1.5"/>')
    L.append(f'<text x="18" y="{(y0+y1)/2:.0f}" font-size="12" fill="#334155" '
             f'transform="rotate(-90 18 {(y0+y1)/2:.0f})" text-anchor="middle">'
             f'cumulative speedup (% faster)</text>')
    L.append(f'<text x="{(x0+x1)/2:.0f}" y="{H-8}" font-size="12" fill="#334155" '
             f'text-anchor="middle">attempt #</text>')

    # trajectories
    for ti, t in enumerate(trajs):
        color = _COLORS[ti % len(_COLORS)]
        # staircase polyline points (compounding only changes y on accept)
        pts = [(X(0), Y(0.0))]
        prev = 0.0
        for s in t.steps:
            pts.append((X(s.i), Y(prev)))
            pts.append((X(s.i), Y(s.speedup_pct)))
            prev = s.speedup_pct
        # split into solid (byte-identical) / dashed (relaxed) by drawing per-segment
        # is overkill for v0; dash the whole line if ANY step is relaxed.
        dash = ' stroke-dasharray="7,4"' if any(s.regime != "byte-identical"
                                                for s in t.steps) else ""
        poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        L.append(f'<polyline points="{poly}" fill="none" stroke="{color}" '
                 f'stroke-width="2.5"{dash}/>')
        # accept dots + Δ labels
        for s in t.steps:
            if not s.accepted:
                continue
            cx, cy = X(s.i), Y(s.speedup_pct)
            L.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" fill="{color}"/>')
            d = f"{s.delta_pct:+.1f}%" if isinstance(s.delta_pct, (int, float)) else ""
            L.append(f'<text x="{cx+7:.1f}" y="{cy-7:.1f}" font-size="10.5" '
                     f'fill="{color}">{_esc(s.label)} {d}</text>')
        # end cap: converged (■) vs ran-to-budget (→)
        if t.steps:
            ex, ey = X(t.steps[-1].i), Y(t.steps[-1].speedup_pct)
            if t.converged:
                L.append(f'<rect x="{ex-4:.1f}" y="{ey-4:.1f}" width="8" height="8" '
                         f'fill="{color}"/>')
                L.append(f'<text x="{ex+10:.1f}" y="{ey+4:.1f}" font-size="11" '
                         f'font-weight="600" fill="{color}">converged (plateau)</text>')
            else:
                L.append(f'<text x="{ex+8:.1f}" y="{ey+4:.1f}" font-size="13" '
                         f'font-weight="700" fill="{color}">→ budget</text>')

    # legend (top-left of the plot — the staircase starts low, so that corner is free)
    lx, ly = x0 + 16, y0 + 14
    for ti, t in enumerate(trajs):
        color = _COLORS[ti % len(_COLORS)]
        yy = ly + ti * 18
        L.append(f'<line x1="{lx}" y1="{yy}" x2="{lx+22}" y2="{yy}" stroke="{color}" '
                 f'stroke-width="2.5"/>')
        cap = "converged" if t.converged else "→ budget"
        L.append(f'<text x="{lx+28}" y="{yy+4}" font-size="11.5" fill="#0f172a">'
                 f'{_esc(t.name)} · {t.accepts} accept(s) · {cap}</text>')

    L.append("</svg>")
    return "\n".join(L)


def explore_svg(elog, floor_pct: float, decision: str, reason: str,
                spec_name: str) -> str:
    """The explorer figure: 进化了 (realized, climbing) vs 能进化的 (headroom,
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
             f'fill="#64748b">碰不得的 floor ≈ {floor_pct:.0f}% (not-ours,跨不过) · '
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
             f'fill="#334155">attempt # (第几次尝试)</text>')
    L.append(f'<text x="22" y="{(y0+y1)/2:.0f}" font-size="12" fill="#334155" '
             f'transform="rotate(-90 22 {(y0+y1)/2:.0f})" text-anchor="middle">'
             f'占总运行时间 %</text>')

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
                     f'fill="#ea580c">{_esc(e["fn"])} {d} · 放宽档 · 要人拍板</text>')
        else:
            L.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" fill="#2563eb"/>')
            L.append(f'<text x="{cx+7:.1f}" y="{cy-7:.1f}" font-size="10.5" '
                     f'fill="#2563eb">{_esc(e["fn"])} {d}</text>')
    if elog:  # inline tag on the realized line itself
        L.append(f'<text x="{x1-6:.1f}" y="{Yc(realized[-1])-9:.1f}" text-anchor="end" '
                 f'font-size="11.5" font-weight="700" fill="#2563eb">↑ 已优化</text>')

    # headroom — drawn PER SEGMENT, each drop colored by WHY it fell, so a reader sees
    # that a drop is not "captured": 因赢而降 (a win shrank it) = green solid;
    # 因试败而降 (tried, no provable win → ruled out at this power) = red dashed;
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
            col, dash, mark = GREEN, "", "✓ 捕获"
        elif drop and cause is not None:
            col, dash, mark = RED, ' stroke-dasharray="5,3"', "✗ 排除"
        else:
            col, dash, mark = ORANGE, ' stroke-dasharray="7,4"', None
        L.append(f'<line x1="{xa:.1f}" y1="{ya:.1f}" x2="{xb:.1f}" y2="{yb:.1f}" '
                 f'stroke="{col}" stroke-width="2.5"{dash}/>')
        if mark and cause is not None:
            L.append(f'<text x="{(xa+xb)/2:.1f}" y="{(ya+yb)/2-5:.1f}" text-anchor="middle" '
                     f'font-size="9.5" fill="{col}">{mark} {_esc(cause["fn"])}</text>')
    if elog:  # inline tag on the headroom line itself
        L.append(f'<text x="{x1-6:.1f}" y="{Yc(head[-1])+17:.1f}" text-anchor="end" '
                 f'font-size="11.5" font-weight="700" fill="#ea580c">↓ 还能优化</text>')

    # legend + verdict
    L.append(f'<line x1="{x0+14}" y1="{y0+12}" x2="{x0+36}" y2="{y0+12}" '
             f'stroke="#2563eb" stroke-width="2.5"/>')
    L.append(f'<text x="{x0+42}" y="{y0+16}" font-size="11.5" fill="#0f172a">'
             f'进化了 realized — 已优化,越高越快 ↑</text>')
    L.append(f'<line x1="{x0+14}" y1="{y0+30}" x2="{x0+36}" y2="{y0+30}" '
             f'stroke="#ea580c" stroke-width="2.5" stroke-dasharray="7,4"/>')
    L.append(f'<text x="{x0+42}" y="{y0+34}" font-size="11.5" fill="#0f172a">'
             f'能进化的 addressable headroom — 剩余可优化,越低越少 ↓</text>')
    vcol = "#dc2626" if decision == "STOP" else "#059669"
    L.append(f'<rect x="{x0}" y="{y1+30}" width="{x1-x0}" height="44" rx="6" '
             f'fill="{vcol}" opacity="0.08"/>')
    L.append(f'<text x="{x0+12}" y="{y1+49}" font-size="13" font-weight="700" '
             f'fill="{vcol}">判定 {decision}</text>')
    L.append(f'<text x="{x0+12}" y="{y1+66}" font-size="11" fill="#334155">'
             f'{_esc(reason)}</text>')
    L.append(f'<text x="{x0+12}" y="{y1+94}" font-size="11" fill="#64748b">'
             f'橙线下降:<tspan fill="#059669" font-weight="600">✓ 优化成功(捕获)</tspan>  '
             f'<tspan fill="#dc2626" font-weight="600">✗ 试了没成(排除)</tspan> '
             f'<tspan fill="#94a3b8">(排除 = 那块时间还在、当前 lens/测量力下榨不出)</tspan></text>')
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
    factor = 1.0                 # cumulative time factor (Π of accepted (1+Δ/100))
    floor_pct = 0.0
    cur_fn, cur_regime = "", ""
    pending: dict = {}
    idx = 0
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
            accepted = verdict == "accepted"
            # the candidate's WOULD-BE absolute speedup = current cumulative ∘ its marginal Δ
            wb = None
            if isinstance(d0, (int, float)):
                wb_factor = factor * (1 + d0 / 100.0)
                wb = (1 - wb_factor) * 100.0          # % faster vs the ORIGINAL baseline
            c = {"x": cum_tok, "idx": idx, "delta": d0, "verdict": verdict,
                 "accepted": accepted, "wouldbe": wb, "fn": meta.get("fn", cur_fn),
                 "lens": meta.get("lens"), "regime": meta.get("regime", cur_regime)}
            cands.append(c)
            if accepted and isinstance(d0, (int, float)):
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
    "within-noise": ("#93c5fd", "试过·噪声内"),
    "noise-limited": ("#fcd34d", "噪声受限(有方向)"),
    "regressed": ("#fca5a5", "变慢(回归)"),
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
        # re-place the staircase nodes at the ordinal of their accepting candidate
        acc_idx = [c["idx"] for c in cands if c["accepted"]]
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
         f'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">']
    L.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
    L.append(f'<text x="{W/2}" y="28" text-anchor="middle" font-size="17" font-weight="700" '
             f'fill="#0f172a">{_esc(spec_name)}: 加速% vs 累计 token</text>')

    # Y grid + labels (span ymin..ymax)
    for k in range(6):
        v = ymin + (ymax - ymin) * k / 5
        yy = Y(v)
        L.append(f'<line x1="{x0}" y1="{yy:.1f}" x2="{x1}" y2="{yy:.1f}" stroke="#eef2f7"/>')
        L.append(f'<text x="{x0-8}" y="{yy+4:.1f}" text-anchor="end" font-size="11" '
                 f'fill="#94a3b8">{v:.0f}%</text>')
    # X ticks — even sixths over tokens; integer ordinals in the candidate-# fallback
    if d["have_tokens"]:
        xticks = [xmax * k / 6 for k in range(7)]
    else:
        st = max(1, int(xmax) // 8)
        xticks = list(range(0, int(xmax) + 1, st))
    for v in xticks:
        xx = X(v)
        L.append(f'<line x1="{xx:.1f}" y1="{y1}" x2="{xx:.1f}" y2="{y1+5}" stroke="#94a3b8"/>')
        L.append(f'<text x="{xx:.1f}" y="{y1+19}" text-anchor="middle" font-size="11" '
                 f'fill="#64748b">{xfmt(v)}</text>')
    L.append(f'<line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y1}" stroke="#334155"/>')
    L.append(f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="#334155"/>')
    L.append(f'<text x="22" y="{(y0+y1)/2:.0f}" font-size="12" fill="#334155" '
             f'transform="rotate(-90 22 {(y0+y1)/2:.0f})" text-anchor="middle">加速 (% faster)</text>')
    L.append(f'<text x="{(x0+x1)/2:.0f}" y="{H-10}" text-anchor="middle" font-size="12" '
             f'fill="#334155">{_esc(xlabel)}</text>')

    # reference lines: baseline 0% + Amdahl floor-ceiling
    L.append(f'<line x1="{x0}" y1="{Y(0):.1f}" x2="{x1}" y2="{Y(0):.1f}" stroke="#cbd5e1" '
             f'stroke-width="1.5"/>')
    if d["ceiling"] <= ymax:
        cy = Y(d["ceiling"])
        L.append(f'<line x1="{x0}" y1="{cy:.1f}" x2="{x1}" y2="{cy:.1f}" stroke="#64748b" '
                 f'stroke-width="1.5" stroke-dasharray="8,5"/>')
        L.append(f'<text x="{x1-4}" y="{cy-6:.1f}" text-anchor="end" font-size="11" '
                 f'fill="#64748b">理论上界 ~{d["ceiling"]:.0f}% (碰不得 floor {d["floor_pct"]:.0f}% 之外全榨干)</text>')

    # candidate dots (incl. regressions); off-spec as ×
    for c in cands:
        if c["accepted"]:
            continue
        cx = X(c["x"])
        if c["verdict"] in _OFFSPEC:
            yy = Y(0.0)
            L.append(f'<path d="M{cx-3.5:.1f},{yy-3.5:.1f} l7,7 M{cx+3.5:.1f},{yy-3.5:.1f} l-7,7" '
                     f'stroke="#cbd5e1" stroke-width="1.6"/>')
        elif isinstance(c.get("wouldbe"), (int, float)):
            col = _DOT.get(c["verdict"], ("#bfdbfe", ""))[0]
            L.append(f'<circle cx="{cx:.1f}" cy="{Y(c["wouldbe"]):.1f}" r="3.6" fill="{col}" '
                     f'opacity="0.9"/>')

    # running-best staircase
    pts = [(X(steps[0]["x"]), Y(0.0))]
    prev = 0.0
    for s in steps[1:]:
        pts.append((X(s["x"]), Y(prev)))
        pts.append((X(s["x"]), Y(s["realized"])))
        prev = s["realized"]
    if len(pts) > 1:
        L.append(f'<polyline points="{_pts(pts)}" fill="none" stroke="#2563eb" stroke-width="2.6"/>')
    for s in steps[1:]:
        cx, cy = X(s["x"]), Y(s["realized"])
        relaxed = s.get("regime") and s["regime"] != "byte-identical"
        fill = "#ffffff" if relaxed else "#2563eb"
        L.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{fill}" stroke="#2563eb" '
                 f'stroke-width="2.2"/>')
        lab = _esc(s.get("fn", ""))
        if s.get("lens"):
            lab += f' · {_esc(s["lens"].split("/")[0].split("-")[0])}'
        dd = f' {s["delta"]:+.1f}%' if isinstance(s.get("delta"), (int, float)) else ""
        L.append(f'<text x="{cx+8:.1f}" y="{cy-7:.1f}" font-size="10.5" fill="#1d4ed8">'
                 f'{lab}{dd}</text>')

    # final running-best tag
    if steps and len(steps) > 1:
        ex, ey = X(steps[-1]["x"]), Y(steps[-1]["realized"])
        L.append(f'<text x="{ex+8:.1f}" y="{ey+4:.1f}" font-size="12" font-weight="700" '
                 f'fill="#1d4ed8">running best · {d["realized"]:.1f}%↑</text>')

    # legend (top-left; staircase starts low so that corner is free)
    lx, ly = x0 + 14, y0 + 12
    leg = [("line", "#2563eb", "running best (累计 accept,越高越快)"),
           ("dot", "#93c5fd", f"候选(含回归) · {d['n']} 个"),
           ("x", "#cbd5e1", "off-spec:apply/build/verify 挂(不计分)"),
           ("dash", "#64748b", "理论上界(碰不得 floor 之外)")]
    # opaque backing so the ceiling dashed line / gridlines don't bleed through the text
    L.append(f'<rect x="{lx-9}" y="{ly-12}" width="306" height="{len(leg)*17+8}" rx="5" '
             f'fill="#ffffff" opacity="0.93" stroke="#eef2f7"/>')
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
        L.append(f'<text x="{lx+28}" y="{yy+4}" font-size="11" fill="#0f172a">{_esc(txt)}</text>')

    L.append("</svg>")
    return "\n".join(L)


def ascii_chart(trajs) -> str:
    """Immediately-visible console view: per-attempt rows with a proportional bar."""
    ymax = _ymax(trajs)
    out = ["", "cumulative speedup (% faster) — bar ∝ magnitude", ""]
    for t in trajs:
        cap = "converged ■ (plateau)" if t.converged else "→ ran to budget"
        out.append(f"[{t.name}]  {t.accepts} accept(s) · final {-t.final_pct:.1f}% faster · {cap}")
        out.append("  att  verdict        Δ          cumulative")
        prevsp = 0.0
        for s in t.steps:
            bar = "█" * int(round(s.speedup_pct / ymax * 34))
            d = f"{s.delta_pct:+.2f}%" if isinstance(s.delta_pct, (int, float)) else "   —  "
            marg = s.speedup_pct - prevsp
            margn = f"(+{marg:.1f})" if s.accepted and marg > 0 else ""
            reg = "" if s.regime == "byte-identical" else f" [{s.regime}]"
            out.append(f"  {s.i:>3}  {s.verdict:<13} {d:>9}  {bar:<34}│ "
                       f"{-s.cum_pct:5.1f}% faster {margn}{reg}")
            prevsp = s.speedup_pct
        out.append("")
    return "\n".join(out)


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def main(argv) -> None:
    def opt(flag, d=None):
        return argv[argv.index(flag) + 1] if flag in argv else d

    series = [argv[i + 1] for i, a in enumerate(argv) if a == "--series"]
    if not series:
        raise SystemExit('usage: python3 -m aro chart '
                         '--series "name|regime|converged|dir1,dir2" '
                         '[--series ...] [--out chart.svg] [--title T]')
    trajs = []
    for s in series:
        parts = s.split("|", 3)
        if len(parts) != 4:
            raise SystemExit(f"bad --series (need name|regime|converged|dirs): {s}")
        name, regime, conv, dirs = parts
        trajs.append(trajmod.stitch([d for d in dirs.split(",") if d], name,
                                    regime=regime, converged=(conv == "converged")))
    print(ascii_chart(trajs))
    out = opt("--out")
    if out:
        title = opt("--title", "ARO search trajectory — cumulative speedup vs attempts")
        Path(out).write_text(svg(trajs, title=title) + "\n")
        print(f"chart → {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
