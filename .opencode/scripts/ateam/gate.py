"""Hardened task gates: fingerprints, task-hash lock, test guard, four-state results."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import frontmatter as fm
from . import quality as Q
from . import schemas
from .common import (
    atomic_write,
    ateam_dir,
    die,
    emit,
    file_lock,
    find_root,
    load_config,
    now_iso,
    read_json,
    sha256_file,
    sha256_text,
    write_json,
)
from . import work as W

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", "target", ".next", ".cache", ".pytest-lib", ".idea", ".vscode", "coverage", ".tox",
}
BIG_FILE = 5 * 1024 * 1024
TEST_FILE_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*\.py$|_test\.(py|go|rs)$|\.(test|spec)\.[cm]?[jt]sx?$|(^|/)[^/]*Tests?\.(java|kt|cs)$"
)
TEST_PATTERNS = [
    re.compile(r"^\s*(?:async\s+)?def test_", re.M),
    re.compile(r"\b(?:it|test)(?:\.\w+)?\s*\(\s*[\"'`]"),
    re.compile(r"^func Test\w+", re.M),
    re.compile(r"#\[test\]"),
    re.compile(r"@Test\b"),
]
ASSERT_PATTERN = re.compile(r"\bassert\w*\b|\bexpect\s*\(|\.should\b|\bt\.(?:Error|Fatal)\w*\(|\bassert_(?:eq|ne)!|\bself\.assert\w+")
SKIP_PATTERN = re.compile(r"@pytest\.mark\.(?:skip|xfail)|\bpytest\.skip\b|\.skip\s*\(|\bxit\s*\(|\bxdescribe\s*\(|@Ignore\b|\bunittest\.skip|#\[ignore\]|\bt\.Skip")


# ------------------------------------------------------------------------ state
def state_path(pdir: Path, tid: str) -> Path:
    return pdir / "state" / f"{tid}.json"


def gate_path(pdir: Path, tid: str) -> Path:
    return pdir / "state" / f"{tid}.gate.json"


def load_state(pdir: Path, tid: str) -> dict:
    s = read_json(state_path(pdir, tid), None) or {}
    s.setdefault("task", tid)
    s.setdefault("attempts", 0)
    s.setdefault("history", [])
    s.setdefault("task_hash", None)
    s.setdefault("quality_hash", None)
    s.setdefault("baseline_fingerprint", None)
    s.setdefault("test_snapshot", None)
    s.setdefault("sessions", [])
    return s


def save_state(pdir: Path, tid: str, state: dict) -> None:
    write_json(state_path(pdir, tid), state)


# ------------------------------------------------------------------ fingerprint
def list_worktree_files(root: Path) -> list[str]:
    files: list[str] = []
    if (root / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=root, capture_output=True, timeout=60
            ).stdout.decode("utf-8", "replace")
            files = [f for f in out.split("\0") if f]
        except subprocess.SubprocessError:
            files = []
    if not files:
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in IGNORE_DIRS]
            for fn in fns:
                files.append(os.path.relpath(os.path.join(dp, fn), root))
    return sorted(f for f in files if not f.startswith((".ateam/", ".opencode/")) and not any(p in IGNORE_DIRS for p in Path(f).parts))


def fingerprint_map(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in list_worktree_files(root):
        p = root / rel
        try:
            st = p.stat()
        except OSError:
            continue  # deleted tracked file
        if not p.is_file():
            continue
        out[rel] = f"big:{st.st_size}" if st.st_size > BIG_FILE else sha256_file(p)
    return out


def digest_map(m: dict[str, str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(m):
        h.update(f"{rel}\0{m[rel]}\n".encode())
    return h.hexdigest()


def fingerprint(root: Path) -> str:
    return digest_map(fingerprint_map(root))


def diff_maps(before: dict[str, str], after: dict[str, str]) -> list[tuple[str, str]]:
    """[(A|M|D, path)] between two fingerprint maps."""
    out = []
    for rel in sorted(set(before) | set(after)):
        if rel not in before:
            out.append(("A", rel))
        elif rel not in after:
            out.append(("D", rel))
        elif before[rel] != after[rel]:
            out.append(("M", rel))
    return out


def baseline_path(pdir: Path, tid: str) -> Path:
    return pdir / "state" / f"{tid}.baseline.json"


def changed_since_start(root: Path, pdir: Path, tid: str) -> list[tuple[str, str]]:
    base = read_json(baseline_path(pdir, tid), None)
    if base is None:
        return []
    return diff_maps(base, fingerprint_map(root))


# -------------------------------------------------------------------- test guard
def is_test_file(rel: str) -> bool:
    return bool(TEST_FILE_RE.search(rel))


def snapshot_tests(root: Path) -> dict:
    snap = {}
    for rel in list_worktree_files(root):
        if not is_test_file(rel):
            continue
        p = root / rel
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        snap[rel] = {
            "hash": sha256_text(text),
            "tests": sum(len(pat.findall(text)) for pat in TEST_PATTERNS),
            "asserts": len(ASSERT_PATTERN.findall(text)),
            "skips": len(SKIP_PATTERN.findall(text)),
        }
    return snap


def compare_tests(before: dict | None, after: dict) -> list[str]:
    """Findings that require human/LLM review. Empty list means the guard passes."""
    if before is None:
        return []
    findings = []
    for rel, b in before.items():
        a = after.get(rel)
        if a is None:
            findings.append(f"test file deleted: {rel}")
            continue
        if a["hash"] == b["hash"]:
            continue
        if a["tests"] < b["tests"]:
            findings.append(f"{rel}: test count dropped {b['tests']} -> {a['tests']}")
        if a["asserts"] < b["asserts"]:
            findings.append(f"{rel}: assertion count dropped {b['asserts']} -> {a['asserts']}")
        if a["skips"] > b["skips"]:
            findings.append(f"{rel}: skip/xfail markers added ({b['skips']} -> {a['skips']})")
    return findings


# ----------------------------------------------------------------------- hashes
def task_content_hash(meta: dict, body: str) -> str:
    m = {k: v for k, v in meta.items() if k != "status"}
    return sha256_text(json.dumps(m, sort_keys=True) + "\n" + body)


def quality_hash(root: Path) -> str:
    p = Q.quality_path(root)
    return sha256_file(p) if p.exists() else "none"


# ------------------------------------------------------------------- attempt API
def begin_attempt(root: Path, project: str, tid: str, resume: bool = False) -> dict:
    """Lock the task definition and snapshot guards; mark in_progress. Returns the state."""
    pdir = W.require_project(root, project)
    tasks = W.task_index(W.load_tasks(pdir))
    t = tasks.get(tid)
    if t is None:
        die(f"unknown task {tid}")
    if t.errors:
        die(f"{tid} is invalid: {t.errors[0]}")
    allowed = ("pending", "gate_failed", "in_progress", "needs_review")
    if t.status not in allowed:
        die(f"{tid} cannot start from status {t.status}")
    for d in t.deps:
        if tasks.get(d) is None or tasks[d].status != "completed":
            die(f"{tid} has unmet dependency {d}")
    with file_lock(pdir / "state" / ".lock"):
        st = load_state(pdir, tid)
        new_attempt = not (resume and t.status == "in_progress")
        if new_attempt:
            st["attempts"] += 1
        st["task_hash"] = task_content_hash(t.meta, t.body)
        st["quality_hash"] = quality_hash(root)
        if not st.get("baseline_fingerprint") or not baseline_path(pdir, tid).exists():
            fmap = fingerprint_map(root)
            st["baseline_fingerprint"] = digest_map(fmap)
            write_json(baseline_path(pdir, tid), fmap)
        if st.get("test_snapshot") is None:
            st["test_snapshot"] = snapshot_tests(root)
        st["last_start"] = now_iso()
        save_state(pdir, tid, st)
    if t.status != "in_progress":
        W.set_task_status(root, project, tid, "in_progress")
    return st


# --------------------------------------------------------------------- the gate
def run_gate(root: Path, project: str, tid: str, timeout: int | None = None) -> dict:
    cfg = load_config(root)
    pdir = W.require_project(root, project)
    tasks = W.task_index(W.load_tasks(pdir))
    t = tasks.get(tid)
    if t is None:
        die(f"unknown task {tid}")
    st = load_state(pdir, tid)
    t0 = time.time()
    checks: list[dict] = []
    problems: list[str] = []
    if st.get("task_hash") is None:
        die(f"{tid} was not started (run `work start` first)")
    if task_content_hash(t.meta, t.body) != st["task_hash"]:
        problems.append("task file changed since `work start` (the gate definition is locked)")
    touches_gate = bool(t.meta.get("touches_gate"))
    if not touches_gate and quality_hash(root) != st.get("quality_hash"):
        problems.append("quality.json changed during a task that does not declare touches_gate")
    log_dir = pdir / "state" / f"{tid}.logs"
    t_gate = timeout or cfg["gate_timeout_s"]
    profile = list(t.meta.get("quality") if t.meta.get("quality") is not None else Q.DEFAULT_PROFILE)
    for cat in profile:
        checks.append(Q.run_check(root, cat, changed=False, log_dir=log_dir, timeout=t_gate))
    for cid, cmd in schemas.extract_verify(t.body):
        checks.append(Q.run_command(root, cid, cmd, t_gate, log_dir))
    # Guards
    guard_findings = compare_tests(st.get("test_snapshot"), snapshot_tests(root))
    if guard_findings:
        checks.append({"check": "test_guard", "kind": "guard", "status": "needs_review", "reason": "; ".join(guard_findings[:5]), "duration_s": 0.0})
    else:
        checks.append({"check": "test_guard", "kind": "guard", "status": "passed", "reason": "no deleted/weakened tests detected", "duration_s": 0.0})
    fp = fingerprint(root)
    if fp == st.get("baseline_fingerprint") and not t.meta.get("allow_no_changes"):
        checks.append({"check": "worktree_changed", "kind": "guard", "status": "needs_review", "reason": "no files changed since the task started", "duration_s": 0.0})
    for pr in problems:
        checks.append({"check": "integrity", "kind": "guard", "status": "failed", "reason": pr, "duration_s": 0.0})
    statuses = [c["status"] for c in checks]
    if "failed" in statuses:
        overall = "failed"
    elif "not_executed" in statuses:
        overall = "not_executed"
    elif "needs_review" in statuses:
        overall = "needs_review"
    else:
        overall = "passed"
    result = {
        "task": tid,
        "attempt": st["attempts"],
        "finished": now_iso(),
        "fingerprint": fp,
        "task_hash": st["task_hash"],
        "quality_hash": quality_hash(root),
        "overall": overall,
        "duration_s": round(time.time() - t0, 2),
        "checks": checks,
    }
    write_json(gate_path(pdir, tid), result)
    return result


def gate_failure_signature(result: dict) -> str:
    """Normalized signature of a failing gate: failing checks plus the first error line, volatile parts stripped."""
    bad = sorted(c["check"] for c in result["checks"] if c["status"] in ("failed", "not_executed"))
    first = ""
    for c in result["checks"]:
        if c["status"] == "failed":
            first = (c.get("reason") or "")
            break
    first = re.sub(r"/[^\s:]+", "<path>", first)
    first = re.sub(r"\b0x[0-9a-f]+\b|\b\d+(?:\.\d+)?\b", "<n>", first)
    first = re.sub(r"\s+", " ", first).strip()[:80]
    return sha256_text("|".join(bad) + "::" + first)[:12]


def gate_summary(result: dict) -> str:
    lines = [f"gate {result['task']} attempt {result['attempt']}: {result['overall']} ({result['duration_s']}s)"]
    for c in result["checks"]:
        lines.append(f"  {c['check']:<16} {c['status']:<14} {c.get('reason', '')[:150]}")
        if c["status"] == "failed" and c.get("excerpt"):
            lines.extend("      | " + x for x in c["excerpt"][-5:])
    return "\n".join(lines)


def check_gate_valid(root: Path, project: str, tid: str, allow_needs_review: bool = False) -> tuple[bool, str]:
    pdir = W.require_project(root, project)
    g = read_json(gate_path(pdir, tid), None)
    if not g:
        return False, "no gate result; run `work gate` first"
    st = load_state(pdir, tid)
    t = W.task_index(W.load_tasks(pdir)).get(tid)
    if t is None:
        return False, f"unknown task {tid}"
    if g["overall"] != "passed" and not (allow_needs_review and g["overall"] == "needs_review"):
        return False, f"gate overall is '{g['overall']}', not 'passed'"
    if g["attempt"] != st["attempts"]:
        return False, "gate belongs to a different attempt"
    if task_content_hash(t.meta, t.body) != st.get("task_hash") or g["task_hash"] != st.get("task_hash"):
        return False, "task file changed since the gate ran"
    if not t.meta.get("touches_gate") and quality_hash(root) != st.get("quality_hash"):
        return False, "quality.json changed since the task started"
    if g["quality_hash"] != quality_hash(root) and not t.meta.get("touches_gate"):
        return False, "quality.json changed since the gate ran"
    if fingerprint(root) != g["fingerprint"]:
        return False, "worktree changed since the gate ran (stale gate)"
    return True, "ok"


def complete_task(root: Path, project: str, tid: str, reviewed: bool = False) -> None:
    """Complete only with a current passed gate. `reviewed` additionally accepts a `needs_review` gate
    (test guard / no-change findings) that a reviewer explicitly approved; failed and not_executed never pass."""
    ok, why = check_gate_valid(root, project, tid, allow_needs_review=reviewed)
    pdir = W.require_project(root, project)
    if not ok:
        die(f"cannot complete {tid}: {why}")
    task = W.task_index(W.load_tasks(pdir))[tid]
    rp = pdir / "reports" / task.path.name
    if rp.exists():
        try:
            meta, body = fm.parse_file(rp)
            if meta.get("status") != "completed":
                meta["status"] = "completed"
                atomic_write(rp, fm.dump(meta, body))
        except fm.FrontmatterError:
            pass
    st = load_state(pdir, tid)
    st["history"].append({"attempt": st["attempts"], "result": "completed", "at": now_iso(), "reviewed_override": bool(reviewed)})
    st["completed_hash"] = task_content_hash(task.meta, task.body)
    save_state(pdir, tid, st)
    W.set_task_status(root, project, tid, "completed")


# --------------------------------------------------------------------- commands
def cmd_start(args) -> int:
    root = find_root(args.root)
    st = begin_attempt(root, args.project, args.task, resume=args.resume)
    emit({"task": args.task, "attempt": st["attempts"]}, args.json, f"started {args.task} (attempt {st['attempts']})")
    return 0


def cmd_gate(args) -> int:
    root = find_root(args.root)
    res = run_gate(root, args.project, args.task, args.timeout)
    if args.json:
        emit(res, True)
    else:
        print(gate_summary(res))
    return {"passed": 0, "failed": 1, "not_executed": 2, "needs_review": 4}[res["overall"]]


def cmd_complete(args) -> int:
    root = find_root(args.root)
    complete_task(root, args.project, args.task, reviewed=args.reviewed)
    emit({"task": args.task, "status": "completed"}, args.json, f"{args.task} completed")
    return 0


def cmd_gate_valid(args) -> int:
    root = find_root(args.root)
    ok, why = check_gate_valid(root, args.project, args.task)
    emit({"valid": ok, "reason": why}, args.json, f"{'valid' if ok else 'invalid'}: {why}")
    return 0 if ok else 1


def add_commands(ws, add) -> None:
    sp = add("start", cmd_start, "lock the task definition, snapshot guards, mark in_progress")
    sp.add_argument("project")
    sp.add_argument("task")
    sp.add_argument("--resume", action="store_true", help="continue an interrupted attempt without counting a new one")
    sp = add("gate", cmd_gate, "run the hardened gate (exit 0 passed, 1 failed, 2 not_executed, 4 needs_review)")
    sp.add_argument("project")
    sp.add_argument("task")
    sp.add_argument("--timeout", type=int)
    sp = add("complete", cmd_complete, "mark completed; requires a current, passed gate")
    sp.add_argument("project")
    sp.add_argument("task")
    sp.add_argument("--reviewed", action="store_true", help="accept a needs_review gate that a reviewer approved (driver only)")
    sp = add("gate-valid", cmd_gate_valid, "check that the stored gate is still valid for the current tree")
    sp.add_argument("project")
    sp.add_argument("task")
