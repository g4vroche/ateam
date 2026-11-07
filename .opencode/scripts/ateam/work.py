"""Work projects: plan, atomic task files, derived manifest, validation and graph."""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
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
    read_json,
    slugify,
    write_json,
)

PROJECT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
PLACEHOLDERS = ("<observable outcome>", "<deterministic command>")


@dataclass
class Task:
    id: str
    path: Path
    meta: dict
    body: str
    errors: list[str] = field(default_factory=list)

    @property
    def deps(self) -> list[str]:
        return list(self.meta.get("dependencies") or [])

    @property
    def status(self) -> str:
        return self.meta.get("status", "pending")


# ------------------------------------------------------------------ paths/loading
def work_root(root: Path) -> Path:
    return ateam_dir(root) / "work"


def project_dir(root: Path, project: str) -> Path:
    if not PROJECT_RE.match(project):
        die(f"invalid project id {project!r} (use kebab-case)")
    return work_root(root) / project


def require_project(root: Path, project: str) -> Path:
    pdir = project_dir(root, project)
    if not pdir.is_dir():
        die(f"project {project!r} not found at {pdir}")
    return pdir


def load_tasks(pdir: Path) -> list[Task]:
    tasks: list[Task] = []
    tdir = pdir / "tasks"
    if not tdir.is_dir():
        return tasks
    for path in sorted(tdir.glob("*.md")):
        try:
            meta, body = fm.parse_file(path)
        except (fm.FrontmatterError, UnicodeDecodeError) as exc:
            t = Task(id=f"?{path.name}", path=path, meta={}, body="")
            t.errors.append(f"unparseable frontmatter: {exc}")
            tasks.append(t)
            continue
        t = Task(id=str(meta.get("id", f"?{path.name}")), path=path, meta=meta, body=body)
        t.errors.extend(schemas.validate_task(meta, body, path.name))
        for ph in PLACEHOLDERS:
            if ph in body:
                t.errors.append(f"template placeholder {ph!r} not replaced")
        tasks.append(t)
    return tasks


def task_index(tasks: list[Task]) -> dict[str, Task]:
    return {t.id: t for t in tasks}


def find_cycle(tasks: list[Task]) -> list[str] | None:
    graph = {t.id: [d for d in t.deps if d != t.id] for t in tasks}
    WHITE, GREY, BLACK = 0, 1, 2
    color = {k: WHITE for k in graph}
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        color[n] = GREY
        stack.append(n)
        for d in graph.get(n, []):
            if d not in graph:
                continue
            if color[d] == GREY:
                return stack[stack.index(d) :] + [d]
            if color[d] == WHITE:
                found = visit(d)
                if found:
                    return found
        stack.pop()
        color[n] = BLACK
        return None

    for n in graph:
        if color[n] == WHITE:
            found = visit(n)
            if found:
                return found
    return None


def topo_order(tasks: list[Task]) -> list[str]:
    """Deterministic order: dependencies first, then priority, then id."""
    by_id = task_index(tasks)
    done: set[str] = set()
    order: list[str] = []
    remaining = set(by_id)
    while remaining:
        ready = [
            i
            for i in remaining
            if all((d in done) or (d not in by_id) for d in by_id[i].deps)
        ]
        if not ready:
            order.extend(sorted(remaining))
            break
        ready.sort(key=lambda i: (PRIORITY_RANK.get(by_id[i].meta.get("priority"), 1), i))
        pick = ready[0]
        order.append(pick)
        done.add(pick)
        remaining.discard(pick)
    return order


# -------------------------------------------------------------------- manifest
def build_manifest(project: str, tasks: list[Task], root: Path) -> dict:
    items = []
    for t in sorted(tasks, key=lambda x: x.id):
        items.append(
            {
                "id": t.id,
                "file": f"tasks/{t.path.name}",
                "status": t.status,
                "dependencies": t.deps,
                "priority": t.meta.get("priority", "medium"),
            }
        )
    return {"project": project, "tasks": items}


