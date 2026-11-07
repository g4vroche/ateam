"""Task lifecycle beyond start/gate/complete: next, fail (bounded retries), reopen, split, immutability."""
from __future__ import annotations

import json
from pathlib import Path

from . import frontmatter as fm
from . import gate as G
from . import work as W
from .common import (
    atomic_write,
    die,
    emit,
    file_lock,
    find_root,
    load_config,
    now_iso,
    read_json,
    sha256_text,
    write_json,
)

RUNNABLE_ORDER = ("in_progress", "gate_failed", "needs_review")
STOP_STATUSES = ("blocked", "needs_architect")
SPLITTABLE = ("pending", "gate_failed", "needs_architect", "blocked")


# ------------------------------------------------------------------------ next
def next_action(root: Path, project: str) -> dict:
    """What the driver should do next. Deterministic; reads task frontmatter only."""
    pdir = W.require_project(root, project)
    tasks = W.load_tasks(pdir)
    bad = [t for t in tasks if t.errors]
    if bad:
        return {"action": "stop", "reason": "invalid", "tasks": [t.id for t in bad], "detail": bad[0].errors[0]}
    if not tasks:
        return {"action": "stop", "reason": "empty", "tasks": []}
    by_id = W.task_index(tasks)
    stopped = sorted(t.id for t in tasks if t.status in STOP_STATUSES)
    if stopped:
        reason = "needs_architect" if any(by_id[i].status == "needs_architect" for i in stopped) else "blocked"
        return {"action": "stop", "reason": reason, "tasks": stopped}
    for status in RUNNABLE_ORDER:
        for t in sorted((t for t in tasks if t.status == status), key=lambda t: t.id):
            if all(by_id.get(d) and by_id[d].status == "completed" for d in t.deps):
                return {"action": "run", "task": t.id, "status": status}
    ready = W.ready_tasks(tasks)
    if ready:
        return {"action": "run", "task": ready[0].id, "status": "pending"}
    if all(t.status == "completed" for t in tasks):
        return {"action": "done"}
    return {"action": "stop", "reason": "stuck", "tasks": sorted(t.id for t in tasks if t.status != "completed")}


# ------------------------------------------------------------------------ fail
def failure_signature_from_reason(reason: str) -> str:
    return sha256_text("reason:" + reason)[:12]


def fail_task(root: Path, project: str, tid: str, reason: str, signature: str | None = None, detail: str = "") -> dict:
    """Record a failed attempt and decide: retry | needs_architect | blocked. Bounded regardless of signatures.

    Two consecutive failures with the same signature stop retrying (needs_architect). `max_attempts` bounds retries
    even when every signature differs (blocked).
    """
    cfg = load_config(root)
    pdir = W.require_project(root, project)
    t = W.task_index(W.load_tasks(pdir)).get(tid)
    if t is None:
        die(f"unknown task {tid}")
    signature = signature or failure_signature_from_reason(reason)
    max_attempts = int(t.meta.get("max_attempts", cfg["max_attempts"]))
    limit = int(cfg["signature_repeat_limit"])
    with file_lock(pdir / "state" / ".lock"):
        st = G.load_state(pdir, tid)
        st["history"].append(
            {"attempt": st["attempts"], "result": "failed", "reason": reason, "signature": signature, "detail": detail[:500], "at": now_iso()}
        )
        repeat = 0
        for h in reversed([h for h in st["history"] if h.get("result") in ("failed", "reopened", "completed")]):
            if h.get("result") == "failed" and h.get("signature") == signature:
                repeat += 1
            else:
                break
        if repeat >= limit:
            decision = status = "needs_architect"
        elif st["attempts"] >= max_attempts:
            decision = status = "blocked"
        else:
            decision, status = "retry", "gate_failed"
        st["last_decision"] = {"decision": decision, "signature": signature, "repeat": repeat, "at": now_iso()}
        G.save_state(pdir, tid, st)
    W.set_task_status(root, project, tid, status)
    return {
        "task": tid,
        "decision": decision,
        "status": status,
        "attempt": st["attempts"],
        "max_attempts": max_attempts,
        "signature": signature,
        "repeat": repeat,
    }


