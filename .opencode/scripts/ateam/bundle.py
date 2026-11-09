"""Per-role input bundles (prompts), script-generated report skeletons, verdict parsing.

Scenario B contract: a coder bundle names only its task file, the reports of its dependencies, its scope files and its
listed skills. It never names plan.md, manifest.json or another task.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import frontmatter as fm
from . import gate as G
from . import knowledge as K
from . import schemas
from . import work as W
from .common import atomic_write, read_json

CODER_SECTIONS = ("Decisions Made", "Outputs for Dependent Tasks", "Known Limitations")
VERDICTS = ("APPROVED", "CHANGES_REQUESTED", "FAILED")
_LEGACY = {"VALIDE": "APPROVED", "A REFAIRE": "CHANGES_REQUESTED", "RECALE": "FAILED"}
_VERDICT_RE = re.compile(r"^\s*\**VERDICT\**\s*:\s*\**\s*([A-Za-zÀ-ÿ_ ]+?)\s*\**\s*$", re.M | re.I)
_TASK_REF_RE = re.compile(r"^\s*(?:[-*]\s*)?\**Task\**\s*:\s*\**(TASK-\d{3,})", re.M | re.I)
PLACEHOLDER = re.compile(r"^\s*(?:-|n/?a|none\.?|todo|\.\.\.)?\s*$", re.I)


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn").upper().strip()


def parse_verdict(text: str) -> str | None:
    """Last VERDICT line wins. Accepts the legacy French verdicts."""
    found = _VERDICT_RE.findall(text or "")
    for raw in reversed(found):
        v = _norm(raw).replace(" ", "_")
        if v in VERDICTS:
            return v
        legacy = _LEGACY.get(_norm(raw))
        if legacy:
            return legacy
    return None


def review_task_refs(text: str) -> list[str]:
    seen: list[str] = []
    for m in _TASK_REF_RE.findall(text or ""):
        tid = m.upper()
        if tid not in seen:
            seen.append(tid)
    return seen


# ---------------------------------------------------------------------- reports
def report_path(pdir: Path, task: W.Task) -> Path:
    return pdir / "reports" / task.path.name


def _section_text(body: str, name: str) -> str:
    return schemas.parse_sections(body).get(name, "").strip("\n")


def render_report(task_id: str, status: str, attempt: int, files: list[tuple[str, str]], gate: dict | None, duration_s: float | None, keep: dict[str, str]) -> str:
    meta = {"task": task_id, "status": status, "attempt": attempt}
    fl = "\n".join(f"- {st} {p}" for st, p in files[:60]) or "- (no files changed yet)"
    if len(files) > 60:
        fl += f"\n- ... and {len(files) - 60} more"
    if gate:
        vr = "\n".join(f"- {c['check']}: {c['status']} ({c.get('duration_s', 0)}s)" + (f" - {c['reason']}" if c["status"] not in ("passed",) and c.get("reason") else "") for c in gate["checks"])
        vr += f"\n- overall: {gate['overall']} (gate {gate['duration_s']}s)"
    else:
        vr = "- gate not run yet"
    if duration_s is not None:
        vr += f"\n- attempt duration: {duration_s:.0f}s"
    sections = {
        "Files Changed": fl,
        "Decisions Made": keep.get("Decisions Made", ""),
        "Verification Results": vr,
        "Outputs for Dependent Tasks": keep.get("Outputs for Dependent Tasks", ""),
        "Known Limitations": keep.get("Known Limitations", ""),
    }
    body = "\n".join(f"# {name}\n{text}\n" for name, text in sections.items())
    return fm.dump(meta, body)


def update_report(root: Path, project: str, task: W.Task, attempt: int, gate: dict | None = None, duration_s: float | None = None, status: str = "partial") -> Path:
    """Regenerate the script-owned sections; keep what the coder wrote in its own sections."""
    pdir = W.project_dir(root, project)
    rp = report_path(pdir, task)
    keep: dict[str, str] = {}
    if rp.exists():
        try:
            _, body = fm.parse_file(rp)
            keep = {n: _section_text(body, n) for n in CODER_SECTIONS}
        except fm.FrontmatterError:
            keep = {}
    files = G.changed_since_start(root, pdir, task.id)
    atomic_write(rp, render_report(task.id, status, attempt, files, gate, duration_s, keep))
    return rp


def report_is_partial(rp: Path) -> bool:
    """True when the report is missing, unparseable, or none of the coder sections carries content."""
    if not rp.exists():
        return True
    try:
        _, body = fm.parse_file(rp)
    except fm.FrontmatterError:
        return True
    return all(PLACEHOLDER.match(_section_text(body, n)) is not None for n in CODER_SECTIONS)


# ----------------------------------------------------------------------- bundles
@dataclass
class Bundle:
    prompt: str
    paths: dict = field(default_factory=dict)


def rel(root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def failure_path(pdir: Path, tid: str) -> Path:
    return pdir / "state" / f"{tid}.failure.md"


def write_failure_summary(pdir: Path, tid: str, gate: dict | None, reason: str, history: list[dict]) -> Path:
    lines = [f"# Failure of {tid}", f"reason: {reason}", ""]
    if gate:
        lines.append(G.gate_summary(gate))
        for c in gate["checks"]:
            if c["status"] in ("failed", "not_executed") and c.get("excerpt"):
                lines += ["", f"## {c['check']} output (tail)", *c["excerpt"][-25:]]
    prev = [h for h in history if h.get("result") == "failed"][-3:]
    if prev:
        lines += ["", "## Previous attempts", *[f"- attempt {h['attempt']}: {h.get('reason', '')}" for h in prev]]
    lines += ["", "Fix exactly this. Do not repeat an approach that already failed."]
    p = failure_path(pdir, tid)
    atomic_write(p, "\n".join(lines) + "\n")
    return p


def coder_bundle(root: Path, project: str, task: W.Task, attempt: int, max_attempts: int, resume: bool = False, review_file: Path | None = None) -> Bundle:
    pdir = W.project_dir(root, project)
    by_id = W.task_index(W.load_tasks(pdir))
    dep_reports = []
    for d in task.deps:
        dt = by_id.get(d)
        if dt is not None:
            rp = report_path(pdir, dt)
            if rp.exists():
                dep_reports.append(rel(root, rp))
    scope = list(task.meta.get("files") or [])
    reads = list(task.meta.get("reads") or [])
    skills = list(task.meta.get("skills") or [])
    know, _missing = K.resolve_paths(root, list(task.meta.get("knowledge") or []))
    reads = reads + [k for k in know if k not in reads]
    rp = report_path(pdir, task)
    paths = {
        "task": rel(root, task.path),
        "dependency_reports": dep_reports,
        "scope": scope,
        "reads": reads,
        "skills": skills,
        "report": rel(root, rp),
    }
    lines = [
        f"Implement ONE task, attempt {attempt} of {max_attempts}.",
        f"Task file: {paths['task']}",
        "Dependency reports: " + (", ".join(dep_reports) if dep_reports else "none"),
    ]
    if scope:
        lines.append("Scope (edit only here): " + ", ".join(scope))
    if reads:
        lines.append("Read first: " + ", ".join(reads))
    if skills:
        lines.append("Skills: " + " ".join(f"@{s}" for s in skills))
    lines.append(f"Your report: {paths['report']} (fill only the sections Decisions Made, Outputs for Dependent Tasks, Known Limitations).")
    fp = failure_path(pdir, task.id)
    if fp.exists():
        paths["failure"] = rel(root, fp)
        lines.append(f"The previous attempt failed. Read {paths['failure']} and fix exactly that.")
    if review_file is not None:
        paths["review"] = rel(root, review_file)
        lines.append(f"A reviewer requested changes. Read {paths['review']} and address every finding for this task.")
    if resume:
        lines.append("This resumes an interrupted attempt: inspect the working tree and the partial report, then finish the task.")
    lines.append("Run `python3 .opencode/scripts/ateam.py quality all --changed` before you finish. Do not mark the task complete yourself.")
    return Bundle("\n".join(lines), paths)


def manual_criteria(task: W.Task) -> list[str]:
    return [f"{task.id} {c['id']}: {c['text'].replace('[manual]', '').strip()}" for c in schemas.acceptance_criteria(task.body) if c["manual"]]


def review_file_path(pdir: Path, scope: str, round_no: int) -> Path:
    return pdir / "reviews" / f"{scope}-r{round_no}.md"


def reviewer_bundle(root: Path, project: str, scope: str, tasks: list[W.Task], round_no: int, max_rounds: int, changed: list[tuple[str, str]], gate_paths: list[Path], needs_review: list[str]) -> Bundle:
    pdir = W.project_dir(root, project)
    out = review_file_path(pdir, scope, round_no)
    lines = [
        f"Review {'the final diff of project ' + project if scope == 'final' else scope}, round {round_no} of {max_rounds}.",
        "Task files: " + ", ".join(rel(root, t.path) for t in tasks),
    ]
    if scope == "final":
        lines.append(f"Plan: {rel(root, pdir / 'plan.md')}")
    reports = [rel(root, report_path(pdir, t)) for t in tasks if report_path(pdir, t).exists()]
    if reports:
        lines.append("Reports: " + ", ".join(reports))
    if gate_paths:
        lines.append("Gate results: " + ", ".join(rel(root, p) for p in gate_paths))
    lines.append("Changed files (A added, M modified, D deleted): " + (", ".join(f"{s}:{p}" for s, p in changed[:80]) or "none"))
    if len(changed) > 80:
        lines.append(f"... and {len(changed) - 80} more (use `git status`).")
    manual = [m for t in tasks for m in manual_criteria(t)]
    if manual:
        lines.append("Manual criteria you must judge:\n" + "\n".join(f"- {m}" for m in manual))
    if needs_review:
        lines.append("Gate guard findings you must judge (approve only if legitimate):\n" + "\n".join(f"- {n}" for n in needs_review))
    lines.append(f"Write your findings to {rel(root, out)}. Give each finding as: file, problem, required outcome, and `Task: TASK-NNN` for the task that owns it.")
    lines.append("End the file with exactly one last line: `VERDICT: APPROVED`, `VERDICT: CHANGES_REQUESTED` or `VERDICT: FAILED`.")
    return Bundle("\n".join(lines), {"review_file": rel(root, out), "tasks": [t.id for t in tasks]})


def tester_bundle(root: Path, project: str, round_no: int, max_rounds: int, changed: list[tuple[str, str]], tasks: list[W.Task]) -> Bundle:
    pdir = W.project_dir(root, project)
    out = review_file_path(pdir, "tester", round_no)
    lines = [
        f"Exploratory adversarial testing of project {project}, round {round_no} of {max_rounds}.",
        f"Plan: {rel(root, pdir / 'plan.md')}",
        "Task files: " + ", ".join(rel(root, t.path) for t in tasks),
        "Changed files: " + (", ".join(f"{s}:{p}" for s, p in changed[:80]) or "none"),
        f"Write findings to {rel(root, out)} and put your test scripts under {rel(root, pdir / 'reviews' / 'exploratory')}/. Never modify project source.",
        "Give each defect as: reproduction, expected, actual, and `Task: TASK-NNN` for the owning task.",
        "End the file with exactly one last line: `VERDICT: APPROVED`, `VERDICT: CHANGES_REQUESTED` or `VERDICT: FAILED`.",
    ]
    return Bundle("\n".join(lines), {"review_file": rel(root, out)})
