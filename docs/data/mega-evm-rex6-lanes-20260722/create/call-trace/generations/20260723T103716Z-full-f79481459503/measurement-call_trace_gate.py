#!/usr/bin/env python3
"""Hardened Lane 1 Callgrind gate with immutable, reproducible generations."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve()
DATA_DIR = SCRIPT.parent
ARO_ROOT = Path(os.environ["LANE1_ARO_ROOT"]) if "LANE1_ARO_ROOT" in os.environ else SCRIPT.parents[5]
SOURCE_REPO = Path(os.environ.get("MEGA_EVM_ARO_REPO", "/home/mega-engineer/workspace/mega-evm-aro"))
WORKTREE_PARENT = Path(os.environ.get("LANE1_WORKTREE_PARENT", "/nvme2/mega-engineer/workspace"))
BASELINE = "245476834741de1e1a615d22e6287621b64f30cb"
TIMED_PROBE = "probes/mega_evm_rex6_create.rs"
DIFF_PROBE = "probes/mega_evm_rex6_create_diff.rs"
PKG = "mega-evm"
TIMED_EXAMPLE = "aro_rex6_lane2_create"
DIFF_EXAMPLE = "aro_rex6_lane2_create_diff"
TIMEOUT = int(os.environ.get("LANE1_CALLGRIND_TIMEOUT", "3600"))
OWNERSHIP_MARKER = ".lane1-owner"
GENERATIONS = DATA_DIR / "generations"
CURRENT = DATA_DIR / "current"
LOCK_PATH = ARO_ROOT / ".aro-runs/locks/mega-evm-rex6-lane2-create.lock"
LAST_PROCESS_GROUP: int | None = None
TARGET_PREFIX = "crates/mega-evm/src/"
EXPECTED_EDITABLE = {
    "crates/mega-evm/src/evm/host.rs",
    "crates/mega-evm/src/evm/instructions.rs",
    "crates/mega-evm/src/external/gas.rs",
    "crates/mega-evm/src/limit/compute_gas.rs",
    "crates/mega-evm/src/limit/frame_limit.rs",
    "crates/mega-evm/src/limit/kv_update.rs",
    "crates/mega-evm/src/limit/limit.rs",
}
EDITABLE_FUNCTION_POLICY = {
    "crates/mega-evm/src/evm/host.rs": [r"(?i)(?:create|new_account|inspect_account)"],
    "crates/mega-evm/src/evm/instructions.rs": [r"(?i)(?:create2?|create_rex6)"],
    "crates/mega-evm/src/external/gas.rs": [r"(?i)(?:create_contract_gas|new_account_gas)"],
    "crates/mega-evm/src/limit/compute_gas.rs": [r"(?i)(?:create|storage_gas_ext)"],
    "crates/mega-evm/src/limit/frame_limit.rs": [r"(?i)(?:before_frame_init|create|frame)"],
    "crates/mega-evm/src/limit/kv_update.rs": [r"(?i)(?:create|on_)"],
    "crates/mega-evm/src/limit/limit.rs": [r"(?i)(?:create|before_frame|on_)"],
}
POLICY_TEST_FUNCTIONS = {
    p: ("storage_gas_ext::sstore" if "compute_gas" in p or "frame_limit" in p else
        "additional_limit_ext::log" if "data_size" in p else
        "sstore_set_gas" if p.endswith("external/gas.rs") else
        "host::sload" if p.endswith("evm/host.rs") else
        "instructions::sstore" if p.endswith("evm/instructions.rs") else "AdditionalLimit::on_sstore")
    for p in EDITABLE_FUNCTION_POLICY
}
COMMON_EXCLUSION_REASONS = {
    "crates/mega-evm/src/access/tracker.rs": "generic state-access tracking support",
    "crates/mega-evm/src/access/volatile.rs": "generic volatile-state support",
    "crates/mega-evm/src/evm/context.rs": "generic transaction context/lifecycle plumbing",
    "crates/mega-evm/src/evm/execution.rs": "generic EVM execution orchestration",
    "crates/mega-evm/src/evm/interfaces.rs": "probe-facing execution interface",
    "crates/mega-evm/src/evm/limit.rs": "generic limit-check integration glue",
    "crates/mega-evm/src/evm/mod.rs": "module/dispatch and inlining attribution",
    "crates/mega-evm/src/evm/precompiles.rs": "generic precompile lifecycle setup",
    "crates/mega-evm/src/evm/spec.rs": "generic EVM spec/trait dispatch attribution",
    "crates/mega-evm/src/limit/mod.rs": "limit module glue/re-export attribution",
    "crates/mega-evm/src/limit/storage_call_stipend.rs": "no CALL opcode; shared lifecycle attribution",
    "crates/mega-evm/src/system/intercept.rs": "generic system-transaction lifecycle",
    "crates/mega-evm/src/system/tx.rs": "generic transaction lifecycle",
    "crates/mega-evm/src/limit/data_size.rs": "no CREATE-specific op evidence; lifecycle attribution only",
    "crates/mega-evm/src/limit/state_growth.rs": "no CREATE-specific op evidence; lifecycle attribution only",
}
SOURCE_FILES = (
    "timed.callgrind.out", "differential.callgrind.out", "timed.stdout", "timed.stderr",
    "differential.stdout", "differential.stderr", "commands.json", "tool-fingerprint.json",
    "build-aro_rex6_lane2_create.jsonl", "build-aro_rex6_lane2_create.stderr",
    "build-aro_rex6_lane2_create_diff.jsonl", "build-aro_rex6_lane2_create_diff.stderr",
    "measurement-call_trace_gate.py", "probe-timed.rs", "probe-differential.rs",
)
LEGACY_GENERATED = set(SOURCE_FILES) | {
    "SHA256SUMS", "timed.calltree.txt", "differential.calltree.txt", "timed.reached.json",
    "differential.reached.json", "intersection.json", "proposed-editable.json", "run-metadata.json",
    "commands.json", "tool-fingerprint.json", "test_call_trace_gate.py",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""): h.update(b)
    return h.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def lexists(path: Path) -> bool:
    return os.path.lexists(path)


def claim_absent_path(path: Path, marker: str) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError(f"refusing path collision (including symlink): {path}")
    path.mkdir(parents=False)
    marker_path = path / OWNERSHIP_MARKER
    fd = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f: f.write(marker)


def verify_owned(path: Path, marker: str) -> None:
    st = path.lstat()
    if not os.path.isdir(path) or os.path.islink(path):
        raise RuntimeError(f"owned path changed type: {path}")
    mp = path / OWNERSHIP_MARKER
    try:
        mst = mp.lstat()
    except FileNotFoundError as e:
        raise RuntimeError(f"ownership marker missing: {path}") from e
    if os.path.islink(mp) or not os.path.isfile(mp) or mp.read_text() != marker:
        raise RuntimeError(f"ownership marker mismatch: {path}")


def remove_owned(path: Path, marker: str) -> None:
    if not lexists(path): return
    verify_owned(path, marker)
    shutil.rmtree(path)
    if lexists(path): raise RuntimeError(f"owned path survived removal: {path}")


def live_group_members(pgid: int | None) -> list[int]:
    if pgid is None: return []
    out=[]
    for p in Path("/proc").iterdir():
        if not p.name.isdigit(): continue
        try:
            fields=(p/"stat").read_text().split()
            if int(fields[4]) == pgid: out.append(int(p.name))
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError): pass
    return sorted(out)


def run_process(command: list[str], *, cwd: Path | None = None, env: dict[str,str] | None = None,
                timeout: float = TIMEOUT, check: bool = True) -> subprocess.CompletedProcess[str]:
    global LAST_PROCESS_GROUP
    proc = subprocess.Popen(command, cwd=str(cwd) if cwd else None, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    LAST_PROCESS_GROUP = proc.pid
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        for sig, grace in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
            try: os.killpg(proc.pid, sig)
            except ProcessLookupError: pass
            try:
                stdout, stderr = proc.communicate(timeout=grace)
                break
            except subprocess.TimeoutExpired: continue
        else:
            proc.kill(); stdout, stderr = proc.communicate()
        survivors = live_group_members(proc.pid)
        if survivors: raise RuntimeError(f"process group survived timeout: {survivors}") from exc
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
    completed=subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
    if check and proc.returncode: raise subprocess.CalledProcessError(proc.returncode, command, stdout, stderr)
    return completed


def output(command: list[str], *, cwd: Path | None=None, env: dict[str,str] | None=None, timeout=120) -> str:
    return run_process(command,cwd=cwd,env=env,timeout=timeout).stdout.strip()


def active_heavy_processes() -> list[dict[str,Any]]:
    heavy={"cargo","rustc","valgrind","callgrind_annotate"}; found=[]
    for p in Path("/proc").iterdir():
        if not p.name.isdigit() or int(p.name)==os.getpid(): continue
        try:
            if p.stat().st_uid != os.getuid(): continue
            comm=(p/"comm").read_text().strip()
            if comm in heavy: found.append({"pid":int(p.name),"comm":comm,"cmdline":(p/"cmdline").read_bytes().replace(b"\0",b" ").decode(errors="replace")})
        except (FileNotFoundError,ProcessLookupError,PermissionError): pass
    return found

def build_spec(name: str):
    from aro.spec import Goal, Stop, TargetSpec
    return TargetSpec(name=name, repo=SOURCE_REPO, baseline_ref=BASELINE, build=[], test=[],
        bench={"probe":TIMED_PROBE,"example":TIMED_EXAMPLE,"pkg":PKG,"sample_prefix":"BENCH","metric":"ns_per_logical_workload"},
        profile={},regions=["crates/mega-evm"],context={},objectives=[{"metric":"ns_per_logical_workload","minimize":True}],
        goal=Goal("ns_per_logical_workload"),stop=Stop(),prompts={},
        differential={"probe":DIFF_PROBE,"pkg":PKG,"example":DIFF_EXAMPLE,"prefix":"DIFF"},timeout=TIMEOUT)

def cargo_build(target, worktree: Path, example: str, commands: list, stage: Path) -> Path:
    env=target.env_for(worktree,measurement_kind="icount"); env["CARGO_BUILD_JOBS"]="1"
    cmd=["cargo","build","--release","-p",PKG,"--example",example,"--message-format=json"]
    commands.append({"name":f"build-{example}","cwd":str(worktree),"argv":cmd,"env":{k:env.get(k) for k in ("CARGO_TARGET_DIR","CARGO_PROFILE_RELEASE_DEBUG","CARGO_PROFILE_RELEASE_STRIP","CARGO_BUILD_JOBS","RAYON_NUM_THREADS")}})
    done=run_process(cmd,cwd=worktree,env=env)
    (stage/f"build-{example}.jsonl").write_text(done.stdout); (stage/f"build-{example}.stderr").write_text(done.stderr)
    executable=None
    for line in done.stdout.splitlines():
        try: msg=json.loads(line)
        except ValueError: continue
        ti=msg.get("target",{})
        if msg.get("reason")=="compiler-artifact" and msg.get("executable") and ti.get("name")==example and "example" in (ti.get("kind") or []): executable=Path(msg["executable"])
    if executable is None or not executable.is_file(): raise RuntimeError(f"cargo executable missing: {example}")
    return executable

def run_trace(label: str, binary: Path, stage: Path, valgrind: Path, worktree: Path, target, commands: list) -> None:
    env=target.env_for(worktree,measurement_kind="icount"); env.update({"ARO_BENCH_SCALE":"1","RAYON_NUM_THREADS":"1","VALGRIND_LIB":str(Path.home()/".local/libexec/valgrind")})
    raw=stage/f"{label}.callgrind.out"; cmd=[str(valgrind),"--tool=callgrind","--collect-atstart=yes","--compress-strings=yes","--compress-pos=yes",f"--callgrind-out-file={raw}",str(binary)]
    commands.append({"name":f"trace-{label}","cwd":str(worktree),"argv":cmd,"env":{k:env.get(k) for k in ("ARO_BENCH_SCALE","RAYON_NUM_THREADS","VALGRIND_LIB","CARGO_TARGET_DIR")}})
    done=run_process(cmd,cwd=worktree,env=env); (stage/f"{label}.stdout").write_text(done.stdout); (stage/f"{label}.stderr").write_text(done.stderr)
    if not raw.is_file() or raw.stat().st_size==0: raise RuntimeError(f"{label}: empty Callgrind output")

def annotate_trace(label: str, stage: Path, annotate: Path, commands: list) -> None:
    env=dict(os.environ); env["VALGRIND_LIB"]=str(Path.home()/".local/libexec/valgrind")
    cmd=[str(annotate),"--show=Ir","--inclusive=yes","--tree=both","--threshold=0.1",str(stage/f"{label}.callgrind.out")]
    commands.append({"name":f"annotate-{label}","cwd":str(stage),"argv":cmd,"env":{"VALGRIND_LIB":env["VALGRIND_LIB"]}})
    done=run_process(cmd,cwd=stage,env=env); (stage/f"{label}.calltree.txt").write_text(done.stdout)
    if not done.stdout.strip(): raise RuntimeError(f"{label}: empty annotation")

def tool_fingerprint(valgrind: Path, annotate: Path, worktree: Path) -> dict[str,Any]:
    cargo=Path(output(["sh","-c","command -v cargo"],cwd=worktree)); rustc=Path(output(["sh","-c","command -v rustc"],cwd=worktree))
    return {"cargo":{"path":str(cargo),"sha256":sha256(cargo),"version":output([str(cargo),"-V"],cwd=worktree)},
            "rustc":{"path":str(rustc),"sha256":sha256(rustc),"version_verbose":output([str(rustc),"-Vv"],cwd=worktree)},
            "valgrind":{"path":str(valgrind),"sha256":sha256(valgrind),"version":output([str(valgrind),"--version"])},
            "callgrind_annotate":{"path":str(annotate),"sha256":sha256(annotate)},"python":sys.version,"git":output(["git","--version"])}


def _mapping(raw: str, table: dict[str,str], line_no: int, kind: str) -> str:
    token=raw.strip()
    m=re.fullmatch(r"\(([A-Za-z0-9]+)\)(?:\s+(.+))?",token)
    if token.startswith("(") and not m: raise RuntimeError(f"line {line_no}: malformed {kind} compressed ID")
    if not m:
        if not token: raise RuntimeError(f"line {line_no}: empty {kind}")
        return token
    key,value=m.groups()
    if value is not None:
        if key in table and table[key] != value: raise RuntimeError(f"line {line_no}: redefined {kind} ID {key}")
        table[key]=value; return value
    if key not in table: raise RuntimeError(f"line {line_no}: undefined {kind} ID {key}")
    return table[key]


def normalize_target_source(raw: str, detached_worktree: Path, baseline_sources: set[str]) -> str:
    if "\\" in raw or "\x00" in raw: raise RuntimeError(f"invalid debug source path: {raw!r}")
    if raw.startswith(TARGET_PREFIX): normalized=raw
    elif raw.startswith("/"):
        root=str(detached_worktree)
        prefix=root.rstrip("/")+"/"
        if not raw.startswith(prefix): raise RuntimeError(f"absolute debug source outside detached worktree: {raw}")
        normalized=raw[len(prefix):]
    else: raise RuntimeError(f"debug source is not anchored target source: {raw}")
    if not re.fullmatch(r"crates/mega-evm/src/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.rs",normalized):
        raise RuntimeError(f"invalid normalized source path: {normalized}")
    if normalized not in baseline_sources: raise RuntimeError(f"source absent from baseline tree: {normalized}")
    return normalized


def parse_callgrind(path: Path, detached_worktree: Path, baseline_sources: set[str]) -> dict[str,Any]:
    events=None; positions=None; summary=None; file_ids={}; fn_ids={}
    current_file=current_fn=None; callee_file=callee_fn=None; pending=None
    evidence=defaultdict(lambda:{"self_ir":0,"called_ir":0,"call_count":0,"locations":set()})
    for no, raw in enumerate(path.read_text(errors="strict").splitlines(),1):
        line=raw.strip()
        if not line or line.startswith("#"): continue
        if line.startswith("positions:"):
            if positions is not None: raise RuntimeError(f"line {no}: duplicate positions")
            positions=line.split(":",1)[1].split()
            if not positions: raise RuntimeError(f"line {no}: empty positions schema")
        elif line.startswith("events:"):
            if events is not None: raise RuntimeError(f"line {no}: duplicate events")
            events=line.split(":",1)[1].split()
            if not events or len(events)!=len(set(events)) or "Ir" not in events: raise RuntimeError(f"line {no}: invalid event schema {events}")
        elif line.startswith("summary:"):
            if events is None: raise RuntimeError(f"line {no}: summary before events")
            toks=line.split(":",1)[1].split()
            if len(toks)!=len(events) or any(not re.fullmatch(r"\d+",x) for x in toks): raise RuntimeError(f"line {no}: malformed summary")
            summary=[int(x) for x in toks]
        elif re.match(r"^(?:fl|fi|fe)=",line):
            if pending is not None: raise RuntimeError(f"line {no}: dangling calls before file change")
            current_file=_mapping(line.split("=",1)[1],file_ids,no,"file"); callee_file=callee_fn=None
        elif line.startswith("fn="):
            if pending is not None: raise RuntimeError(f"line {no}: dangling calls before function change")
            current_fn=_mapping(line[3:],fn_ids,no,"function"); callee_file=callee_fn=None
        elif re.match(r"^(?:cfl|cfi)=",line):
            if pending is not None: raise RuntimeError(f"line {no}: duplicate/dangling call state")
            callee_file=_mapping(line.split("=",1)[1],file_ids,no,"callee file")
        elif line.startswith("cfn="):
            if pending is not None: raise RuntimeError(f"line {no}: duplicate/dangling call state")
            callee_fn=_mapping(line[4:],fn_ids,no,"callee function")
        elif line.startswith("calls="):
            if pending is not None or callee_fn is None or current_file is None or current_fn is None: raise RuntimeError(f"line {no}: invalid calls state")
            if positions is None: raise RuntimeError(f"line {no}: calls before positions")
            toks=line[6:].split()
            if len(toks)!=1+len(positions) or not re.fullmatch(r"\d+",toks[0]) or int(toks[0])<=0: raise RuntimeError(f"line {no}: malformed calls record")
            if any(not re.fullmatch(r"(?:\*|[+-]?\d+)",x) for x in toks[1:]): raise RuntimeError(f"line {no}: malformed calls position")
            pending=int(toks[0])
        elif re.match(r"^(?:\*|[+-]?\d+)(?:\s|$)",line):
            if events is None or positions is None or current_file is None or current_fn is None: raise RuntimeError(f"line {no}: cost before complete schema/context")
            toks=line.split(); needed=len(positions)+len(events)
            if len(toks)!=needed: raise RuntimeError(f"line {no}: malformed cost field count")
            if any(not re.fullmatch(r"(?:\*|[+-]?\d+)",x) for x in toks[:len(positions)]): raise RuntimeError(f"line {no}: malformed cost position")
            vals=[]
            for x in toks[len(positions):]:
                if x=="*": vals.append(0)
                elif re.fullmatch(r"\d+",x): vals.append(int(x))
                else: raise RuntimeError(f"line {no}: malformed/negative event cost")
            ir=vals[events.index("Ir")]; location=" ".join(toks[:len(positions)])
            if pending is not None:
                raw_source=callee_file or current_file
                try: source=normalize_target_source(raw_source,detached_worktree,baseline_sources)
                except RuntimeError:
                    target_absolute=str(detached_worktree).rstrip("/")+"/"+TARGET_PREFIX
                    if raw_source.startswith(TARGET_PREFIX) or raw_source.startswith(target_absolute): raise
                    pending=None
                    continue
                key=(source,callee_fn); evidence[key]["called_ir"]+=ir; evidence[key]["call_count"]+=pending; evidence[key]["locations"].add(location); pending=None
            else:
                try: source=normalize_target_source(current_file,detached_worktree,baseline_sources)
                except RuntimeError:
                    # Non-target debug records are irrelevant, but target-looking malformed paths fail closed.
                    target_absolute=str(detached_worktree).rstrip("/")+"/"+TARGET_PREFIX
                    if current_file.startswith(TARGET_PREFIX) or current_file.startswith(target_absolute): raise
                    continue
                key=(source,current_fn); evidence[key]["self_ir"]+=ir; evidence[key]["locations"].add(location)
        elif re.match(r"^(?:ob|cob)=",line) or ":" in line:
            if pending is not None and line.startswith("cob="): raise RuntimeError(f"line {no}: dangling calls before object change")
            continue
        else: raise RuntimeError(f"line {no}: unrecognized Callgrind record: {line[:80]}")
    if pending is not None: raise RuntimeError("dangling calls record at EOF")
    if events is None or summary is None: raise RuntimeError("missing events/summary")
    total=summary[events.index("Ir")]
    if total<=0: raise RuntimeError("missing/non-positive summary Ir")
    rows=[{"file":f,"function":fn,"self_ir":v["self_ir"],"called_ir":v["called_ir"],"call_count":v["call_count"],"locations":sorted(v["locations"])} for (f,fn),v in sorted(evidence.items()) if v["self_ir"]>0 or v["called_ir"]>0]
    files=sorted({r["file"] for r in rows})
    if not rows: raise RuntimeError("no positive-Ir target-owned source/function mapping")
    return {"schema_version":2,"raw_file":path.name,"events":events,"summary_ir":total,"reached_files":files,"evidence":rows,
            "called_ir_semantics":"inclusive call-edge Ir; the same instructions may be charged on multiple caller edges, so summed called_ir can exceed summary Ir"}


def _operation_evidence(parsed: dict[str,Any], path: str) -> list[dict[str,Any]]:
    regexes=[re.compile(x) for x in EDITABLE_FUNCTION_POLICY[path]]
    return [{"function":r["function"],"self_ir":r["self_ir"],"called_ir":r["called_ir"],"regex":next(x.pattern for x in regexes if x.search(r["function"]))}
            for r in parsed["evidence"] if r["file"]==path and r["self_ir"]+r["called_ir"]>0 and any(x.search(r["function"]) for x in regexes)]


def derive_selection(timed: dict[str,Any], differential: dict[str,Any]):
    common=sorted(set(timed["reached_files"]) & set(differential["reached_files"])); tonly=sorted(set(timed["reached_files"])-set(common)); donly=sorted(set(differential["reached_files"])-set(common))
    if not common: raise RuntimeError("empty dynamic intersection")
    per={}; editable=[]
    for p in sorted(EDITABLE_FUNCTION_POLICY):
        if p not in common: continue
        te=_operation_evidence(timed,p); de=_operation_evidence(differential,p)
        if not te or not de:
            print(f"SKIP_NO_OP_EVIDENCE {p} te={bool(te)} de={bool(de)}", flush=True)
            continue
        editable.append(p); per[p]={"policy_regex":EDITABLE_FUNCTION_POLICY[p],"timed":te,"differential":de}
    if set(editable)!=EXPECTED_EDITABLE:
        if os.environ.get("LANE2_DISCOVER_EDITABLE") == "1":
            print("DISCOVER_EDITABLE", sorted(editable), flush=True)
            EXPECTED_EDITABLE.clear(); EXPECTED_EDITABLE.update(editable)
        else:
            raise RuntimeError(f"final editable set mismatch: got={sorted(editable)} expected={sorted(EXPECTED_EDITABLE)}")
    excluded=sorted(set(common)-set(editable)); missing=sorted(set(excluded)-set(COMMON_EXCLUSION_REASONS))
    if missing: raise RuntimeError(f"missing exclusion reasons: {missing}")
    intersection={"schema_version":2,"timed_summary_ir":timed["summary_ir"],"differential_summary_ir":differential["summary_ir"],"common_files":common,"timed_only_files":tonly,"differential_only_files":donly}
    proposed={"schema_version":2,"gate":"call-trace/editable-intersection","gate_passed":True,"mutation_gate_passed":False,"mutation_status":"NOT RUN; target spec creation remains blocked","selection_rule":"dynamic common baseline-owned files; every editable requires explicit per-file operation-function regex evidence with positive Ir in both traces","proposed_editable":editable,"per_file_operation_evidence":per,"common_harness_or_lifecycle_excluded":excluded,"common_exclusion_reasons":{p:COMMON_EXCLUSION_REASONS[p] for p in excluded},"one_sided_timed_excluded":tonly,"one_sided_differential_excluded":donly}
    return intersection,proposed


def baseline_sources() -> set[str]:
    text=output(["git","-C",str(SOURCE_REPO),"ls-tree","-r","--name-only",BASELINE,"--","crates/mega-evm/src"])
    result={x for x in text.splitlines() if x.endswith(".rs")}
    if not result: raise RuntimeError("baseline source set is empty")
    return result


def generation_manifest(stage: Path) -> None:
    files=[p for p in stage.rglob("*") if p.is_file() and p.name!="SHA256SUMS"]
    (stage/"SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.relative_to(stage)}\n" for p in sorted(files)))


def verify_manifest(generation: Path, name="SHA256SUMS") -> dict[str,str]:
    result={}
    for line in (generation/name).read_text().splitlines():
        expected,rel=line.split("  ",1); p=generation/rel
        if not p.is_file() or p.is_symlink() or sha256(p)!=expected: raise RuntimeError(f"manifest mismatch: {rel}")
        result[rel]=expected
    return result


def publish(stage: Path, generation_id: str) -> Path:
    GENERATIONS.mkdir(parents=True,exist_ok=True)
    final=GENERATIONS/generation_id
    if lexists(final): raise RuntimeError(f"generation collision: {generation_id}")
    for p in sorted(stage.rglob("*"), reverse=True):
        if p.is_file(): p.chmod(0o444)
        elif p.is_dir(): p.chmod(0o555)
    os.replace(stage,final)
    final.chmod(0o555)
    tmp=DATA_DIR/f".current-{uuid.uuid4().hex}"
    os.symlink(f"generations/{generation_id}",tmp)
    os.replace(tmp,CURRENT)
    return final


def lock_stream():
    LOCK_PATH.parent.mkdir(parents=True,exist_ok=True)
    return LOCK_PATH.open("a+")


def resolve_source(spec: str | None) -> Path:
    if spec is None: p=CURRENT
    else: p=GENERATIONS/spec
    resolved=p.resolve(strict=True)
    if resolved.parent != GENERATIONS.resolve() or resolved.is_symlink(): raise RuntimeError("source generation escapes generations root")
    return resolved


def validate_source_manifest(source: Path) -> dict[str,str]:
    manifest=json.loads((source/"source-run-manifest.json").read_text())
    if manifest.get("schema_version")!=1 or set(manifest.get("artifacts",{}))!=set(SOURCE_FILES): raise RuntimeError("invalid source-run-manifest schema/artifact set")
    for rel,expected in manifest["artifacts"].items():
        p=source/rel
        if not p.is_file() or p.is_symlink() or sha256(p)!=expected: raise RuntimeError(f"source-run-manifest mismatch: {rel}")
    return manifest["artifacts"]


def make_id(kind: str) -> str:
    return f"{time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())}-{kind}-{uuid.uuid4().hex[:12]}"


def migrate_legacy() -> dict[str,Any]:
    with lock_stream() as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if GENERATIONS.exists() and any(GENERATIONS.iterdir()): raise RuntimeError("generations already exist")
        staging=Path(tempfile.mkdtemp(prefix="lane1-migrate-",dir=DATA_DIR)); gid=make_id("source"); stage=staging/gid; stage.mkdir()
        try:
            # Preserve the exact successful measurement runner separately from the mutable top-level runner.
            measurement=DATA_DIR/"measurement-call_trace_gate.py"
            required=[x for x in SOURCE_FILES if x not in {"measurement-call_trace_gate.py","probe-timed.rs","probe-differential.rs"}]
            for name in required: shutil.copy2(DATA_DIR/name,stage/name)
            shutil.copy2(measurement,stage/"measurement-call_trace_gate.py")
            shutil.copy2(ARO_ROOT/TIMED_PROBE,stage/"probe-timed.rs"); shutil.copy2(ARO_ROOT/DIFF_PROBE,stage/"probe-differential.rs")
            oldmeta=json.loads((DATA_DIR/"run-metadata.json").read_text())
            oldcommands=json.loads((DATA_DIR/"commands.json").read_text())
            build_commands=[x for x in oldcommands if str(x.get("name","")).startswith("build-")]
            if not build_commands or len({x.get("cwd") for x in build_commands}) != 1:
                raise RuntimeError("cannot recover unique recorded detached worktree from commands")
            detached=build_commands[0]["cwd"]
            source_manifest={"schema_version":1,"kind":"immutable-full-run-inputs","baseline":BASELINE,"recorded_detached_worktree":detached,"artifacts":{n:sha256(stage/n) for n in SOURCE_FILES},"excludes_mutable_derived":["*.reached.json","intersection.json","proposed-editable.json","gate-summary.json","run-metadata.json","SHA256SUMS"]}
            write_json(stage/"source-run-manifest.json",source_manifest)
            shutil.copy2(SCRIPT,stage/"call_trace_gate.py"); shutil.copy2(DATA_DIR/"test_call_trace_gate.py",stage/"test_call_trace_gate.py")
            write_json(stage/"gate-summary.json",{"status":"source-preserved","generation_id":gid,"raw_sha256":{n:sha256(stage/n) for n in ("timed.callgrind.out","differential.callgrind.out")},"timed_ir":19604714,"differential_ir":7737299,"called_ir_note":"called_ir is inclusive call-edge cost and may exceed summary Ir when summed across edges"})
            generation_manifest(stage); final=publish(stage,gid)
        finally:
            if staging.exists(): shutil.rmtree(staging)
        # Only after immutable publication, remove legacy generated artifacts; runner and generations remain.
        for name in LEGACY_GENERATED:
            p=DATA_DIR/name
            if p.name=="call_trace_gate.py" or not lexists(p): continue
            if p.is_dir() and not p.is_symlink(): shutil.rmtree(p)
            else: p.unlink()
        py=DATA_DIR/"__pycache__"
        if lexists(py): shutil.rmtree(py)
        return {"status":"passed","mode":"migration","generation_id":gid,"generation_sha256":sha256(final/"SHA256SUMS")}


def reparse_existing(source_spec: str | None=None) -> dict[str,Any]:
    with lock_stream() as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        source=resolve_source(source_spec); source_artifacts=validate_source_manifest(source); verify_manifest(source)
        sm=json.loads((source/"source-run-manifest.json").read_text()); worktree=Path(sm["recorded_detached_worktree"]); sources=baseline_sources()
        gid=make_id("reparse"); staging_root=ARO_ROOT/".aro-runs/staging"; staging_root.mkdir(parents=True,exist_ok=True); stage=Path(tempfile.mkdtemp(prefix="lane1-reparse-",dir=staging_root))
        try:
            for name in SOURCE_FILES: shutil.copy2(source/name,stage/name)
            shutil.copy2(source/"source-run-manifest.json",stage/"source-run-manifest.json")
            shutil.copy2(SCRIPT,stage/"call_trace_gate.py"); shutil.copy2(source/"test_call_trace_gate.py",stage/"test_call_trace_gate.py")
            timed=parse_callgrind(stage/"timed.callgrind.out",worktree,sources); differential=parse_callgrind(stage/"differential.callgrind.out",worktree,sources)
            if (timed["summary_ir"],differential["summary_ir"])!=(19604714,7737299): raise RuntimeError("preserved Ir mismatch")
            intersection,proposed=derive_selection(timed,differential)
            write_json(stage/"timed.reached.json",timed); write_json(stage/"differential.reached.json",differential); write_json(stage/"intersection.json",intersection); write_json(stage/"proposed-editable.json",proposed)
            preexisting=[x for x in output(["git","-C",str(SOURCE_REPO),"status","--short","--untracked-files=all"]).splitlines() if x.startswith("?? ")]
            metadata={"schema_version":2,"status":"passed","mode":"selection-only-reparse","generation_id":gid,"source_generation_id":source.name,"source_run_manifest_sha256":sha256(source/"source-run-manifest.json"),"measurement_rerun":False,"baseline":BASELINE,"recorded_detached_worktree":str(worktree),"baseline_source_count":len(sources),"timed_ir":timed["summary_ir"],"differential_ir":differential["summary_ir"],"proposed_editable":proposed["proposed_editable"],"raw_trace_sha256":{n:source_artifacts[n] for n in ("timed.callgrind.out","differential.callgrind.out")},"called_ir_semantics":"inclusive edge cost; a callee's Ir is charged on each represented caller edge, so summing called_ir is not an exclusive total and can exceed summary Ir","pre_existing_unrelated_target_untracked_items":preexisting}
            write_json(stage/"run-metadata.json",metadata)
            write_json(stage/"gate-summary.json",{"schema_version":1,"status":"passed","generation_id":gid,"source_generation_id":source.name,"timed_ir":timed["summary_ir"],"differential_ir":differential["summary_ir"],"common_file_count":len(intersection["common_files"]),"proposed_editable":proposed["proposed_editable"],"raw_trace_sha256":metadata["raw_trace_sha256"],"integrity_boundary":"this generation; external Markdown is narrative only"})
            generation_manifest(stage); final=publish(stage,gid)
        except BaseException:
            if stage.exists():
                for p in [stage, *stage.rglob("*")]:
                    try:
                        if p.is_dir() and not p.is_symlink(): p.chmod(0o755)
                        elif p.is_file(): p.chmod(0o644)
                    except FileNotFoundError: pass
                shutil.rmtree(stage)
            raise
        py=DATA_DIR/"__pycache__"
        if lexists(py): shutil.rmtree(py)
        return {"status":"passed","mode":"selection-only-reparse","generation_id":gid,"source_generation_id":source.name,"generation_manifest_sha256":sha256(final/"SHA256SUMS"),"timed_ir":timed["summary_ir"],"differential_ir":differential["summary_ir"],"proposed_editable":proposed["proposed_editable"]}


def execute() -> dict[str,Any]:
    """Perform a new full measurement using only owned paths and process groups."""
    with lock_stream() as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if active_heavy_processes(): raise RuntimeError("host is not quiet")
        marker=uuid.uuid4().hex+uuid.uuid4().hex
        reservation=Path(tempfile.mkdtemp(prefix="lane1-owned-",dir=WORKTREE_PARENT)); (reservation/OWNERSHIP_MARKER).write_text(marker)
        worktree=reservation/"worktree"  # deliberately absent child for git worktree add
        unique=uuid.uuid4().hex
        spec_name=f"rex6-lane1-calltrace-{unique}"
        target_root=SOURCE_REPO.parent/f".aro-{spec_name}-td"
        staging_root=ARO_ROOT/".aro-runs/staging"; staging_root.mkdir(parents=True,exist_ok=True)
        stage=None; target=None; original=None; cleanup_errors=[]; result=None; commands=[]
        preexisting=[x for x in output(["git","-C",str(SOURCE_REPO),"status","--short","--untracked-files=all"]).splitlines() if x.startswith("?? ")]
        try:
            if lexists(worktree): raise RuntimeError("reserved worktree child unexpectedly exists")
            claim_absent_path(target_root,marker)
            stage=Path(tempfile.mkdtemp(prefix="lane1-full-",dir=staging_root))
            from aro.target import SpecTarget
            target=SpecTarget(build_spec(spec_name))
            add=["git","-C",str(SOURCE_REPO),"worktree","add","--detach",str(worktree),BASELINE]; commands.append({"name":"worktree-add","cwd":str(ARO_ROOT),"argv":add}); run_process(add)
            sub=["git","-C",str(worktree),"submodule","update","--init","--recursive"]; commands.append({"name":"submodule-init","cwd":str(ARO_ROOT),"argv":sub}); run_process(sub)
            if output(["git","-C",str(worktree),"rev-parse","HEAD"])!=BASELINE: raise RuntimeError("detached baseline mismatch")
            target.write_probe(worktree,PKG,TIMED_EXAMPLE); pkg=target.pkg_dir(worktree,PKG); diff=pkg/"examples"/f"{DIFF_EXAMPLE}.rs"; diff.parent.mkdir(parents=True,exist_ok=True); diff.write_text(target.spec.diff_probe_src())
            valgrind=Path.home()/".local/bin/valgrind"; annotate=Path.home()/".local/bin/callgrind_annotate"
            tools=tool_fingerprint(valgrind,annotate,worktree)
            timed_bin=cargo_build(target,worktree,TIMED_EXAMPLE,commands,stage); diff_bin=cargo_build(target,worktree,DIFF_EXAMPLE,commands,stage)
            run_trace("timed",timed_bin,stage,valgrind,worktree,target,commands); run_trace("differential",diff_bin,stage,valgrind,worktree,target,commands)
            annotate_trace("timed",stage,annotate,commands); annotate_trace("differential",stage,annotate,commands)
            sources=baseline_sources(); timed=parse_callgrind(stage/"timed.callgrind.out",worktree,sources); differential=parse_callgrind(stage/"differential.callgrind.out",worktree,sources); intersection,proposed=derive_selection(timed,differential)
            for n,v in (("timed.reached.json",timed),("differential.reached.json",differential),("intersection.json",intersection),("proposed-editable.json",proposed),("commands.json",commands),("tool-fingerprint.json",tools)): write_json(stage/n,v)
            shutil.copy2(SCRIPT,stage/"measurement-call_trace_gate.py"); shutil.copy2(ARO_ROOT/TIMED_PROBE,stage/"probe-timed.rs"); shutil.copy2(ARO_ROOT/DIFF_PROBE,stage/"probe-differential.rs")
            result={"schema_version":2,"status":"passed","mode":"full-run","baseline":BASELINE,"recorded_detached_worktree":str(worktree),"timed_ir":timed["summary_ir"],"differential_ir":differential["summary_ir"],"proposed_editable":proposed["proposed_editable"],"pre_existing_unrelated_target_untracked_items":preexisting}
        except BaseException as e: original=e
        finally:
            try:
                registrations=output(["git","-C",str(SOURCE_REPO),"worktree","list","--porcelain"])
                if str(worktree) in registrations: run_process(["git","-C",str(SOURCE_REPO),"worktree","remove","--force",str(worktree)])
            except BaseException as e: cleanup_errors.append(str(e))
            try: run_process(["git","-C",str(SOURCE_REPO),"worktree","prune"])
            except BaseException as e: cleanup_errors.append(str(e))
            try: remove_owned(target_root,marker)
            except BaseException as e: cleanup_errors.append(str(e))
            try: remove_owned(reservation,marker)
            except BaseException as e: cleanup_errors.append(str(e))
            survivors=active_heavy_processes()
            if survivors: cleanup_errors.append(f"heavy survivors: {survivors}")
            if original is not None or cleanup_errors:
                if stage and stage.exists():
                    try: shutil.rmtree(stage)
                    except BaseException as e: cleanup_errors.append(str(e))
        if cleanup_errors: raise RuntimeError("cleanup failed: "+"; ".join(cleanup_errors)) from original
        if original is not None: raise original
        assert stage is not None and result is not None
        result["cleanup"]={"worktree_absent":not lexists(worktree),"target_root_absent":not lexists(target_root),"reservation_absent":not lexists(reservation),"host_quiet_after":not active_heavy_processes()}
        write_json(stage/"run-metadata.json",result)
        source_manifest={"schema_version":1,"kind":"immutable-full-run-inputs","baseline":BASELINE,"recorded_detached_worktree":str(worktree),"artifacts":{n:sha256(stage/n) for n in SOURCE_FILES},"excludes_mutable_derived":["*.reached.json","intersection.json","proposed-editable.json","gate-summary.json","run-metadata.json","SHA256SUMS"]}; write_json(stage/"source-run-manifest.json",source_manifest)
        shutil.copy2(SCRIPT,stage/"call_trace_gate.py"); test_source=(CURRENT.resolve()/"test_call_trace_gate.py") if CURRENT.exists() else DATA_DIR/"test_call_trace_gate.py"; shutil.copy2(test_source,stage/"test_call_trace_gate.py")
        gid=make_id("full"); result["generation_id"]=gid; write_json(stage/"run-metadata.json",result); write_json(stage/"gate-summary.json",{"status":"passed","generation_id":gid,"timed_ir":result["timed_ir"],"differential_ir":result["differential_ir"],"proposed_editable":result["proposed_editable"]}); generation_manifest(stage); final=publish(stage,gid)
        return result|{"generation_manifest_sha256":sha256(final/"SHA256SUMS")}


def main():
    ap=argparse.ArgumentParser(); group=ap.add_mutually_exclusive_group()
    group.add_argument("--migrate-legacy",action="store_true"); group.add_argument("--reparse-existing",action="store_true"); group.add_argument("--self-test-parser",action="store_true")
    ap.add_argument("--source-generation")
    args=ap.parse_args()
    if args.self_test_parser:
        test_path=(CURRENT.resolve(strict=True)/"test_call_trace_gate.py") if CURRENT.exists() else DATA_DIR/"test_call_trace_gate.py"
        run_process([sys.executable,str(test_path)]); print("call-trace parser/security selftests: OK"); return
    if args.migrate_legacy: result=migrate_legacy()
    elif args.reparse_existing: result=reparse_existing(args.source_generation)
    else: result=execute()
    print(json.dumps(result,indent=2,sort_keys=True))

if __name__=="__main__": main()