def sync_manifest(root: Path, project: str) -> dict:
    pdir = require_project(root, project)
    with file_lock(pdir / "state" / ".lock"):
        tasks = [t for t in load_tasks(pdir) if not t.id.startswith("?")]
        manifest = build_manifest(project, tasks, root)
        write_json(pdir / "manifest.json", manifest)
    return manifest


def set_task_status(root: Path, project: str, task_id: str, status: str) -> None:
    """Canonical status change: task file first, then manifest."""
    if status not in schemas.TASK_STATUSES:
        die(f"invalid status {status!r}")
    pdir = require_project(root, project)
    with file_lock(pdir / "state" / ".lock"):
        tasks = task_index(load_tasks(pdir))
        t = tasks.get(task_id)
        if t is None:
            die(f"unknown task {task_id}")
        meta = dict(t.meta)
        meta["status"] = status
        atomic_write(t.path, fm.dump(meta, t.body))
        tasks[task_id] = Task(task_id, t.path, meta, t.body)
        write_json(pdir / "manifest.json", build_manifest(project, list(tasks.values()), root))


# ------------------------------------------------------------------- validation
def validate_project(root: Path, project: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    pdir = project_dir(root, project)
    if not pdir.is_dir():
        return [f"project {project!r} does not exist"], []
    for need in ("plan.md", "manifest.json", "tasks"):
        if not (pdir / need).exists():
            errors.append(f"missing {need}")
    plan = pdir / "plan.md"
    if plan.exists():
        errors.extend(schemas.validate_plan(plan.read_text(encoding="utf-8")))
    tasks = load_tasks(pdir)
    cfg = load_config(root)
    by_id: dict[str, Task] = {}
    for t in tasks:
        for e in t.errors:
            errors.append(f"{t.id} ({t.path.name}): {e}")
        if t.id in by_id:
            errors.append(f"duplicate task id {t.id}")
        by_id[t.id] = t
        if len(t.body) > 2 * cfg["max_task_chars"]:
            errors.append(f"{t.id}: task body is {len(t.body)} chars (hard limit {2 * cfg['max_task_chars']}); split it")
        elif len(t.body) > cfg["max_task_chars"]:
            warnings.append(f"{t.id}: task body is {len(t.body)} chars (budget {cfg['max_task_chars']}); split it or move detail into a referenced doc")
        for s in t.meta.get("skills") or []:
            if not (root / ".opencode" / "skills" / s / "SKILL.md").exists():
                errors.append(f"{t.id}: skill {s!r} does not exist")
        for r in t.meta.get("reads") or []:
            if not (root / r).exists():
                warnings.append(f"{t.id}: reads path {r!r} does not exist")
    good = [t for t in tasks if not t.id.startswith("?")]
    for t in good:
        for d in t.deps:
            if d not in by_id:
                errors.append(f"{t.id}: dependency {d} does not exist")
    cyc = find_cycle(good)
    if cyc:
        errors.append("dependency cycle: " + " -> ".join(cyc))
    warnings.extend(_quality_gap_warnings(root, good))
    from . import lifecycle

    for tid in lifecycle.completed_modified(root, project):
        errors.append(f"{tid}: completed task definition was modified after completion (completed tasks are immutable)")
    # manifest consistency
    mpath = pdir / "manifest.json"
    if mpath.exists():
        try:
            manifest = read_json(mpath)
        except ValueError as exc:
            manifest = None
            errors.append(f"manifest.json is not valid JSON: {exc}")
        if manifest is not None:
            errors.extend(schemas.validate_manifest(manifest))
            if isinstance(manifest, dict) and manifest.get("project") not in (None, project):
                errors.append(f"manifest.project {manifest.get('project')!r} != directory {project!r}")
            entries = {e.get("id"): e for e in (manifest.get("tasks") or []) if isinstance(e, dict)} if isinstance(manifest, dict) else {}
            for tid, e in entries.items():
                t = by_id.get(tid)
                if t is None:
                    errors.append(f"manifest lists {tid} but no task file defines it")
                    continue
                if e.get("file") != f"tasks/{t.path.name}":
                    errors.append(f"{tid}: manifest file {e.get('file')!r} != tasks/{t.path.name}")
                if not (pdir / str(e.get("file", ""))).exists():
                    errors.append(f"{tid}: manifest file {e.get('file')!r} is missing")
                if e.get("status") != t.status:
                    errors.append(f"{tid}: manifest status {e.get('status')!r} != task status {t.status!r} (run `work sync`)")
                if sorted(e.get("dependencies") or []) != sorted(t.deps):
                    errors.append(f"{tid}: manifest dependencies differ from task file (run `work sync`)")
            for t in good:
                if t.id not in entries:
                    errors.append(f"{t.id}: task file not listed in manifest (run `work sync`)")
    # completed tasks need a completed report
    for t in good:
        if t.status == "completed":
            rp = pdir / "reports" / t.path.name
            if not rp.exists():
                errors.append(f"{t.id}: completed but reports/{t.path.name} is missing")
            else:
                try:
                    rmeta, rbody = fm.parse_file(rp)
                    for e in schemas.validate_report(rmeta, rbody, t.id):
                        errors.append(f"{t.id}: report: {e}")
                except fm.FrontmatterError as exc:
                    errors.append(f"{t.id}: report unparseable: {exc}")
    return errors, warnings


def _quality_gap_warnings(root: Path, tasks: list[Task]) -> list[str]:
    """A task that needs a missing quality category must depend (transitively) on a touches_gate task."""
    from . import quality as Q

    try:
        eff = Q.effective(root)
    except Exception:  # noqa: BLE001 - detection must never break validation
        return []
    missing = {c for c, e in eff.items() if e["status"] == "missing" or (e["status"] in ("configured", "detected") and not e.get("available", True))}
    if not missing:
        return []
    by_id = task_index(tasks)

    def ancestors(tid: str, seen: set[str]) -> set[str]:
        for d in by_id[tid].deps if tid in by_id else []:
            if d not in seen and d in by_id:
                seen.add(d)
                ancestors(d, seen)
        return seen

    out = []
    for t in tasks:
        need = [c for c in (t.meta.get("quality") if t.meta.get("quality") is not None else Q.DEFAULT_PROFILE) if c in missing]
        if not need or t.meta.get("touches_gate"):
            continue
        if any(by_id[a].meta.get("touches_gate") for a in ancestors(t.id, set())):
            continue
        out.append(f"{t.id}: needs quality {need}, which is not configured, and no earlier touches_gate task establishes it")
    return out


# ----------------------------------------------------------------------- commands
def cmd_init(args) -> int:
    root = find_root(args.root)
    pdir = project_dir(root, args.project)
    if pdir.exists() and any(pdir.iterdir()):
        die(f"{pdir} already exists")
    for sub in ("tasks", "reports", "reviews", "state"):
        (pdir / sub).mkdir(parents=True, exist_ok=True)
    plan = load_template("plan.md")
    atomic_write(pdir / "plan.md", plan)
    write_json(pdir / "manifest.json", {"project": args.project, "tasks": []})
    emit({"project": args.project, "dir": str(pdir)}, args.json, f"initialized {pdir}")
    return 0


def next_task_number(pdir: Path) -> int:
    nums = [0]
    for p in (pdir / "tasks").glob("*.md"):
        m = schemas.TASK_FILE_RE.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) + 1


