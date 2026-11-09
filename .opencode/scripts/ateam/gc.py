"""Garbage collection for runtime data. Dry-run by default; `--apply` deletes. Retention comes from `.ateam/config.json`.

Never deleted: task files, reports, reviews, plans, completed-task state (`TASK-*.json`, `*.gate.json`, `final.json`
carry the immutability hashes), open feedback, proposals, the knowledge store, the newest run, running runs.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from . import feedback as F
from . import frontmatter as fm
from .common import ateam_dir, emit, find_root, load_config, read_json, sha256_text

DAY = 86400


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _age_days(p: Path, now: float) -> float:
    return (now - p.stat().st_mtime) / DAY


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def project_completed(pdir: Path) -> bool:
    tasks = sorted((pdir / "tasks").glob("*.md")) if (pdir / "tasks").is_dir() else []
    if not tasks:
        return False
    for t in tasks:
        try:
            meta, _ = fm.parse_file(t)
        except (fm.FrontmatterError, OSError):
            return False
        if meta.get("status") != "completed":
            return False
    return True


def plan(root: Path, now: float | None = None) -> list[dict]:
    now = now or time.time()
    cfg = load_config(root)
    ret = cfg["retention"]
    a = ateam_dir(root)
    items: list[dict] = []

    def add(path: Path, kind: str, reason: str) -> None:
        items.append({"path": str(path.relative_to(root)), "kind": kind, "reason": reason, "bytes": _size(path)})

    # runs: keep the newest `keep_runs`, never a live one
    rdir = a / "runs"
    if rdir.is_dir():
        run_ids = sorted(p.name for p in rdir.iterdir() if p.is_dir() and p.name.startswith("RUN-"))
        for rid in run_ids[: max(0, len(run_ids) - max(1, int(ret["keep_runs"])))]:
            rec = read_json(rdir / f"{rid}.json", {}) or {}
            if rec.get("status") == "running" and _pid_alive(rec.get("pid")):
                continue
            add(rdir / rid, "run", f"older than the newest {ret['keep_runs']} runs")
            if (rdir / f"{rid}.json").exists():
                add(rdir / f"{rid}.json", "run", "record of a collected run")
        ql = rdir / "quality-logs"
        if ql.is_dir():
            for f in sorted(ql.iterdir()):
                if _age_days(f, now) > ret["state_days"]:
                    add(f, "quality-log", f"older than {ret['state_days']} days")

    # state of completed projects: logs and attempt baselines only
    wdir = a / "work"
    if wdir.is_dir():
        for pdir in sorted(p for p in wdir.iterdir() if p.is_dir()):
            st = pdir / "state"
            if not st.is_dir() or not project_completed(pdir):
                continue
            for f in sorted(st.iterdir()):
                logs = f.is_dir() and f.name.endswith(".logs")
                base = f.is_file() and (f.name.endswith(".baseline.json") or f.name == "run-baseline.json")
                if (logs or base) and _age_days(f, now) > ret["state_days"]:
                    add(f, "state", f"completed project, older than {ret['state_days']} days")

    # feedback that has been dealt with
    ev = a / "feedback" / "events"
    if ev.is_dir():
        status = {e["path"]: e["status"] for e in F.read_events(root)[0]}
        for f in sorted(ev.glob("*/*.md")):
            if status.get(str(f.relative_to(root))) in ("addressed", "dismissed") and _age_days(f, now) > ret["feedback_addressed_days"]:
                add(f, "feedback", f"addressed or dismissed, older than {ret['feedback_addressed_days']} days")

    # inbox notes that have already been dreamed
    inbox = a / "knowledge" / "inbox"
    ledger = (read_json(a / "knowledge" / ".checkpoint.json", {}) or {}).get("sources", {})
    if inbox.is_dir():
        for f in sorted(inbox.glob("*.md")):
            rel = str(f.relative_to(root))
            if _age_days(f, now) > ret["inbox_days"] and ledger.get(rel) == sha256_text(f.read_text(encoding="utf-8", errors="replace")):
                add(f, "inbox", f"already dreamed, older than {ret['inbox_days']} days")
    return items


def apply(root: Path, items: list[dict]) -> int:
    base = ateam_dir(root).resolve()
    n = 0
    for it in items:
        p = (root / it["path"]).resolve()
        try:
            p.relative_to(base)
        except ValueError:
            continue  # defensive: only ever delete inside .ateam
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
        n += 1
    if any(i["kind"] == "feedback" for i in items):
        F.rebuild_index(root)
    return n


def cmd_gc(args) -> int:
    root = find_root(args.root)
    items = plan(root)
    total = sum(i["bytes"] for i in items)
    removed = apply(root, items) if args.apply else 0
    res = {"applied": bool(args.apply), "items": items, "bytes": total, "removed": removed}
    head = f"{'removed' if args.apply else 'would remove'} {len(items)} item(s), {total} bytes"
    emit(res, args.json, "\n".join([head] + [f"  {i['kind']:<12} {i['path']}  ({i['reason']})" for i in items]) + ("" if args.apply or not items else "\nre-run with --apply to delete"))
    return 0


def register(sub) -> None:
    p = sub.add_parser("gc", help="remove expired runtime data (dry-run unless --apply)")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_gc)
