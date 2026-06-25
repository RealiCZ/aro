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

import difflib
import json
import sys
from pathlib import Path


def _latest_slice(evs):
    rids = [e.get("run_id") for e in evs if e.get("run_id")]
    if not rids:
        return evs
    last = rids[-1]
    return [e for e in evs if e.get("run_id") == last]


def _compact_diff(patch_text: str) -> str:
    """A candidate's stored patch is whole-file SEARCH→REPLACE blocks (huge). Turn it
    into a COMPACT unified diff — only the changed hunks with 3 lines of context, `+`/`-`
    prefixed — so the report shows the actual edit, not the whole file. Per edit, a
    `# <path>` header then the `@@`/`+`/`-`/` ` lines (the `---`/`+++` file headers dropped)."""
    from . import store
    try:
        edits = store._parse_patch_file(patch_text)
    except Exception:
        return patch_text
    if not edits:
        return ""
    out = []
    for e in edits:
        ud = list(difflib.unified_diff(e.search.splitlines(), e.replace.splitlines(),
                                       lineterm="", n=3))
        body = "\n".join(l for l in ud if not l.startswith("--- ") and not l.startswith("+++ "))
        out.append(f"# {e.path}\n{body}" if body.strip() else f"# {e.path}\n(无文本差异)")
    return "\n\n".join(out)


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
    floor_frames = []
    for e in evs:
        ev = e.get("event")
        if ev == "attempt_frontier":
            frontier = e.get("fns", []) or []
        elif ev == "profile_floor":
            floor_frames = e.get("frames", []) or []
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
                    "diff": _compact_diff(diff_p.read_text()) if diff_p.exists() else ""})

    last_step = steps.get(max(steps) if steps else None, {})
    attempted = [n for n in nodes if n["type"] == "fn"]
    floor = round(last_step.get("floor_pct", 0.0) or 0.0, 1)
    unreach = round(last_step.get("unreachable_pct", 0.0) or 0.0, 1)
    head = round(last_step.get("headroom_pct", 0.0) or 0.0, 1)
    # Coverage decomposition (self-time %), one entry per distinct FUNCTION (the two
    # sstore attempts are the same frame → dedup, accepted status wins). The bar shows
    # WHERE the runtime is + WHAT we did to each part; the realized speedup is separate.
    best = {}
    for n in attempted:
        nm = n.get("fn"); st = n.get("status"); pct = n.get("pct") or 0.0
        if nm not in best or st == "accepted" or n.get("accepted"):
            best[nm] = ("accepted" if n.get("accepted") else st, pct)
    cap = round(sum(p for s, p in best.values() if s == "accepted"), 1)
    tried_fail = round(sum(p for s, p in best.values() if s != "accepted"), 1)
    segs = [
        {"key": "captured", "label": "已优化(accept)", "pct": cap, "color": "#16a34a"},
        {"key": "tried", "label": "试过没过", "pct": tried_fail, "color": "#cbd5e1"},
        {"key": "headroom", "label": "未试(headroom)", "pct": head, "color": "#93c5fd"},
        {"key": "unreachable", "label": "够不着(内联/宏)", "pct": unreach, "color": "#e5e7eb", "hatch": True},
        {"key": "floor", "label": "碰不得(crypto/runtime)", "pct": floor, "color": "#475569"},
    ]
    rest = round(max(0.0, 100.0 - sum(s["pct"] for s in segs)), 1)
    if rest >= 0.5:
        segs.append({"key": "other", "label": "其它/未归类", "pct": rest, "color": "#f1f5f9"})
    summary = {
        "attempted": len(attempted),
        "accepted": sum(1 for n in attempted if n.get("accepted")),
        "skipped": sum(1 for n in nodes if n["type"] == "skipped"),
        "realized_pct": last_step.get("realized_pct", 0.0),
        "headroom_pct": head, "floor_pct": floor, "unreachable_pct": unreach,
        "decision": last_step.get("decision", "?"),
        "reason": last_step.get("reason", ""),
        "frontier": frontier, "coverage": segs, "floor_frames": floor_frames,
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
 details.fnnode{margin:3px 0}
 details.fnnode>summary{list-style:none}
 details.fnnode>summary::-webkit-details-marker{display:none}
 details.fnnode>summary::before{content:'▶';color:#94a3b8;font-size:9px;flex:0 0 auto}
 details.fnnode[open]>summary::before{content:'▼'}
 summary{cursor:pointer;outline:none}
 .treebar button{font-size:12px;padding:3px 10px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;cursor:pointer}
 .treebar button:hover{background:#f1f5f9}
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
 .covbar{display:flex;height:32px;border-radius:6px;overflow:hidden;border:1px solid #e2e8f0}
 .covseg{display:flex;align-items:center;justify-content:center;font-size:10.5px;min-width:2px;overflow:hidden;white-space:nowrap;cursor:default}
 .covseg.hatch{background-image:repeating-linear-gradient(45deg,#cbd5e1 0 4px,transparent 4px 8px)!important}
 .icicle{display:flex;gap:14px;align-items:stretch;min-height:440px;height:calc(100% - 130px)}
 .col{display:flex;flex-direction:column}
 .col-root{justify-content:center;flex:0 0 100px}
 .rootbox{padding:10px;border:1px solid #cbd5e1;border-radius:8px;background:#fff;font-size:12px;text-align:center}
 .col-fns{flex:0 0 220px;gap:4px}
 .col-cands{flex:1;overflow:auto;border-left:2px dashed #e2e8f0;padding-left:14px}
 .fnblock{border:1px solid #e2e8f0;border-radius:7px;background:#fff;padding:5px 9px;cursor:pointer;display:flex;flex-direction:column;justify-content:center;min-height:30px;overflow:hidden;transition:.1s}
 .fnblock:hover{border-color:#94a3b8} .fnblock.sel{outline:2px solid #2563eb;outline-offset:-1px}
 .fnblock.accepted{background:#f0fdf4}
 .fnname{font-size:12.5px;font-weight:600} .fnmeta{font-size:10.5px;color:#64748b;margin-top:1px}
 .candblock{border:1px solid #e2e8f0;border-radius:6px;background:#f8fafc;padding:6px 9px;margin:3px 0;cursor:pointer}
 .candblock:hover{border-color:#94a3b8} .candblock.sel{outline:2px solid #2563eb;outline-offset:-1px}
 .diffbox{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11.5px;line-height:1.55;border:1px solid #e2e8f0;border-radius:8px;overflow:auto;max-height:62vh;white-space:pre;margin-top:6px}
 .covrow{display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:12.5px;padding:7px 10px;margin:3px 0;border:1px solid #e2e8f0;border-radius:7px;background:#fff;cursor:pointer}
 .covrow:hover{border-color:#94a3b8;background:#f8fafc}
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
const COL = {accepted:'#16a34a','within-noise':'#64748b','noise-limited':'#ca8a04',regressed:'#dc2626','verify-failed':'#dc2626','build-failed':'#ea580c',rejected:'#dc2626',unlocated:'#ea580c',skipped:'#ea580c',running:'#94a3b8'};
const col = s => COL[s] || '#64748b';
const el = (t,c,txt)=>{const e=document.createElement(t); if(c)e.className=c; if(txt!=null)e.textContent=txt; return e;};
function badge(text,color){const b=el('span','badge',text); b.style.background=color; return b;}
function dpct(d){return (typeof d==='number')? (d>=0?'+':'')+d.toFixed(2)+'%' : '—';}

let selected=null;
const NODES={};                 // n.i -> fn node, so candidate-switch chips can reach it
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

function regimeCn(r){ return (r&&r!=='byte-identical') ? '需人工复核(动了结构,不建议直接合)' : '行为不变(可直接合)'; }
function diffHtml(d){
  return (d||'').split('\n').map(function(l){
    const ch=l.charAt(0); let c='#475569', bg='transparent';
    if(l.indexOf('# ')===0){c='#0f172a';bg='#eef2f7';}
    else if(ch==='@'){c='#7c3aed';bg='#faf5ff';}
    else if(ch==='+'){c='#166534';bg='#dcfce7';}
    else if(ch==='-'){c='#991b1b';bg='#fee2e2';}
    return '<div style="color:'+c+';background:'+bg+';padding:0 6px">'+(escapeHtml(l)||'&nbsp;')+'</div>';
  }).join('');
}
function showFn(n, ci){
  ci = ci|0;
  const d=document.getElementById('detail'); d.innerHTML='';
  d.innerHTML=`<h2>${n.i}. <code>${n.fn}</code> <span style="color:${col(n.status)}">· ${n.status}${typeof n.delta==='number'?' '+dpct(n.delta):''}</span></h2>`
    + `<div class="sub">第 ${n.i} 个尝试 · ${regimeCn(n.regime)} · 占运行时 ${n.pct!=null?n.pct+'%':'-'}</div>`
    + kv([['本函数贡献', (n.accepted && typeof n.delta==='number')
            ? `<b style="color:#16a34a">快 ${(-n.delta).toFixed(2)}%</b> · 计入累计`
            : `<span class="muted">0%(${n.status||'—'},未计入累计)</span>`],
          ['此刻累计(realized)', n.realized!=null? `快 ${(-n.realized).toFixed(2)}%<span class="muted"> · 全轮跑到这一步的总进度,非本函数</span>`:''],
          ['探索器判定', n.decision? `<b style="color:${n.decision==='STOP'?'#dc2626':'#16a34a'}">${n.decision}</b> — ${n.reason||''}`:''],
          ['编辑范围', (n.files||[]).map(f=>'<code>'+f+'</code>').join('<br>')]]);
  if(n.candidates&&n.candidates.length){
    const cs=n.candidates; if(ci>=cs.length) ci=0; const c=cs[ci];
    d.innerHTML+=`<h3 style="margin-top:16px;font-size:13px">候选 <code>${c.id}</code> · <span style="color:${col(c.verdict)}">${c.verdict}</span></h3>`;
    d.innerHTML+=`<div style="font-size:12.5px;line-height:1.6;margin:8px 0"><b>改了什么:</b> ${escapeHtml(c.hypothesis)}</div>`;
    d.innerHTML+=metricsTable(c.metrics);
    if(c.diff){const det=document.createElement('details'); det.open=true; det.style.marginTop='12px'; const sum=el('summary',null,'代码改动(diff)'); sum.style.cssText='font-size:13px;font-weight:600;cursor:pointer;color:#334155;user-select:none'; det.appendChild(sum); const dv=el('div','diffbox'); dv.innerHTML=diffHtml(c.diff); det.appendChild(dv); d.appendChild(det);}
  } else { d.innerHTML+='<div class="muted" style="margin-top:14px">(无候选记录)</div>'; }
}
function showFloor(){
  const d=document.getElementById('detail'); const fr=DATA.summary.floor_frames||[];
  let h=`<h2>碰不得 — 动不了的底座 <span class="muted">${(DATA.summary.floor_pct||0).toFixed(1)}%</span></h2>`
    +`<div class="sub">这些热帧不在我方代码里(上游 crypto / 运行时库)—— ARO 不能改。按所属 crate 归组,占运行时越多越靠前。</div>`;
  if(!fr.length){ h+='<div class="muted" style="margin-top:14px">本轮没记明细(旧 run)。新一轮探索会记下每个碰不得的热帧 + 它属于哪个上游 crate,这里就能展开成树。</div>'; d.innerHTML=h; return; }
  const g={};
  fr.forEach(f=>{ const o=f.owner||'?'; g[o]=g[o]||{}; const w=f.why||'?'; (g[o][w]=g[o][w]||[]).push(f); });
  Object.keys(g).forEach(owner=>{
    let osum=0; Object.keys(g[owner]).forEach(w=>g[owner][w].forEach(f=>osum+=f.pct||0));
    const oname=owner==='crypto'?'crypto(密码学)':owner==='runtime'?'runtime(运行时/框架)':owner;
    h+=`<div style="margin-top:14px;font-weight:700;font-size:13px">${oname} <span class="muted">≈${osum.toFixed(1)}%</span></div>`;
    Object.keys(g[owner]).forEach(why=>{
      h+=`<div style="margin:5px 0 2px 8px;color:#64748b;font-size:12px">▸ <code>${escapeHtml(why)}</code></div>`;
      g[owner][why].sort((a,b)=>(b.pct||0)-(a.pct||0)).forEach(f=>{
        h+=`<div style="margin-left:22px;font-size:12px;display:flex;justify-content:space-between;max-width:440px;padding:1px 0"><code>${escapeHtml(f.name)}</code><span class="muted">${(f.pct||0).toFixed(1)}%</span></div>`;
      });
    });
  });
  d.innerHTML=h;
}
function JUMP(i){ if(i==null||i<0) return; const b=document.querySelector('.fnblock[data-i="'+i+'"]'); if(b){ b.click(); b.scrollIntoView({block:'center'}); } }
function covList(title,desc,nodes){
  let h='<h2>'+escapeHtml(title)+'</h2><div class="sub">'+escapeHtml(desc)+'</div>';
  if(!nodes.length) return h+'<div class="muted" style="margin-top:12px">(本轮无)</div>';
  nodes.forEach(n=>{ const stat=n.type==='skipped'?'skipped':n.status; const ji=(n.i!=null?n.i:-1);
    h+='<div class="covrow" onclick="JUMP('+ji+')"><code>'+escapeHtml(n.fn)+'</code><span>'+(n.pct!=null?n.pct+'% · ':'')
      +'<span style="color:'+col(stat)+';font-weight:600">'+stat+(typeof n.delta==='number'?' '+dpct(n.delta):'')+'</span>'+(ji>=0?' ▸':'')+'</span></div>';
  });
  return h;
}
function showCov(key){
  if(key==='floor') return showFloor();
  const d=document.getElementById('detail');
  const fns=DATA.nodes.filter(n=>n.type==='fn');
  const seg=(DATA.summary.coverage||[]).find(s=>s.key===key)||{};
  if(key==='captured') d.innerHTML=covList('已优化(accept) '+(seg.pct||0)+'% — 落地的优化','判过完整 judge、确认真提速的函数;它们的 Δ 计入累计 realized。点进去看候选 + 代码改动。',fns.filter(n=>n.accepted));
  else if(key==='tried') d.innerHTML=covList('试过没过 '+(seg.pct||0)+'% — 打了但没赢','judge 判过但没过(噪声内 / noise-limited / 变慢)。诚实记录:这些不计入 realized。',fns.filter(n=>!n.accepted));
  else if(key==='unreachable') d.innerHTML=covList('够不着 '+(seg.pct||0)+'% — 无 fn 可定位','热帧在 workspace 找不到对应 fn(内联/宏/demangler 残留),无处下手。',DATA.nodes.filter(n=>n.type==='skipped'));
  else if(key==='headroom'){
    const at=new Set(fns.map(n=>n.fn)); const left=(DATA.summary.frontier||[]).filter(f=>!at.has(f));
    let h='<h2>未试(headroom) '+(seg.pct||0)+'%</h2><div class="sub">还能定位、本轮预算没轮到打的我方函数(Amdahl 上界)。再跑一轮可继续挖。</div>';
    h+= left.length? left.map(f=>'<div class="covrow"><code>'+escapeHtml(f)+'</code><span class="muted">未试</span></div>').join('')
                   : '<div class="muted" style="margin-top:12px">本轮前沿基本打完;剩余 headroom 来自 re-profile 后才浮现的小函数,没有固定名单。</div>';
    d.innerHTML=h;
  }
  else d.innerHTML='<h2>其它/未归类 '+(seg.pct||0)+'%</h2><div class="sub">bench 里未归入上述类别的零散帧(测量误差 + 未分类的小帧),没有可下钻的函数。</div>';
}
function showReflect(r){const d=document.getElementById('detail'); d.innerHTML=`<h2 style="color:#7c3aed">reflect 方向 [${r.id}] · <span class="muted">未试</span></h2><div style="font-size:13px;line-height:1.7;margin-top:10px">${escapeHtml(r.text)}</div><div class="muted" style="margin-top:14px">这是 agent 在该轮 reflect 阶段提出的下一步想法,但在停机前没轮到试。</div>`;}
function showSkip(n){const d=document.getElementById('detail'); d.innerHTML=`<h2><code>${n.fn}</code> · <span style="color:#ea580c">skipped</span></h2><div class="sub">${n.reason||'source not located'}</div><div class="muted" style="margin-top:14px">这个热帧在 workspace 源码里找不到对应的 <code>fn</code>(宏生成 / 内联 / demangler 残留)→ 无处下手,跳过。</div>`;}
function escapeHtml(s){const e=el('div'); e.textContent=s||''; return e.innerHTML;}

function fillCands(n, cands){
  cands.innerHTML='';
  if(n.type==='skipped'){ cands.innerHTML='<div class="muted" style="font-size:12px;padding:8px">跳过 — 无 fn 可定位(内联/宏/demangler 残留)</div>'; return; }
  const h=el('div'); h.style.cssText='font-size:11px;color:#64748b;margin:2px 0 6px';
  h.textContent=(n.candidates||[]).length+' 候选 · '+(n.reflect||[]).length+' 未试方向';
  cands.appendChild(h);
  (n.candidates||[]).forEach((c,ci)=>{
    const x=el('div','candblock'); x.style.borderLeft='4px solid '+col(c.verdict);
    x.innerHTML='<code>'+c.id+'</code> <span style="color:'+col(c.verdict)+';font-size:11px;font-weight:600">'+c.verdict+'</span>'
      +'<div class="muted" style="font-size:11px;margin-top:2px">'+escapeHtml((c.hypothesis||'').slice(0,64))+'…</div>';
    x.onclick=()=>{ cands.querySelectorAll('.candblock.sel').forEach(e=>e.classList.remove('sel')); x.classList.add('sel'); showFn(n,ci); };
    cands.appendChild(x);
  });
  if(n.reflect&&n.reflect.length){
    const rdet=document.createElement('details'); rdet.style.marginTop='8px';
    const rsum=el('summary',null,'⟳ '+n.reflect.length+' 条 reflect 未试方向'); rsum.style.cssText='font-size:12px;color:#7c3aed;cursor:pointer;user-select:none'; rdet.appendChild(rsum);
    n.reflect.forEach(r=>{ const x=el('div'); x.style.cssText='font-size:11px;border:1px dashed #c4b5fd;background:#faf5ff;border-radius:5px;padding:5px 8px;margin:3px 0;cursor:pointer'; x.innerHTML='<b>['+r.id+'] 未试</b> '+escapeHtml((r.text||'').slice(0,72))+'…'; x.onclick=()=>showReflect(r); rdet.appendChild(x); });
    cands.appendChild(rdet);
  }
}


function build(){
  document.getElementById('title').textContent = DATA.spec + ' — 搜索图(覆盖 + icicle)';
  const s=DATA.summary, chips=document.getElementById('chips');
  [['尝试',s.attempted],['优化成功',s.accepted],['跳过',s.skipped],['进化了','快 '+(-s.realized_pct).toFixed(1)+'%'],['能进化的',s.headroom_pct.toFixed(1)+'%'],['判定',s.decision]]
    .forEach(([k,v])=>{const c=el('span','chip'); c.innerHTML=k+' <b>'+v+'</b>'; chips.appendChild(c);});
  if(s.decision) chips.lastChild.style.background = s.decision==='STOP'?'#fee2e2':'#dcfce7';
  const t=document.getElementById('tree');

  // ---- coverage bar (where the runtime goes, block width ∝ self-time%) ----
  const cap=el('div'); cap.style.cssText='font-size:12px;color:#334155;margin:0 0 6px';
  cap.innerHTML='<b>运行时覆盖</b> · 块宽 ∝ self-time% · 该负载净 <b style="color:#16a34a">快 '+(-s.realized_pct).toFixed(1)+'%</b>';
  t.appendChild(cap);
  const bar=el('div','covbar');
  (s.coverage||[]).forEach(seg=>{ if(!seg.pct||seg.pct<=0) return; const b=el('div','covseg'); b.style.flexGrow=seg.pct; b.style.background=seg.color; if(seg.hatch) b.classList.add('hatch'); b.style.color=(seg.key==='floor'||seg.key==='captured')?'#fff':'#334155'; b.style.cursor='pointer'; b.title=seg.label+' '+seg.pct+'% — 点开看详情'; b.onclick=()=>showCov(seg.key); if(seg.pct>=7) b.textContent=seg.pct+'% ▸'; bar.appendChild(b); });
  t.appendChild(bar);
  const cleg=el('div'); cleg.style.cssText='display:flex;flex-wrap:wrap;gap:10px;font-size:11px;color:#64748b;margin:6px 0 16px';
  (s.coverage||[]).forEach(seg=>{ if(!seg.pct||seg.pct<=0) return; const x=el('span'); x.style.cursor='pointer'; x.onclick=()=>showCov(seg.key); x.innerHTML='<i class="dot" style="background:'+seg.color+'"></i>'+seg.label+' '+seg.pct+'% ▸'; cleg.appendChild(x); });
  t.appendChild(cleg);

  // ---- horizontal icicle: 测试负载 → 函数(高 ∝ self-time) → 候选 ----
  const ic=el('div','icicle');
  const root=el('div','col col-root'); const rb=el('div','rootbox'); rb.innerHTML='<b>'+DATA.spec+'</b><br><span class="muted">测试负载(整体 100%)</span>'; root.appendChild(rb); ic.appendChild(root);
  const colf=el('div','col col-fns');
  const cands=el('div','col col-cands'); cands.innerHTML='<div class="muted" style="font-size:12px;padding:8px">← 点左边函数,看它试过的候选</div>';
  DATA.nodes.forEach(n=>{
    if(n.i!=null) NODES[n.i]=n;
    const stat = n.type==='skipped' ? 'skipped' : n.status;
    const blk=el('div','fnblock'); blk.style.flexGrow=Math.max(n.pct||1.2,1.2); blk.style.borderLeft='5px solid '+col(stat);
    if(n.i!=null) blk.dataset.i=n.i;
    if(n.accepted) blk.classList.add('accepted');
    blk.innerHTML='<div class="fnname"><code>'+n.fn+'</code></div><div class="fnmeta">'+(n.pct!=null?n.pct+'% · ':'')
      +'<span style="color:'+col(stat)+';font-weight:600">'+stat+(typeof n.delta==='number'?' '+dpct(n.delta):'')+'</span>'
      +(n.accepted?' ✓':'')+(n.regime&&n.regime!=='byte-identical'?' · <span style="color:#c2410c" title="动了结构,不建议直接合">需复核</span>':'')+'</div>';
    blk.onclick=()=>{ colf.querySelectorAll('.fnblock.sel').forEach(e=>e.classList.remove('sel')); blk.classList.add('sel'); fillCands(n,cands); if(n.type==='skipped') showSkip(n); else showFn(n,0); };
    colf.appendChild(blk);
  });
  ic.appendChild(colf); ic.appendChild(cands);
  t.appendChild(ic);
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