def cmd_new_task(args) -> int:
    root = find_root(args.root)
    pdir = require_project(root, args.project)
    n = next_task_number(pdir)
    tid = f"TASK-{n:03d}"
    slug = slugify(args.slug or args.title)
    deps = [d.strip() for d in (args.deps or "").split(",") if d.strip()]
    text = (
        load_template("task.md")
        .replace("__ID__", tid)
        .replace("__TITLE__", fm._fmt_scalar(args.title))
        .replace("__DEPS__", "[" + ", ".join(deps) + "]")
        .replace("__PRIORITY__", args.priority)
    )
    path = pdir / "tasks" / f"{n:03d}-{slug}.md"
    atomic_write(path, text)
    sync_manifest(root, args.project)
    emit({"id": tid, "file": str(path.relative_to(pdir))}, args.json, f"created {path}")
    return 0


ATEAM_CMD = "python3 .opencode/scripts/ateam.py"


def create_adhoc(root: Path, request: str, project: str | None = None) -> tuple[str, str]:
    """Fast path: a one-task project built from the request, with no architect.

    The task still carries a verify block, the detected quality profile and a manual criterion that the final
    review checks against the diff. Returns (project, task id).
    """
    from . import quality as Q

    request = request.strip()
    if not request:
        die("adhoc request is empty")
    first = request.splitlines()[0].strip()
    title = first[:80].rstrip()
    if project is None:
        stamp = _dt_stamp()
        project = f"adhoc-{stamp}-{slugify(first, 24)}"
    pdir = project_dir(root, project)
    if pdir.exists() and any(pdir.iterdir()):
        die(f"{pdir} already exists")
    for sub in ("tasks", "reports", "reviews", "state"):
        (pdir / sub).mkdir(parents=True, exist_ok=True)
    quoted = "\n".join("> " + ln if ln.strip() else ">" for ln in request.splitlines())
    atomic_write(
        pdir / "plan.md",
        "# Objective\n"
        f"{first}\n\n# Scope and Non-Goals\nSingle request on the fast path; no decomposition.\n\n"
        "# Architectural Decisions\nNone (no architect run).\n\n# Shared Constraints\nFollow the project conventions.\n\n"
        "# Task Graph\nTASK-001\n\n# Acceptance Criteria\nSee TASK-001.\n\n# Progress\n0/1 completed.\n",
    )
    eff = Q.effective(root)
    cats = [c for c in Q.DEFAULT_PROFILE if eff[c]["status"] in ("configured", "detected", "not_applicable") and eff[c].get("available", True)]
    crit = [
        "- AC1: The request is implemented as stated. [manual]",
        "- AC2: The project plan stays valid. [check: plan]",
    ]
    if cats:
        crit.append(f"- AC3: The configured quality checks pass. [check: {','.join(cats)}]")
    text = (
        "---\n"
        "id: TASK-001\n"
        f"title: {fm._fmt_scalar(title)}\n"
        "status: pending\n"
        "dependencies: []\n"
        "priority: medium\n"
        "review: none\n"
        "max_attempts: 3\n"
        "files: []\n"
        "reads: []\n"
        "knowledge: []\n"
        "skills: []\n"
        f"quality: [{', '.join(cats)}]\n"
        "adhoc: true\n"
        "---\n\n"
        f"# Objective\nImplement the request quoted under Context, with the smallest change that satisfies it.\n\n"
        "# Context\n" + quoted + "\n\n"
        "# Files to Inspect\nChosen by the coder from the request; read only what is needed.\n\n"
        "# Acceptance Criteria\n" + "\n".join(crit) + "\n\n"
        "# Verification Commands\n\n```verify\n"
        f"plan: {ATEAM_CMD} work validate {project}\n"
        "```\n"
    )
    atomic_write(pdir / "tasks" / "001-request.md", text)
    write_json(pdir / "manifest.json", {"project": project, "tasks": []})
    sync_manifest(root, project)
    return project, "TASK-001"


