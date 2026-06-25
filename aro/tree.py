"""aro tree — render a run's DECISION TREE as a self-contained interactive HTML.

Reads an explorer run's `out-dir` (events.jsonl + a{N}/records.jsonl + a{N}/patches/)
and emits ONE standalone .html (no CDN, no deps): the left pane is the search tree —
each attempted function in order, its candidate(s), and the reflect-proposed-but-UNTRIED
branches; nodes are color-coded by verdict, skipped/untried shown distinctly. Click any
node → the right pane shows that point's report (hypothesis, verdict, Δ/CI/floor, the
diff, the explore decision at that step).

    python3 -m aro tree <out-dir> [--out tree.html]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _latest_slice(evs):
    rids = [e.get("run_id") for e in evs if e.get("run_id")]
    if not rids:
        return evs
    last = rids[-1]
    return [e for e in evs if e.get("run_id") == last]


def build_tree(out_dir) -> dict:
    out_dir = Path(out_dir)
    evs = []
    for ln in (out_dir / "events.jsonl").read_text().splitlines():
        ln = ln.strip()
        if ln:
            try:
                evs.append(json.loads(ln))
            except Exception:
                pass
    evs = _latest_slice(evs)

    nodes, steps = [], {}
    cur = None
    ran = 0
    frontier = []
    for e in evs:
        ev = e.get("event")
        if ev == "attempt_frontier":
            frontier = e.get("fns", []) or []
        elif ev == "attempt_started":
            ran += 1
            cur = {"id": f"a{ran}", "type": "fn", "i": ran, "fn": e.get("fn"),
                   "regime": e.get("regime"), "pct": e.get("pct"),
                   "files": e.get("files", []), "reflect": [], "candidates": [],
                   "status": "running"}
            nodes.append(cur)
        elif ev == "attempt_skipped":
            nodes.append({"id": f"sk{len(nodes)}", "type": "skipped",
                          "fn": e.get("fn"), "reason": e.get("reason")})
        elif ev == "direction_proposed" and cur is not None:
            cur["reflect"].append({"id": e.get("id"), "text": e.get("direction"),
                                   "tried": False})
        elif ev == "attempt_finished" and cur is not None:
            cur["status"] = e.get("verdict")
            cur["delta"] = e.get("delta")
            cur["accepted"] = e.get("accepted")
            cur["regime"] = e.get("regime")
            cur = None
        elif ev == "explore_step":
            steps[e.get("i")] = e

    # attach the explorer's per-step decision; load each attempt's candidate(s) + diff
    for n in nodes:
        if n["type"] != "fn":
            continue
        s = steps.get(n["i"], {})
        n["decision"] = s.get("decision")
        n["reason"] = s.get("reason")
        n["realized"] = s.get("realized_pct")
        n["headroom"] = s.get("headroom_pct")
        rec = out_dir / n["id"] / "records.jsonl"
        if rec.exists():
            for ln in rec.read_text().splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if str(r.get("id", "")).startswith("base-"):
                    continue  # cumulative-compound seed, not a real candidate
                diff_p = out_dir / n["id"] / "patches" / (r["id"] + ".txt")
                n["candidates"].append({
                    "id": r["id"], "hypothesis": r.get("hypothesis", ""),
                    "verdict": r.get("verdict"), "metrics": r.get("metrics", []),
                    "notes": r.get("notes", []),
                    "diff": diff_p.read_text() if diff_p.exists() else ""})

    last_step = steps.get(max(steps) if steps else None, {})
    attempted = [n for n in nodes if n["type"] == "fn"]
    summary = {
        "attempted": len(attempted),
        "accepted": sum(1 for n in attempted if n.get("accepted")),
        "skipped": sum(1 for n in nodes if n["type"] == "skipped"),
        "realized_pct": last_step.get("realized_pct", 0.0),
        "headroom_pct": last_step.get("headroom_pct", 0.0),
        "decision": last_step.get("decision", "?"),
        "reason": last_step.get("reason", ""),
        "frontier": frontier,
    }
    return {"spec": out_dir.name, "summary": summary, "nodes": nodes}


def render_html(tree: dict, title: str) -> str:
    data = json.dumps(tree, ensure_ascii=False).replace("</", "<\\/")
    return _TEMPLATE.replace("/*__DATA__*/null", data).replace("__TITLE__", title)


_TEMPLATE = r"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>__TITLE__ · 决策树</title>
<style>
 *{box-sizing:border-box} body{margin:0;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#0f172a;background:#f8fafc}
 header{padding:14px 20px;background:#fff;border-bottom:1px solid #e2e8f0}
 h1{margin:0 0 6px;font-size:17px} .chips{display:flex;gap:8px;flex-wrap:wrap;font-size:12px}
 .chip{padding:3px 9px;border-radius:12px;background:#f1f5f9;color:#334155}
 .chip b{color:#0f172a}
 .legend{display:flex;gap:12px;flex-wrap:wrap;font-size:11px;color:#64748b;margin-top:6px}
 .dot{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px;vertical-align:middle}
 main{display:flex;gap:0;height:calc(100vh - 92px)}
 #tree{flex:1.1;overflow:auto;padding:16px 20px;border-right:1px solid #e2e8f0}
 #detail{flex:1;overflow:auto;padding:18px 22px;background:#fff}
 .node{margin:3px 0;padding:7px 11px;border-radius:7px;border:1px solid #e2e8f0;background:#fff;cursor:pointer;display:flex;align-items:center;gap:8px;font-size:13px;transition:.1s}
 .node:hover{border-color:#94a3b8;background:#f8fafc} .node.sel{outline:2px solid #2563eb;outline-offset:-1px}
 .node .idx{font-weight:700;color:#64748b;min-width:22px}
 .badge{font-size:10.5px;font-weight:600;padding:1px 7px;border-radius:10px;color:#fff;white-space:nowrap}
 .children{margin-left:30px;border-left:2px dashed #e2e8f0;padding-left:14px}
 .child{margin:2px 0;padding:5px 10px;border-radius:6px;font-size:12px;cursor:pointer;border:1px solid transparent}
 .child:hover{background:#f1f5f9} .child.sel{outline:2px solid #2563eb;outline-offset:-1px}
 .cand{background:#f8fafc;border-color:#e2e8f0} .reflect{color:#7c3aed;border:1px dashed #c4b5fd;background:#faf5ff}
 .skip{opacity:.7} .skip .badge{background:#ea580c}
 .muted{color:#94a3b8} .fade{opacity:.55}
 #detail h2{font-size:15px;margin:0 0 4px} #detail .sub{color:#64748b;font-size:12px;margin-bottom:12px}
 .kv{display:grid;grid-template-columns:120px 1fr;gap:4px 10px;font-size:12.5px;margin:10px 0}
 .kv .k{color:#64748b} pre{background:#0f172a;color:#e2e8f0;padding:12px;border-radius:8px;overflow:auto;font-size:11.5px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
 .hint{color:#94a3b8;font-size:13px;margin-top:40px;text-align:center}
 table.m{border-collapse:collapse;font-size:12px;margin:8px 0} table.m td,table.m th{border:1px solid #e2e8f0;padding:3px 8px}
</style></head><body>
<header>
 <h1 id="title"></h1>
 <div class="chips" id="chips"></div>
 <div class="legend">
  <span><i class="dot" style="background:#16a34a"></i>accepted</span>
  <span><i class="dot" style="background:#64748b"></i>within-noise</span>
  <span><i class="dot" style="background:#ca8a04"></i>noise-limited</span>
  <span><i class="dot" style="background:#dc2626"></i>regressed/verify/rejected</span>
  <span><i class="dot" style="background:#ea580c"></i>skipped(无 fn)</span>
  <span><i class="dot" style="background:#7c3aed"></i>reflect 提出·未试</span>
 </div>
</header>
<main><div id="tree"></div><div id="detail"><div class="hint">← 点左边任意节点,看当时的报告</div></div></main>
<script>
const DATA = /*__DATA__*/null;
const COL = {accepted:'#16a34a','within-noise':'#64748b','noise-limited':'#ca8a04',regressed:'#dc2626','verify-failed':'#dc2626','build-failed':'#ea580c',rejected:'#dc2626',unlocated:'#ea580c',running:'#94a3b8'};
const col = s => COL[s] || '#64748b';
const el = (t,c,txt)=>{const e=document.createElement(t); if(c)e.className=c; if(txt!=null)e.textContent=txt; return e;};
function badge(text,color){const b=el('span','badge',text); b.style.background=color; return b;}
function dpct(d){return (typeof d==='number')? (d>=0?'+':'')+d.toFixed(2)+'%' : '—';}

let selected=null;
function select(node,detailFn){
  document.querySelectorAll('.sel').forEach(e=>e.classList.remove('sel'));
  node.classList.add('sel'); detailFn();
}

function metricsTable(ms){
  if(!ms||!ms.length) return '';
  let h='<table class="m"><tr><th>metric</th><th>Δ</th><th>CI</th><th>floor</th><th>improved</th></tr>';
  ms.forEach(m=>{h+=`<tr><td>${m.metric}</td><td>${dpct(m.delta_pct)}</td><td>[${(m.ci_low_pct||0).toFixed(2)}, ${(m.ci_high_pct||0).toFixed(2)}]</td><td>${(m.floor_pct||0).toFixed(2)}%</td><td>${m.improved?'✓':'—'}</td></tr>`;});
  return h+'</table>';
}
function kv(rows){let h='<div class="kv">'; rows.forEach(([k,v])=>{if(v!=null&&v!=='')h+=`<div class="k">${k}</div><div>${v}</div>`;}); return h+'</div>';}

function showFn(n){
  const d=document.getElementById('detail'); d.innerHTML='';
  d.innerHTML=`<h2>${n.i}. <code>${n.fn}</code> <span style="color:${col(n.status)}">· ${n.status}${typeof n.delta==='number'?' '+dpct(n.delta):''}</span></h2>`
    + `<div class="sub">第 ${n.i} 轮 · regime: ${n.regime||'-'} · 占比 ${n.pct!=null?n.pct+'%':'-'}</div>`
    + kv([['探索器判定', n.decision? `<b style="color:${n.decision==='STOP'?'#dc2626':'#16a34a'}">${n.decision}</b> — ${n.reason||''}`:''],
          ['进化了(realized)', n.realized!=null? (n.realized>0?'+':'')+'快 '+(-n.realized).toFixed(2)+'%':''],
          ['能进化的(headroom)', n.headroom!=null? n.headroom.toFixed(2)+'%':''],
          ['编辑范围', (n.files||[]).map(f=>'<code>'+f+'</code>').join('<br>')]]);
  if(n.candidates&&n.candidates.length){
    const c=n.candidates[0];
    d.innerHTML+=`<h3 style="margin-top:16px;font-size:13px">候选(agent 提的方案)</h3>`+kv([['verdict',`<b style="color:${col(c.verdict)}">${c.verdict}</b>`]]);
    d.innerHTML+=`<div style="font-size:12.5px;line-height:1.6;margin:8px 0"><b>改了什么:</b> ${escapeHtml(c.hypothesis)}</div>`;
    d.innerHTML+=metricsTable(c.metrics);
    if(c.diff){const p=el('pre',null,c.diff); d.appendChild(document.createElement('h3')).textContent=''; const h=el('div'); h.style.cssText='font-size:13px;font-weight:600;margin:12px 0 6px'; h.textContent='代码 diff'; d.appendChild(h); d.appendChild(p);}
  } else { d.innerHTML+='<div class="muted" style="margin-top:14px">(无候选记录)</div>'; }
  if(n.reflect&&n.reflect.length){
    const h=el('div'); h.style.cssText='font-size:13px;font-weight:600;margin:16px 0 6px;color:#7c3aed'; h.textContent='reflect 提出但未试的方向'; d.appendChild(h);
    n.reflect.forEach(r=>{const x=el('div'); x.style.cssText='font-size:12px;border:1px dashed #c4b5fd;background:#faf5ff;border-radius:6px;padding:7px 10px;margin:4px 0'; x.innerHTML=`<b>[${r.id}] 未试</b> — ${escapeHtml(r.text)}`; d.appendChild(x);});
  }
}
function showReflect(r){const d=document.getElementById('detail'); d.innerHTML=`<h2 style="color:#7c3aed">reflect 方向 [${r.id}] · <span class="muted">未试</span></h2><div style="font-size:13px;line-height:1.7;margin-top:10px">${escapeHtml(r.text)}</div><div class="muted" style="margin-top:14px">这是 agent 在该轮 reflect 阶段提出的下一步想法,但在停机前没轮到试。</div>`;}
function showSkip(n){const d=document.getElementById('detail'); d.innerHTML=`<h2><code>${n.fn}</code> · <span style="color:#ea580c">skipped</span></h2><div class="sub">${n.reason||'source not located'}</div><div class="muted" style="margin-top:14px">这个热帧在 workspace 源码里找不到对应的 <code>fn</code>(宏生成 / 内联 / demangler 残留)→ 无处下手,跳过。</div>`;}
function escapeHtml(s){const e=el('div'); e.textContent=s||''; return e.innerHTML;}

function build(){
  document.getElementById('title').textContent = DATA.spec + ' — 决策树';
  const s=DATA.summary, chips=document.getElementById('chips');
  [['尝试',s.attempted],['优化成功',s.accepted],['跳过',s.skipped],['进化了','快 '+(-s.realized_pct).toFixed(1)+'%'],['能进化的',s.headroom_pct.toFixed(1)+'%'],['判定',s.decision]]
    .forEach(([k,v])=>{const c=el('span','chip'); c.innerHTML=k+' <b>'+v+'</b>'; chips.appendChild(c);});
  if(s.decision) chips.lastChild.style.background = s.decision==='STOP'?'#fee2e2':'#dcfce7';
  const t=document.getElementById('tree');
  DATA.nodes.forEach(n=>{
    if(n.type==='fn'){
      const row=el('div','node'); row.appendChild(el('span','idx','#'+n.i));
      const lbl=document.createElement('code'); lbl.textContent=n.fn; lbl.style.fontWeight='600'; row.appendChild(lbl);
      row.appendChild(badge(n.status+(typeof n.delta==='number'?' '+dpct(n.delta):''), col(n.status)));
      if(n.regime&&n.regime!=='byte-identical'){const rb=badge('放宽档','#ea580c'); rb.style.background='#fff7ed'; rb.style.color='#c2410c'; rb.style.border='1px solid #fdba74'; row.appendChild(rb);}
      if(n.decision==='STOP'){row.appendChild(badge('→ STOP','#dc2626'));}
      row.onclick=()=>select(row,()=>showFn(n)); t.appendChild(row);
      const kids=el('div','children');
      (n.candidates||[]).forEach(c=>{const x=el('div','child cand'); x.innerHTML=`<span style="color:${col(c.verdict)}">●</span> 候选: ${escapeHtml((c.hypothesis||'').slice(0,70))}…`; x.onclick=ev=>{ev.stopPropagation();select(x,()=>showFn(n));}; kids.appendChild(x);});
      (n.reflect||[]).forEach(r=>{const x=el('div','child reflect'); x.textContent='⟳ '+r.id+' 未试: '+(r.text||'').slice(0,64)+'…'; x.onclick=ev=>{ev.stopPropagation();select(x,()=>showReflect(r));}; kids.appendChild(x);});
      if(kids.childNodes.length) t.appendChild(kids);
    } else if(n.type==='skipped'){
      const row=el('div','node skip'); row.appendChild(el('span','idx','⊘'));
      const lbl=document.createElement('code'); lbl.textContent=n.fn; row.appendChild(lbl);
      row.appendChild(badge('skipped','#ea580c')); row.onclick=()=>select(row,()=>showSkip(n)); t.appendChild(row);
    }
  });
}
build();
</script></body></html>"""


def main(argv) -> None:
    if not argv:
        raise SystemExit("usage: python3 -m aro tree <out-dir> [--out tree.html]")
    out_dir = argv[0]
    tree = build_tree(out_dir)
    html = render_html(tree, tree["spec"])
    out = (argv[argv.index("--out") + 1] if "--out" in argv
           else str(Path(out_dir) / "decision-tree.html"))
    Path(out).write_text(html)
    print(f"decision tree → {out}")
    print(f"  {tree['summary']['attempted']} attempted · "
          f"{tree['summary']['accepted']} accepted · "
          f"{tree['summary']['skipped']} skipped · {tree['summary']['decision']}")


if __name__ == "__main__":
    main(sys.argv[1:])
