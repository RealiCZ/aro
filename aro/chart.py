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