def _dt_stamp() -> str:
    import datetime as dt

    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def cmd_adhoc(args) -> int:
    root = find_root(args.root)
    request = args.request if args.request != "-" else sys.stdin.read()
    project, tid = create_adhoc(root, request, args.project)
    errors, _ = validate_project(root, project)
    if errors:
        die("adhoc project is invalid: " + "; ".join(errors))
    emit({"project": project, "task": tid}, args.json, f"{project} {tid}")
    return 0


LEGACY_FILES = {"PLAN.md": "legacy/PLAN.md", "REVUE.md": "reviews/legacy-REVUE.md", "TESTS.md": "reviews/legacy-TESTS.md"}


def migrate_legacy(root: Path, project: str, move: bool = False) -> dict:
    """Archive the v0 `equipe/` folder into a work project. No tasks are invented: the architect re-plans from legacy/PLAN.md."""
    import shutil

    src = root / "equipe"
    if not src.is_dir():
        die(f"{src} does not exist; nothing to migrate")
    pdir = project_dir(root, project)
    if pdir.exists() and any(pdir.iterdir()):
        die(f"{pdir} already exists")
    for sub in ("tasks", "reports", "reviews", "state", "legacy"):
        (pdir / sub).mkdir(parents=True, exist_ok=True)
    copied = []
    for name, dst in LEGACY_FILES.items():
        if (src / name).exists():
            shutil.copy2(src / name, pdir / dst)
            copied.append(dst)
    for extra in ("etapes", "essais"):
        if (src / extra).is_dir():
            shutil.copytree(src / extra, pdir / "legacy" / extra, dirs_exist_ok=True)
            copied.append(f"legacy/{extra}/")
    plan_src = (pdir / "legacy" / "PLAN.md")
    objective = "Continue the work described in legacy/PLAN.md."
    if plan_src.exists():
        paras = [p.strip() for p in plan_src.read_text(encoding="utf-8", errors="replace").split("\n\n") if p.strip() and not p.strip().startswith("#")]
        if paras:
            objective = paras[0][:500]
    atomic_write(
        pdir / "plan.md",
        f"# Objective\n{objective}\n\n# Scope and Non-Goals\nMigrated from `equipe/`; see legacy/PLAN.md. Re-scope before executing.\n\n"
        "# Architectural Decisions\nSee legacy/PLAN.md.\n\n# Shared Constraints\nSee legacy/PLAN.md.\n\n"
        "# Task Graph\nNo tasks yet: run the architect to decompose the remaining work.\n\n"
        "# Acceptance Criteria\nSee legacy/PLAN.md.\n\n# Progress\n0/0 completed (migrated).\n",
    )
    write_json(pdir / "manifest.json", {"project": project, "tasks": []})
    if move:
        shutil.move(str(src), str(root / "equipe.migrated"))
    return {"project": project, "dir": str(pdir), "copied": copied, "moved_to": "equipe.migrated" if move else None}


