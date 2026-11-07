"""Feedback capture: deterministic signal events, capped agent notes, derived index, run summaries.

Capture only. Nothing here calls a model. One Markdown file per event, `index.json` is derived and rebuildable.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import frontmatter as fm
from . import schemas
from .common import (
    atomic_write,
    ateam_dir,
    die,
    emit,
    file_lock,
    find_root,
    load_config,
    load_template,
    now_iso,
    read_json,
    today,
    write_json,
)

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d|credential)s?\b\s*[:=]\s*[\"']?[^\s\"']{8,}"),
]

# signal kind -> (category, severity, signature slug, title). Kinds not listed stay in the run record only.
SIGNAL_MAP = {
    "repeated_gate_failure": ("repeated-mistake", "medium", "repeated-gate-failure", "The same gate failure repeated"),
    "needs_architect": ("orchestration", "high", "needs-architect", "A task could not be completed as scoped"),
    "blocked": ("orchestration", "high", "task-blocked", "A task exhausted its attempts"),
    "timeout": ("context-overload", "medium", "role-timeout", "A role run exceeded its time limit"),
    "context_exhausted": ("context-overload", "medium", "context-exhausted", "A role run exhausted the model context"),
    "partial_report": ("unclear-instructions", "medium", "partial-report", "The gate passed but the report was empty"),
    "test_guard": ("testing-difficulty", "medium", "test-guard-triggered", "The test guard flagged weakened or deleted tests"),
    "tool_failure_rate": ("tooling-gap", "medium", "tool-failure-rate", "A high share of tool calls failed"),
    "role_error": ("environment", "medium", "role-error", "A role run crashed or errored"),
    "provider_error": ("environment", "low", "provider-error", "The model provider returned an error"),
    "no_verdict": ("unclear-instructions", "low", "no-verdict", "A reviewer produced no parseable verdict"),
    "integration_failed": ("testing-difficulty", "high", "integration-failed", "The integration gate failed after all tasks passed"),
    "review_exhausted": ("orchestration", "high", "review-rounds-exhausted", "Review rounds were exhausted"),
    "tester_exhausted": ("orchestration", "high", "tester-rounds-exhausted", "Tester rounds were exhausted"),
}
RUN_FALLBACK = "RUN-{day}-000"


def feedback_dir(root: Path) -> Path:
    return ateam_dir(root) / "feedback"


def events_dir(root: Path) -> Path:
    return feedback_dir(root) / "events"


def has_secret(text: str) -> str | None:
    for pat in SECRET_PATTERNS:
        if pat.search(text or ""):
            return pat.pattern[:40]
    return None


def current_run_id() -> str:
    return os.environ.get("ATEAM_RUN_ID") or RUN_FALLBACK.format(day=today().replace("-", ""))


# ------------------------------------------------------------------ id allocation
def _max_id(root: Path) -> int:
    best = 0
    ed = events_dir(root)
    if ed.is_dir():
        for p in ed.glob("*/*.md"):
            m = re.search(r"-FB-(\d{6})\.md$", p.name)
            if m:
                best = max(best, int(m.group(1)))
    return best


def allocate(root: Path, run_id: str, agent: str, content_fn) -> tuple[str, Path]:
    """Allocate the next FB id and create its file exclusively, under a lock. `content_fn(id) -> text`."""
    fd_dir = events_dir(root) / run_id
    fd_dir.mkdir(parents=True, exist_ok=True)
    with file_lock(feedback_dir(root) / ".lock"):
        n = _max_id(root) + 1
        while True:
            fid = f"FB-{n:06d}"
            path = fd_dir / f"{agent}-{fid}.md"
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                n += 1
                continue
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(content_fn(fid))
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            return fid, path


def render_event(fid: str, run_id: str, task: str | None, agent: str, category: str, severity: str, source: str, signature: str,
                 problem: str, refs: list[str], context: str = "", workaround: str = "", improvement: str = "", symptoms: str = "", root_cause: str = "") -> str:
    text = load_template("feedback.md")
    for key, val in {
        "__ID__": fid, "__RUN__": run_id, "__TASK__": task or "none", "__AGENT__": agent, "__CATEGORY__": category,
        "__SEVERITY__": severity, "__SOURCE__": source, "__SIGNATURE__": signature, "__CREATED__": now_iso(),
        "__PROBLEM__": problem.strip(), "__REFS__": "\n".join(f"- {r}" for r in refs),
    }.items():
        text = text.replace(key, val)
    for head, val in (("Context", context), ("Symptoms", symptoms), ("Root Cause", root_cause), ("Successful Workaround", workaround), ("Proposed Improvement", improvement)):
        if val.strip():
            text = text.replace(f"# {head}\n", f"# {head}\n{val.strip()}\n", 1)
    return text


# ----------------------------------------------------------------------- reading
def read_events(root: Path) -> tuple[list[dict], list[str]]:
    out, errors = [], []
    ed = events_dir(root)
    if not ed.is_dir():
        return out, errors
    for p in sorted(ed.glob("*/*.md")):
        try:
            meta, body = fm.parse_file(p)
        except (fm.FrontmatterError, UnicodeDecodeError) as exc:
            errors.append(f"{p.name}: {exc}")
            continue
        errs = schemas.validate_feedback(meta, body)
        if errs:
            errors.append(f"{p.name}: {'; '.join(errs)}")
            continue
        out.append(
            {
                "id": meta["id"], "run": meta["execution_id"], "task": meta.get("task_id"), "agent": meta["agent"],
                "category": meta["category"], "severity": meta["severity"], "status": meta["status"], "source": meta["source"],
                "signature": meta["signature"], "created_at": meta.get("created_at"), "path": str(p.relative_to(root)),
                "addressed_by": meta.get("addressed_by"),
            }
        )
    return out, errors


def rebuild_index(root: Path) -> dict:
    events, errors = read_events(root)
    data = {"generated": now_iso(), "count": len(events), "events": events, "errors": errors}
    write_json(feedback_dir(root) / "index.json", data)
    return data


def load_index(root: Path) -> dict:
    return rebuild_index(root)  # cheap (one file per event); always consistent with the files


def group(events: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for e in events:
        g = groups.setdefault(e["signature"], {"signature": e["signature"], "count": 0, "ids": [], "tasks": [], "runs": [], "categories": [], "severities": [], "sources": []})
        g["count"] += 1
        g["ids"].append(e["id"])
        for k, v in (("tasks", e["task"]), ("runs", e["run"]), ("categories", e["category"]), ("severities", e["severity"]), ("sources", e["source"])):
            if v and v not in g[k]:
                g[k].append(v)
    return sorted(groups.values(), key=lambda g: (-g["count"], g["signature"]))


def query(root: Path, open_only: bool = False, task: str | None = None, category: str | None = None, run: str | None = None) -> list[dict]:
    events = load_index(root)["events"]
    return [
        e for e in events
        if (not open_only or e["status"] == "open") and (task is None or e["task"] == task)
        and (category is None or e["category"] == category) and (run is None or e["run"] == run)
    ]


def set_status(root: Path, fid: str, status: str, by: str | None = None) -> Path:
    if status not in schemas.FEEDBACK_STATUSES:
        die(f"status must be one of {schemas.FEEDBACK_STATUSES}")
    for e in read_events(root)[0]:
        if e["id"] == fid:
            p = root / e["path"]
            meta, body = fm.parse_file(p)
            meta["status"] = status
            if by:
                meta["addressed_by"] = by
            atomic_write(p, fm.dump(meta, body))
            return p
    die(f"unknown feedback id {fid}")


# ------------------------------------------------------------------------ signals
def record_signal(root: Path, run_id: str, kind: str, task: str | None, detail: str, **extra) -> str | None:
    """Deterministic event written by the driver. One event per (run, kind, task, signature). Returns the id or None."""
    spec = SIGNAL_MAP.get(kind)
    if spec is None:
        return None
    category, severity, signature, title = spec
    dedupe = f"{kind}|{task}|{extra.get('signature', '')}"
    ledger_path = events_dir(root) / run_id / ".signals.json"
    with file_lock(feedback_dir(root) / ".signals.lock"):
        seen = read_json(ledger_path, []) or []
        if dedupe in seen:
            return None
        seen.append(dedupe)
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(ledger_path, seen)
    refs = [f".ateam/runs/{run_id}.json"]
    if task:
        refs.append(f"task {task}")
    problem = f"{title}. {detail}".strip()
    if has_secret(problem):
        problem = f"{title}. (details withheld: possible secret)"
    fid, _ = allocate(
        root, run_id, "driver",
        lambda fid: render_event(fid, run_id, task, "driver", category, severity, "signal", signature, problem, refs, context=f"Detected by the driver in {run_id}."),
    )
    return fid


# ------------------------------------------------------------------------- notes
def add_note(root: Path, *, category: str, severity: str, signature: str, problem: str, evidence: list[str], task: str | None,
             run_id: str, agent: str, context: str = "", workaround: str = "", improvement: str = "") -> tuple[str, Path]:
    cfg = load_config(root)
    if category not in schemas.FEEDBACK_CATEGORIES:
        die(f"category must be one of {', '.join(schemas.FEEDBACK_CATEGORIES)}")
    if severity not in schemas.SEVERITIES:
        die(f"severity must be one of {', '.join(schemas.SEVERITIES)}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", signature or ""):
        die("signature must be a kebab-case slug (lowercase letters, digits, '.', '_', '-')")
    if not (problem or "").strip():
        die("rejected: Problem is empty")
    evidence = [e.strip() for e in evidence if e and e.strip()]
    if not evidence:
        die("rejected: an evidence reference is required (--evidence <file path, command or log excerpt reference>)")
    if task and not schemas.TASK_ID_RE.match(task):
        die("task must look like TASK-001")
    if not schemas.RUN_ID_RE.match(run_id):
        die(f"run id {run_id!r} must look like RUN-YYYYMMDD-NNN")
    for label, text in (("problem", problem), ("context", context), ("workaround", workaround), ("improvement", improvement), ("evidence", " ".join(evidence))):
        hit = has_secret(text)
        if hit:
            die(f"rejected: the {label} text looks like it contains a secret ({hit}...). Remove it and describe the problem without it")
    cap = int(cfg["feedback_notes_per_task"]) if task else int(cfg.get("feedback_notes_per_run", 10))
    with file_lock(feedback_dir(root) / ".notes.lock"):
        mine = [e for e in read_events(root)[0] if e["source"] == "agent" and e["run"] == run_id and (e["task"] == task if task else e["task"] in (None, "none"))]
        if len(mine) >= cap:
            die(f"rejected: at most {cap} notes per {'task' if task else 'run'} ({'task ' + task if task else run_id}); ordinary bugs do not belong here")
        fid, path = allocate(
            root, run_id, agent,
            lambda fid: render_event(fid, run_id, task, agent, category, severity, "agent", signature, problem, evidence, context, workaround, improvement),
        )
    return fid, path


# --------------------------------------------------------------------- run summary
def finish_run(root: Path, record: dict, pdir: Path) -> Path:
    """Write the run summary (completed, failed, repeated problems, candidate improvements, reusable procedures)."""
    run_id = record["id"]
    rebuild_index(root)
    events = [e for e in read_events(root)[0] if e["run"] == run_id]
    groups = group(events)
    completed = [t for t, r in record["tasks"].items() if r.get("outcome") == "completed"]
    failed = [t for t, r in record["tasks"].items() if r.get("outcome") in ("blocked", "needs_architect")]
    repeated = [g for g in group([e for e in read_events(root)[0] if e["status"] == "open"]) if g["count"] >= 2]
    procedures = []
    for e in events:
        if e["source"] == "agent":
            try:
                _, body = fm.parse_file(root / e["path"])
            except fm.FrontmatterError:
                continue
            wk = schemas.parse_sections(body).get("Successful Workaround", "").strip()
            if wk:
                procedures.append({"id": e["id"], "signature": e["signature"], "workaround": wk[:300]})
    totals = record["totals"]
    summary = {
        "run": run_id, "project": record["project"], "status": record["status"], "wall_s": record.get("wall_s"),
        "completed": completed, "failed": failed,
        "repeated_problems": [{"signature": g["signature"], "count": g["count"], "ids": g["ids"][:5]} for g in repeated],
        "events_this_run": {g["signature"]: g["count"] for g in groups},
        "candidate_improvements": [g["signature"] for g in repeated if "agent" in g["sources"] or g["count"] >= 3],
        "reusable_procedures": procedures,
        "metrics": {
            "attempts": totals["attempts"], "role_calls": totals["role_calls"], "role_s": totals["role_s"], "gate_s": totals["gate_s"],
            "retries": totals["retries"], "tokens": totals["tokens"],
            "task_loads": {t: r["attempts"] for t, r in record["tasks"].items()},
        },
    }
    d = runs_summary_dir(root, run_id)
    write_json(d / "summary.json", summary)
    lines = [
        f"# Run {run_id} ({record['project']})", f"Status: {record['status']}  Wall: {record.get('wall_s')}s", "",
        f"Completed: {', '.join(completed) or '-'}", f"Failed: {', '.join(failed) or '-'}",
        f"Attempts: {totals['attempts']}  Role calls: {totals['role_calls']}  Retries: {totals['retries']}",
        f"Tokens: in {totals['tokens']['input']}, out {totals['tokens']['output']}, cache read {totals['tokens']['cache_read']}", "",
        "## Repeated problems (open, across runs)",
        *([f"- {g['signature']} x{g['count']} ({', '.join(g['ids'][:4])})" for g in repeated] or ["- none"]),
        "", "## Candidate improvements", *([f"- {s}" for s in summary["candidate_improvements"]] or ["- none"]),
        "", "## Reusable procedures", *([f"- {p['id']}: {p['workaround']}" for p in procedures] or ["- none"]),
    ]
    atomic_write(d / "summary.md", "\n".join(lines) + "\n")
    return d / "summary.md"


def runs_summary_dir(root: Path, run_id: str) -> Path:
    d = ateam_dir(root) / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------- commands
def cmd_note(args) -> int:
    root = find_root(args.root)
    task = args.task or os.environ.get("ATEAM_TASK") or None
    fid, path = add_note(
        root, category=args.category, severity=args.severity, signature=args.signature, problem=args.problem or "",
        evidence=args.evidence or [], task=task, run_id=args.run or current_run_id(), agent=args.agent or os.environ.get("ATEAM_AGENT") or "agent",
        context=args.context or "", workaround=args.workaround or "", improvement=args.improvement or "",
    )
    emit({"id": fid, "path": str(path.relative_to(root))}, args.json, f"{fid} {path.relative_to(root)}")
    return 0


def cmd_index(args) -> int:
    root = find_root(args.root)
    data = rebuild_index(root)
    emit({"count": data["count"], "errors": data["errors"]}, args.json, f"indexed {data['count']} event(s)" + (f", {len(data['errors'])} invalid" if data["errors"] else ""))
    return 1 if data["errors"] else 0


def cmd_query(args) -> int:
    root = find_root(args.root)
    events = query(root, args.open, args.task, args.category, args.run)
    if args.group:
        groups = group(events)
        emit(groups, args.json, "\n".join(f"{g['count']:>3}  {g['signature']}  ({', '.join(g['ids'][:4])})" for g in groups) or "(none)")
    else:
        emit(events, args.json, "\n".join(f"{e['id']}  {e['severity']:<6} {e['category']:<22} {e['signature']}  {e['task'] or '-'}" for e in events) or "(none)")
    return 0


def cmd_mark(args) -> int:
    root = find_root(args.root)
    p = set_status(root, args.id, args.status, args.by)
    rebuild_index(root)
    emit({"id": args.id, "status": args.status}, args.json, f"{args.id} -> {args.status} ({p.relative_to(root)})")
    return 0


def register(sub) -> None:
    p = sub.add_parser("feedback", help="capture and query agent-experience events")
    fs = p.add_subparsers(dest="fb_cmd", required=True)
    n = fs.add_parser("note", help="record an agent note (max 2 per task; evidence required)")
    n.add_argument("--category", required=True, choices=list(schemas.FEEDBACK_CATEGORIES))
    n.add_argument("--severity", default="medium", choices=list(schemas.SEVERITIES))
    n.add_argument("--signature", required=True, help="kebab-case slug that groups repeated problems")
    n.add_argument("--problem", required=True)
    n.add_argument("--evidence", action="append", help="file path, command or log reference (repeatable, at least one)")
    n.add_argument("--context")
    n.add_argument("--workaround")
    n.add_argument("--improvement")
    n.add_argument("--task")
    n.add_argument("--run")
    n.add_argument("--agent")
    n.add_argument("--json", action="store_true")
    n.set_defaults(func=cmd_note)
    i = fs.add_parser("index", help="rebuild index.json from the event files")
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_index)
    q = fs.add_parser("query", help="list or group events")
    q.add_argument("--open", action="store_true")
    q.add_argument("--task")
    q.add_argument("--category")
    q.add_argument("--run")
    q.add_argument("--group", action="store_true", help="group by signature")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_query)
    m = fs.add_parser("mark", help="set an event's status (addressed or dismissed)")
    m.add_argument("id")
    m.add_argument("status", choices=list(schemas.FEEDBACK_STATUSES))
    m.add_argument("--by", help="proposal id that addressed it")
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_mark)
