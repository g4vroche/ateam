"""Schemas and structural validators for ateam artifacts. Each validator returns a list of error strings."""
from __future__ import annotations

import re
from typing import Any

TASK_ID_RE = re.compile(r"^TASK-(\d{3,})$")
TASK_FILE_RE = re.compile(r"^(\d{3,})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
RUN_ID_RE = re.compile(r"^RUN-[0-9]{8}-[0-9]{3,}$")
FB_ID_RE = re.compile(r"^FB-\d{6}$")
KNOW_ID_RE = re.compile(r"^KNOW-\d{4,}$")
PROP_ID_RE = re.compile(r"^PROP-\d{3,}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

TASK_STATUSES = (
    "pending",
    "in_progress",
    "gate_failed",
    "needs_review",
    "needs_architect",
    "blocked",
    "completed",
)
PRIORITIES = ("low", "medium", "high")
REVIEW_MODES = ("none", "required")
GATE_STATES = ("passed", "failed", "not_executed", "not_applicable", "needs_review")
QUALITY_CATEGORIES = (
    "format",
    "lint",
    "typecheck",
    "test",
    "build",
    "security",
    "deps",
    "coverage",
    "architecture",
)
FEEDBACK_CATEGORIES = (
    "tooling-gap",
    "unclear-instructions",
    "repeated-mistake",
    "context-overload",
    "testing-difficulty",
    "documentation-gap",
    "skill-deficiency",
    "orchestration",
    "dependency",
    "environment",
    "unnecessary-llm-call",
    "other",
)
SEVERITIES = ("low", "medium", "high")
FEEDBACK_STATUSES = ("open", "addressed", "dismissed")
FEEDBACK_SOURCES = ("signal", "agent")
KNOWLEDGE_CATEGORIES = (
    "architecture",
    "conventions",
    "decisions",
    "troubleshooting",
    "workflows",
    "projects",
    "lessons",
)
CONFIDENCE = ("verified", "hypothesis")
KNOWLEDGE_STATUSES = ("active", "contradicted", "deprecated")
PROPOSAL_TYPES = ("automation", "skill", "agent-instruction", "architecture", "knowledge")
PROPOSAL_STATUSES = ("proposed", "validated", "active", "deprecated", "rejected")
SKILL_LIFECYCLE = ("proposed", "validated", "active", "deprecated")
REPORT_STATUSES = ("completed", "partial", "blocked")

PLAN_SECTIONS = (
    "Objective",
    "Scope and Non-Goals",
    "Architectural Decisions",
    "Shared Constraints",
    "Task Graph",
    "Acceptance Criteria",
    "Progress",
)
TASK_REQUIRED_SECTIONS = ("Objective", "Files to Inspect", "Acceptance Criteria", "Verification Commands")
TASK_OPTIONAL_SECTIONS = (
    "Context",
    "Required Inputs",
    "Expected Changes",
    "Constraints",
    "Expected Deliverables",
)
REPORT_SECTIONS = (
    "Files Changed",
    "Decisions Made",
    "Verification Results",
    "Outputs for Dependent Tasks",
    "Known Limitations",
)
FEEDBACK_SECTIONS = (
    "Problem",
    "Context",
    "Symptoms",
    "Root Cause",
    "Resolution Attempted",
    "Successful Workaround",
    "Proposed Improvement",
    "Reusability Assessment",
    "Related References",
)
FEEDBACK_REQUIRED_NONEMPTY = ("Problem", "Related References")

TRIVIAL_COMMANDS = {"true", ":", "echo", "exit 0", "/bin/true", "test 1", "pass"}


# ----------------------------------------------------------------- body helpers
_FENCE_RE = re.compile(r"^(```+|~~~+)")


def parse_sections(body: str) -> dict[str, str]:
    """Split a Markdown body on level-1 headings (outside code fences)."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    in_fence: str | None = None
    for line in body.split("\n"):
        fence = _FENCE_RE.match(line.strip())
        if fence:
            marker = fence.group(1)[0]
            if in_fence is None:
                in_fence = marker
            elif in_fence == marker:
                in_fence = None
        if in_fence is None and line.startswith("# "):
            current = line[2:].strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip("\n") for k, v in sections.items()}


def extract_verify(body: str) -> list[tuple[str, str]]:
    """Return [(check_id, command)] from the ```verify block of the Verification Commands section."""
    sec = parse_sections(body).get("Verification Commands", "")
    m = re.search(r"```verify[^\n]*\n(.*?)```", sec, re.S)
    if not m:
        return []
    out = []
    for raw in m.group(1).split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cid, sep, cmd = line.partition(":")
        if sep and re.fullmatch(r"[a-z][a-z0-9_-]*", cid.strip()) and cmd.strip():
            out.append((cid.strip(), cmd.strip()))
        else:
            out.append(("", line))
    return out


_AC_RE = re.compile(r"^\s*[-*]\s*(?:\[[ xX]\]\s*)?(AC\d+)\s*[:.)-]\s*(.+)$")
_CHECK_TAG_RE = re.compile(r"\[check:\s*([a-z0-9_,\s-]+)\]")


def acceptance_criteria(body: str) -> list[dict]:
    """Parse criteria as '- AC1: text [check: id1,id2]' or '... [manual]'."""
    sec = parse_sections(body).get("Acceptance Criteria", "")
    out = []
    for line in sec.split("\n"):
        m = _AC_RE.match(line)
        if not m:
            continue
        text = m.group(2)
        checks: list[str] = []
        tag = _CHECK_TAG_RE.search(text)
        if tag:
            checks = [c.strip() for c in tag.group(1).split(",") if c.strip()]
        out.append({"id": m.group(1), "text": text, "checks": checks, "manual": "[manual]" in text})
    return out


def is_trivial_command(cmd: str) -> bool:
    c = cmd.strip()
    if c in TRIVIAL_COMMANDS:
        return True
    first = c.split()[0] if c.split() else ""
    return first in ("true", ":", "echo", "printf") and "&&" not in c and "|" not in c


# -------------------------------------------------------------------- validators
def _is_list_of_str(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def validate_task(meta: dict, body: str, filename: str | None = None) -> list[str]:
    errs: list[str] = []
    tid = meta.get("id")
    if not isinstance(tid, str) or not TASK_ID_RE.match(tid):
        errs.append(f"id must match TASK-NNN, got {tid!r}")
    if not meta.get("title") or not isinstance(meta.get("title"), str):
        errs.append("title is required")
    if meta.get("status") not in TASK_STATUSES:
        errs.append(f"status must be one of {TASK_STATUSES}, got {meta.get('status')!r}")
    deps = meta.get("dependencies")
    if deps is None:
        deps = []
    if not _is_list_of_str(deps):
        errs.append("dependencies must be a list of task ids")
    else:
        for d in deps:
            if not TASK_ID_RE.match(d):
                errs.append(f"dependency {d!r} is not a task id")
            if d == tid:
                errs.append("task depends on itself")
    if meta.get("priority") not in PRIORITIES:
        errs.append(f"priority must be one of {PRIORITIES}")
    if meta.get("review", "none") not in REVIEW_MODES:
        errs.append(f"review must be one of {REVIEW_MODES}")
    ma = meta.get("max_attempts", 3)
    if not isinstance(ma, int) or isinstance(ma, bool) or not 1 <= ma <= 10:
        errs.append("max_attempts must be an integer in 1..10")
    for flag in ("touches_gate", "allow_no_changes"):
        if flag in meta and not isinstance(meta[flag], bool):
            errs.append(f"{flag} must be true or false")
    if meta.get("touches_gate") is True and meta.get("review") != "required":
        errs.append("a task with touches_gate: true must set review: required")
    for key in ("files", "reads", "knowledge", "skills", "quality"):
        v = meta.get(key)
        if v is not None and not _is_list_of_str(v):
            errs.append(f"{key} must be a list of strings")
    for q in meta.get("quality") or []:
        if q not in QUALITY_CATEGORIES:
            errs.append(f"quality category {q!r} unknown (allowed: {QUALITY_CATEGORIES})")
    for k in meta.get("knowledge") or []:
        if not KNOW_ID_RE.match(k):
            errs.append(f"knowledge ref {k!r} is not a KNOW id")
    if filename is not None:
        m = TASK_FILE_RE.match(filename)
        if not m:
            errs.append(f"file name {filename!r} must be NNN-kebab-slug.md")
        elif isinstance(tid, str) and TASK_ID_RE.match(tid) and int(m.group(1)) != int(tid[5:]):
            errs.append(f"file number {m.group(1)} does not match id {tid}")
    sections = parse_sections(body)
    for sec in TASK_REQUIRED_SECTIONS:
        if not sections.get(sec, "").strip():
            errs.append(f"section '# {sec}' is missing or empty")
    verify = extract_verify(body)
    if "Verification Commands" in sections and not verify:
        errs.append("'# Verification Commands' needs a ```verify block with 'check-id: command' lines")
    seen = set()
    for cid, cmd in verify:
        if not cid:
            errs.append(f"verify line without 'check-id:' prefix: {cmd!r}")
            continue
        if cid in seen:
            errs.append(f"duplicate verify check id {cid!r}")
        seen.add(cid)
        if is_trivial_command(cmd):
            errs.append(f"verify check {cid!r} uses a trivial command ({cmd!r})")
    available = seen | set(meta.get("quality") or [])
    crits = acceptance_criteria(body)
    if "Acceptance Criteria" in sections and not crits:
        errs.append("Acceptance Criteria must list items like '- AC1: text [check: id]' or '[manual]'")
    for c in crits:
        if not c["checks"] and not c["manual"]:
            errs.append(f"{c['id']} has neither [check: ...] nor [manual]")
        for chk in c["checks"]:
            if chk not in available:
                errs.append(f"{c['id']} references unknown check {chk!r}")
    return errs


def validate_plan(body: str) -> list[str]:
    errs: list[str] = []
    sections = parse_sections(body)
    for sec in PLAN_SECTIONS:
        if sec not in sections:
            errs.append(f"plan section '# {sec}' is missing")
        elif not sections[sec].strip():
            errs.append(f"plan section '# {sec}' is empty")
    return errs


def validate_manifest(m: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(m, dict):
        return ["manifest must be a JSON object"]
    if not isinstance(m.get("project"), str) or not m.get("project"):
        errs.append("manifest.project must be a non-empty string")
    tasks = m.get("tasks")
    if not isinstance(tasks, list):
        return errs + ["manifest.tasks must be a list"]
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            errs.append(f"manifest.tasks[{i}] must be an object")
            continue
        for k in ("id", "file", "status", "dependencies"):
            if k not in t:
                errs.append(f"manifest.tasks[{i}] missing '{k}'")
        if t.get("status") not in TASK_STATUSES:
            errs.append(f"manifest.tasks[{i}].status invalid: {t.get('status')!r}")
        extra = set(t) - {"id", "file", "status", "dependencies", "priority"}
        if extra:
            errs.append(f"manifest.tasks[{i}] has unexpected keys {sorted(extra)} (manifest is an index only)")
    return errs


def validate_report(meta: dict, body: str, task_id: str | None = None) -> list[str]:
    errs: list[str] = []
    if task_id and meta.get("task") != task_id:
        errs.append(f"report task {meta.get('task')!r} != {task_id}")
    if meta.get("status") not in REPORT_STATUSES:
        errs.append(f"report status must be one of {REPORT_STATUSES}")
    if not isinstance(meta.get("attempt"), int):
        errs.append("report attempt must be an integer")
    sections = parse_sections(body)
    for s in REPORT_SECTIONS:
        if s not in sections:
            errs.append(f"report section '# {s}' missing")
    return errs


def validate_feedback(meta: dict, body: str) -> list[str]:
    errs: list[str] = []
    if not FB_ID_RE.match(str(meta.get("id", ""))):
        errs.append("id must match FB-NNNNNN")
    if not RUN_ID_RE.match(str(meta.get("execution_id", ""))):
        errs.append("execution_id must match RUN-YYYYMMDD-NNN")
    tid = meta.get("task_id")
    if tid not in (None, "none") and not TASK_ID_RE.match(str(tid)):
        errs.append("task_id must be a TASK id or 'none'")
    if not meta.get("agent"):
        errs.append("agent is required")
    if meta.get("category") not in FEEDBACK_CATEGORIES:
        errs.append(f"category must be one of {FEEDBACK_CATEGORIES}")
    if meta.get("severity") not in SEVERITIES:
        errs.append(f"severity must be one of {SEVERITIES}")
    if meta.get("status") not in FEEDBACK_STATUSES:
        errs.append(f"status must be one of {FEEDBACK_STATUSES}")
    if meta.get("source") not in FEEDBACK_SOURCES:
        errs.append(f"source must be one of {FEEDBACK_SOURCES}")
    if not meta.get("signature") or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", str(meta.get("signature"))):
        errs.append("signature must be a kebab/dot slug")
    sections = parse_sections(body)
    for s in FEEDBACK_REQUIRED_NONEMPTY:
        if not sections.get(s, "").strip():
            errs.append(f"section '# {s}' must not be empty")
    return errs


def validate_knowledge(meta: dict, body: str) -> list[str]:
    errs: list[str] = []
    if not KNOW_ID_RE.match(str(meta.get("id", ""))):
        errs.append("id must match KNOW-NNNN")
    if meta.get("category") not in KNOWLEDGE_CATEGORIES:
        errs.append(f"category must be one of {KNOWLEDGE_CATEGORIES}")
    for k in ("created_at", "updated_at"):
        if not DATE_RE.match(str(meta.get(k, ""))):
            errs.append(f"{k} must be YYYY-MM-DD")
    if meta.get("confidence") not in CONFIDENCE:
        errs.append(f"confidence must be one of {CONFIDENCE}")
    if meta.get("status", "active") not in KNOWLEDGE_STATUSES:
        errs.append(f"status must be one of {KNOWLEDGE_STATUSES}")
    if not meta.get("title"):
        errs.append("title is required")
    if not meta.get("key") or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", str(meta.get("key"))):
        errs.append("key must be a kebab/dot slug (dedupe key)")
    src = meta.get("sources")
    if not _is_list_of_str(src) or not src:
        errs.append("sources must be a non-empty list")
    if not body.strip():
        errs.append("body must not be empty")
    return errs


def validate_proposal(meta: dict, body: str) -> list[str]:
    errs: list[str] = []
    if not PROP_ID_RE.match(str(meta.get("id", ""))):
        errs.append("id must match PROP-NNN")
    if meta.get("type") not in PROPOSAL_TYPES:
        errs.append(f"type must be one of {PROPOSAL_TYPES}")
    if meta.get("status") not in PROPOSAL_STATUSES:
        errs.append(f"status must be one of {PROPOSAL_STATUSES}")
    if meta.get("risk") not in ("low", "high"):
        errs.append("risk must be low or high")
    fb = meta.get("feedback")
    if not _is_list_of_str(fb) or not fb:
        errs.append("feedback must list at least one FB id")
    else:
        for f in fb:
            if not FB_ID_RE.match(f):
                errs.append(f"feedback ref {f!r} invalid")
    return errs


def validate_skill(meta: dict, body: str, dirname: str) -> list[str]:
    errs: list[str] = []
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", dirname) or len(dirname) > 64:
        errs.append(f"skill directory {dirname!r} must be lowercase kebab-case (<=64)")
    desc = meta.get("description")
    if not isinstance(desc, str) or len(desc.strip()) < 20:
        errs.append("description (>= 20 chars) is required so the skill is advertised")
    elif "use when" not in desc.lower() and "use only when" not in desc.lower():
        errs.append("description must state activation criteria ('Use when ...')")
    if not meta.get("name"):
        errs.append("name is required")
    md = meta.get("metadata")
    if md is not None:
        if not isinstance(md, dict):
            errs.append("metadata must be a map")
        else:
            lc = md.get("ateam/lifecycle")
            if lc is not None and lc not in SKILL_LIFECYCLE:
                errs.append(f"metadata.ateam/lifecycle must be one of {SKILL_LIFECYCLE}")
    return errs