def cmd_migrate_legacy(args) -> int:
    root = find_root(args.root)
    res = migrate_legacy(root, args.project, args.move)
    emit(res, args.json, f"migrated {len(res['copied'])} item(s) into {res['dir']}; run the architect to create tasks")
    return 0


def cmd_sync(args) -> int:
    root = find_root(args.root)
    m = sync_manifest(root, args.project)
    emit(m, args.json, f"manifest synced ({len(m['tasks'])} tasks)")
    return 0


def cmd_validate(args) -> int:
    root = find_root(args.root)
    errors, warnings = validate_project(root, args.project)
    if args.json:
        emit({"ok": not errors, "errors": errors, "warnings": warnings}, True)
    else:
        for w in warnings:
            print(f"WARN  {w}")
        for e in errors:
            print(f"ERROR {e}")
        print("OK" if not errors else f"FAILED: {len(errors)} error(s)")
    return 0 if not errors else 1


def cmd_graph(args) -> int:
    root = find_root(args.root)
    pdir = require_project(root, args.project)
    tasks = [t for t in load_tasks(pdir) if not t.id.startswith("?")]
    order = topo_order(tasks)
    by_id = task_index(tasks)
    if args.mermaid:
        lines = ["graph TD"]
        for i in order:
            lines.append(f'  {i.replace("-", "_")}["{i}: {by_id[i].meta.get("title", "")} ({by_id[i].status})"]')
        for i in order:
            for d in by_id[i].deps:
                lines.append(f'  {d.replace("-", "_")} --> {i.replace("-", "_")}')
        print("\n".join(lines))
    elif args.json:
        emit({"order": order, "edges": {i: by_id[i].deps for i in order}}, True)
    else:
        for i in order:
            deps = ",".join(by_id[i].deps) or "-"
            print(f"{i}  [{by_id[i].status}]  deps={deps}  {by_id[i].meta.get('title', '')}")
    return 0