# ---------------------------------------------------------------------- reopen
def reopen_task(root: Path, project: str, tid: str, reason: str = "", review: str | None = None) -> None:
    """Explicit exception to immutability: after a reviewer or tester finding, a completed task becomes pending again."""
    pdir = W.require_project(root, project)
    t = W.task_index(W.load_tasks(pdir)).get(tid)
    if t is None:
        die(f"unknown task {tid}")
    if t.status not in ("completed", "needs_review"):
        die(f"{tid} is {t.status}; only completed or needs_review tasks can be reopened")
    with file_lock(pdir / "state" / ".lock"):
        st = G.load_state(pdir, tid)
        st["history"].append({"attempt": st["attempts"], "result": "reopened", "reason": reason[:300], "review": review, "at": now_iso()})
        st["reopened"] = st.get("reopened", 0) + 1
        st["attempts"] = 0
        st["baseline_fingerprint"] = None
        st["test_snapshot"] = None
        st.pop("completed_hash", None)
        G.save_state(pdir, tid, st)
        for p in (G.gate_path(pdir, tid), G.baseline_path(pdir, tid)):
            if p.exists():
                p.unlink()
    rp = pdir / "reports" / t.path.name
    if rp.exists():
        try:
            meta, body = fm.parse_file(rp)
            meta["status"] = "partial"
            atomic_write(rp, fm.dump(meta, body))
        except fm.FrontmatterError:
            pass
    W.set_task_status(root, project, tid, "pending")


# ----------------------------------------------------------------------- split
def split_task(root: Path, project: str, tid: str) -> dict:
    """Retire a pending or failed task so the architect can replace it. Completed and running tasks are refused."""
    pdir = W.require_project(root, project)
    t = W.task_index(W.load_tasks(pdir)).get(tid)
    if t is None:
        die(f"unknown task {tid}")
    if t.status not in SPLITTABLE:
        die(f"{tid} is {t.status}; split is allowed only for {', '.join(SPLITTABLE)}")
    with file_lock(pdir / "state" / ".lock"):
        st = G.load_state(pdir, tid)
        arch = pdir / "tasks" / "superseded"
        arch.mkdir(parents=True, exist_ok=True)
        dst = arch / t.path.name
        context = {
            "task": tid,
            "title": t.meta.get("title"),
            "status": t.status,
            "attempts": st["attempts"],
            "history": st["history"][-6:],
            "gate": read_json(G.gate_path(pdir, tid), None),
            "archived_task": str(dst.relative_to(pdir)),
            "dependents": [x.id for x in W.load_tasks(pdir) if tid in x.deps],
            "retired_at": now_iso(),
        }
        write_json(pdir / "state" / f"{tid}.superseded.json", context)
        t.path.replace(dst)
    W.sync_manifest(root, project)
    return context


# -------------------------------------------------------------------- immutable
def completed_modified(root: Path, project: str) -> list[str]:
    """Completed tasks whose definition changed after completion."""
    pdir = W.project_dir(root, project)
    out = []
    for t in W.load_tasks(pdir):
        if t.status != "completed" or t.errors:
            continue
        st = read_json(G.state_path(pdir, t.id), None) or {}
        want = st.get("completed_hash")
        if want and G.task_content_hash(t.meta, t.body) != want:
            out.append(t.id)
    return out


# -------------------------------------------------------------------- commands
def cmd_next(args) -> int:
    root = find_root(args.root)
    res = next_action(root, args.project)
    emit(res, args.json, json.dumps(res))
    return 0


def cmd_fail(args) -> int:
    root = find_root(args.root)
    res = fail_task(root, args.project, args.task, args.reason, args.signature)
    emit(res, args.json, f"{args.task}: {res['decision']} (attempt {res['attempt']}/{res['max_attempts']}, status {res['status']})")
    return 0


def cmd_reopen(args) -> int:
    root = find_root(args.root)
    reopen_task(root, args.project, args.task, args.reason or "", args.review)
    emit({"task": args.task, "status": "pending"}, args.json, f"{args.task} reopened")
    return 0


def cmd_split(args) -> int:
    root = find_root(args.root)
    ctx = split_task(root, args.project, args.task)
    emit(
        ctx,
        args.json,
        f"{args.task} retired to {ctx['archived_task']}; dependents to rewire: {', '.join(ctx['dependents']) or '-'}. "
        "Create replacement tasks with `work new-task`, then `work sync` and `work validate`.",
    )
    return 0


def add_commands(ws, add) -> None:
    sp = add("next", cmd_next, "what to run next: run <task> | done | stop <reason>")
    sp.add_argument("project")
    sp = add("fail", cmd_fail, "record a failed attempt; decides retry, needs_architect or blocked")
    sp.add_argument("project")
    sp.add_argument("task")
    sp.add_argument("--reason", required=True)
    sp.add_argument("--signature")
    sp = add("reopen", cmd_reopen, "reopen a completed task after review findings")
    sp.add_argument("project")
    sp.add_argument("task")
    sp.add_argument("--reason")
    sp.add_argument("--review", help="path of the review file with the findings")
    sp = add("split", cmd_split, "retire a pending or failed task so the architect can replace it (never completed tasks)")
    sp.add_argument("project")
    sp.add_argument("task")