def ready_tasks(tasks: list[Task]) -> list[Task]:
    by_id = task_index(tasks)
    out = [
        t
        for t in tasks
        if t.status == "pending" and all(by_id.get(d) and by_id[d].status == "completed" for d in t.deps)
    ]
    out.sort(key=lambda t: (PRIORITY_RANK.get(t.meta.get("priority"), 1), t.id))
    return out


def cmd_status(args) -> int:
    root = find_root(args.root)
    pdir = require_project(root, args.project)
    tasks = [t for t in load_tasks(pdir) if not t.id.startswith("?")]
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.status] = counts.get(t.status, 0) + 1
    data = {
        "project": args.project,
        "counts": counts,
        "total": len(tasks),
        "ready": [t.id for t in ready_tasks(tasks)],
        "completed": counts.get("completed", 0) == len(tasks) and len(tasks) > 0,
    }
    text = f"{args.project}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) + f" | ready: {', '.join(data['ready']) or '-'}"
    emit(data, args.json, text)
    return 0


def cmd_list(args) -> int:
    root = find_root(args.root)
    wr = work_root(root)
    names = sorted(p.name for p in wr.iterdir() if p.is_dir()) if wr.is_dir() else []
    emit(names, args.json, "\n".join(names) or "(no projects)")
    return 0


def register(sub) -> None:
    p = sub.add_parser("work", help="work projects: plan, tasks, manifest, state")
    ws = p.add_subparsers(dest="work_cmd", required=True)

    def add(name, fn, help_):
        sp = ws.add_parser(name, help=help_)
        sp.set_defaults(func=fn)
        sp.add_argument("--json", action="store_true")
        return sp

    sp = add("init", cmd_init, "scaffold .ateam/work/<project>")
    sp.add_argument("project")
    sp = add("new-task", cmd_new_task, "create the next numbered task file from the template")
    sp.add_argument("project")
    sp.add_argument("title")
    sp.add_argument("--slug")
    sp.add_argument("--deps", default="")
    sp.add_argument("--priority", default="medium", choices=schemas.PRIORITIES)
    sp = add("adhoc", cmd_adhoc, "fast path: create a one-task project from a request (use '-' to read stdin)")
    sp.add_argument("request")
    sp.add_argument("--project")
    sp = add("migrate-legacy", cmd_migrate_legacy, "archive the v0 equipe/ folder into a work project")
    sp.add_argument("project", nargs="?", default="legacy")
    sp.add_argument("--move", action="store_true", help="rename equipe/ to equipe.migrated afterwards")
    sp = add("sync", cmd_sync, "rebuild manifest.json from task files")
    sp.add_argument("project")
    sp = add("validate", cmd_validate, "validate plan, tasks, manifest, graph")
    sp.add_argument("project")
    sp = add("graph", cmd_graph, "print dependency order")
    sp.add_argument("project")
    sp.add_argument("--mermaid", action="store_true")
    sp = add("status", cmd_status, "compact status and ready tasks")
    sp.add_argument("project")
    add("list", cmd_list, "list work projects")
    from . import gate

    gate.add_commands(ws, add)
    from . import lifecycle

    lifecycle.add_commands(ws, add)
