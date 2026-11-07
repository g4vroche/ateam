"""Static verification of the boilerplate itself: agents (resolved), skills, work projects."""
from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
from pathlib import Path

from . import frontmatter as fm
from . import schemas
from .common import emit, find_root, load_config
from .work import validate_project, work_root

# Hidden French aliases kept for one release: alias -> canonical agent.
ALIASES = {"architecte": "architect", "dev": "coder", "testeur": "tester"}

REQUIRED_AGENTS = ("chef", "architect", "coder", "tech-lead", "tester")
OPTIONAL_AGENTS = ("skillsmith", "dreamer")

ATEAM = "python3 .opencode/scripts/ateam.py"

# (agent, action, resource, expected effect). Evaluated with last-match-wins semantics.
PERMISSION_CONTRACT: list[tuple[str, str, str, str]] = [
    ("coder", "edit", ".opencode/agents/chef.md", "deny"),
    ("coder", "edit", ".opencode/scripts/ateam.py", "deny"),
    ("coder", "edit", ".ateam/quality.json", "deny"),
    ("coder", "edit", ".ateam/work/demo/tasks/001-x.md", "deny"),
    ("coder", "edit", ".ateam/work/demo/manifest.json", "deny"),
    ("coder", "edit", ".ateam/work/demo/reports/001-x.md", "allow"),
    ("coder", "read", ".ateam/work/demo/plan.md", "deny"),
    ("coder", "read", ".ateam/work/demo/manifest.json", "deny"),
    ("coder", "shell", f"{ATEAM} work complete demo TASK-001", "deny"),
    ("coder", "shell", f"{ATEAM} work gate demo TASK-001", "deny"),
    ("coder", "shell", f"{ATEAM} run demo", "deny"),
    ("coder", "shell", f"{ATEAM} quality all", "allow"),
    ("coder", "subagent", "coder", "deny"),
    ("coder", "shell", f"{ATEAM} feedback note --category x", "allow"),
    ("coder", "shell", "python3 ./.opencode/scripts/ateam.py work complete demo TASK-001", "deny"),
    ("coder", "shell", "cd . && python .opencode/scripts/ateam.py work gate demo TASK-001", "deny"),
    ("coder", "shell", "pytest -q", "allow"),
    ("coder", "edit", ".ateam/work/demo/state/TASK-001.json", "deny"),
    ("coder", "edit", ".opencode/skills/x/SKILL.md", "deny"),
    ("coder", "edit", "src/app.py", "allow"),
    ("coder", "read", ".env", "deny"),
    ("coder", "external_directory", "/etc/passwd", "deny"),
    ("tech-lead", "edit", ".ateam/work/demo/reviews/TASK-001-r1.md", "allow"),
    ("tech-lead", "shell", "git diff --stat", "allow"),
    ("tech-lead", "shell", "rm -rf /", "deny"),
    ("tech-lead", "shell", f"{ATEAM} work complete demo TASK-001", "deny"),
    ("tester", "edit", ".ateam/work/demo/reviews/exploratory/t.sh", "allow"),
    ("tester", "shell", f"{ATEAM} work complete demo TASK-001", "deny"),
    ("tester", "shell", f"{ATEAM} run demo", "deny"),
    ("tester", "shell", "python3 app.py", "allow"),
    ("chef", "edit", "src/app.py", "deny"),
    ("chef", "shell", f"{ATEAM} run demo", "allow"),
    ("chef", "shell", "rm -rf /", "deny"),
    ("chef", "shell", f"{ATEAM} status calc", "allow"),
    ("chef", "shell", f"{ATEAM} work reopen demo TASK-001", "deny"),
    ("chef", "shell", f"{ATEAM} quality set test --cmd x", "deny"),
    ("chef", "subagent", "architect", "allow"),
    ("chef", "subagent", "architecte", "deny"),
    ("chef", "skill", "manual-orchestration", "allow"),
    ("architect", "edit", "src/app.py", "deny"),
    ("architect", "edit", ".ateam/work/demo/plan.md", "allow"),
    ("architect", "edit", ".ateam/work/demo/tasks/002-y.md", "allow"),
    ("architect", "shell", "rm -rf /", "deny"),
    ("architect", "shell", f"{ATEAM} work validate demo", "allow"),
    ("architect", "shell", f"{ATEAM} work init demo", "allow"),
    ("architect", "shell", f"{ATEAM} work split demo TASK-002", "allow"),
    ("architect", "shell", f"{ATEAM} work reopen demo TASK-002", "deny"),
    ("architect", "shell", f"{ATEAM} quality detect --json", "allow"),
    ("architect", "shell", f"{ATEAM} work complete demo TASK-001", "deny"),
    ("architect", "shell", f"{ATEAM} work gate demo TASK-001", "deny"),
    ("architect", "shell", f"{ATEAM} quality set test --cmd x", "deny"),
    ("architect", "shell", f"{ATEAM} run demo", "deny"),
    ("architect", "edit", ".ateam/quality.json", "deny"),
    ("architect", "edit", ".opencode/agents/coder.md", "deny"),
    ("architect", "edit", ".ateam/work/demo/reports/001-x.md", "deny"),
    ("architect", "edit", ".ateam/work/demo/manifest.json", "allow"),
    ("architect", "read", ".env", "deny"),
    ("tech-lead", "edit", "src/app.py", "deny"),
    ("tester", "edit", "src/app.py", "deny"),
    ("skillsmith", "edit", "src/app.py", "deny"),
    ("skillsmith", "edit", ".opencode/agents/coder.md", "deny"),
    ("skillsmith", "edit", ".ateam/proposals/PROP-001/proposal.md", "allow"),
    ("dreamer", "edit", "src/app.py", "deny"),
    ("dreamer", "edit", ".opencode/skills/x/SKILL.md", "deny"),
    ("dreamer", "edit", ".ateam/knowledge/lessons/x.md", "allow"),
    ("dreamer", "read", ".env", "deny"),
]


def _pattern_re(pattern: str) -> re.Pattern:
    out = ""
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            out += ".*"
            while i + 1 < len(pattern) and pattern[i + 1] == "*":
                i += 1
        elif c == "?":
            out += "."
        else:
            out += re.escape(c)
        i += 1
    return re.compile("^" + out + "$", re.S)


def evaluate(perms: list[dict], action: str, resource: str) -> str:
    """Last matching rule wins; no match -> 'ask'."""
    effect = "ask"
    for rule in perms:
        if _pattern_re(rule["action"]).match(action) and _pattern_re(rule["resource"]).match(resource):
            effect = rule["effect"]
    return effect


def load_resolved(path: str | None = None) -> list[dict] | None:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    if not shutil.which("opencode"):
        return None
    try:
        out = subprocess.run(["opencode", "debug", "agents"], capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout)
    except (subprocess.SubprocessError, ValueError):
        return None


def available_models() -> set[str] | None:
    if not shutil.which("opencode"):
        return None
    try:
        out = subprocess.run(["opencode", "models"], capture_output=True, text=True, timeout=60)
    except subprocess.SubprocessError:
        return None
    ids = {ln.strip() for ln in out.stdout.splitlines() if "/" in ln}
    return ids or None


def lint_agents(resolved: list[dict] | None, check_models: bool = False, strict_required: bool = True) -> list[str]:
    errs: list[str] = []
    if resolved is None:
        return ["cannot resolve agents (opencode not available and no --resolved file)"]
    by_id = {a["id"]: a for a in resolved}
    if strict_required:
        for need in REQUIRED_AGENTS:
            if need not in by_id:
                errs.append(f"required agent missing: {need}")
    for agent, action, resource, expected in PERMISSION_CONTRACT:
        a = by_id.get(agent)
        if a is None:
            continue
        got = evaluate(a["permissions"], action, resource)
        if got != expected:
            errs.append(f"{agent}: {action} {resource!r} is '{got}', contract requires '{expected}'")
    for alias, target in ALIASES.items():
        a, t = by_id.get(alias), by_id.get(target)
        if a is None:
            continue
        if t is None:
            if strict_required:
                errs.append(f"alias {alias} points to missing agent {target}")
            continue
        if a.get("hidden") is not True:
            errs.append(f"alias {alias} must be hidden")
        if _norm_perms(a["permissions"]) != _norm_perms(t["permissions"]):
            errs.append(f"alias {alias} permissions differ from {target}")
        if a.get("model") != t.get("model"):
            errs.append(f"alias {alias} model differs from {target}")
    for aid, a in by_id.items():
        if a.get("mode") == "primary" and not a.get("hidden") and aid not in ("chef", "build", "plan", "compaction", "summary", "title"):
            errs.append(f"unexpected primary agent: {aid}")
    if check_models:
        models = available_models()
        if models:
            for aid, a in by_id.items():
                m = a.get("model")
                if isinstance(m, dict) and f"{m.get('providerID')}/{m.get('id')}" not in models:
                    errs.append(f"{aid}: model {m.get('providerID')}/{m.get('id')} not available")
    return errs


def _norm_perms(perms: list[dict]) -> list[tuple]:
    return [(p["action"], p["resource"], p["effect"]) for p in perms]


_REF_RE = re.compile(r"`((?:references|scripts)/[A-Za-z0-9_./-]+)`")


def lint_skills(root: Path) -> list[str]:
    errs: list[str] = []
    sdir = root / ".opencode" / "skills"
    if not sdir.is_dir():
        return errs
    descs: dict[str, set[str]] = {}
    cfg = load_config(root)
    active = 0
    for d in sorted(p for p in sdir.iterdir() if p.is_dir()):
        sk = d / "SKILL.md"
        if not sk.exists():
            errs.append(f"{d.name}: SKILL.md missing")
            continue
        try:
            meta, body = fm.parse_file(sk)
        except fm.FrontmatterError as exc:
            errs.append(f"{d.name}: frontmatter: {exc}")
            continue
        errs.extend(f"{d.name}: {e}" for e in schemas.validate_skill(meta, body, d.name))
        for ref in _REF_RE.findall(body):
            if not (d / ref).exists():
                errs.append(f"{d.name}: referenced path {ref} does not exist")
        if len(body.split("\n")) > 140:
            errs.append(f"{d.name}: body over 140 lines; move detail into references/")
        words = set(re.findall(r"[a-z]{4,}", str(meta.get("description", "")).lower()))
        descs[d.name] = words
        lc = (meta.get("metadata") or {}).get("ateam/lifecycle", "active")
        if lc == "active":
            active += 1
    names = list(descs)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            inter = len(descs[a] & descs[b])
            union = len(descs[a] | descs[b]) or 1
            if inter / union > 0.6:
                errs.append(f"skills {a} and {b} overlap (description similarity {inter / union:.2f})")
    if active > cfg["skill_budget"]:
        errs.append(f"{active} active skills exceed skill_budget {cfg['skill_budget']}")
    return errs


def lint_work(root: Path) -> list[str]:
    errs: list[str] = []
    wr = work_root(root)
    if not wr.is_dir():
        return errs
    for p in sorted(wr.iterdir()):
        if p.is_dir():
            e, _ = validate_project(root, p.name)
            errs.extend(f"work/{p.name}: {x}" for x in e)
    return errs


def cmd_lint(args) -> int:
    root = find_root(args.root)
    errs: list[str] = []
    what = args.what
    if what in ("all", "agents"):
        resolved = load_resolved(args.resolved)
        if resolved is None and what == "all":
            print("note: skipping agents lint (opencode not available)")
        else:
            errs += [f"agents: {e}" for e in lint_agents(resolved, check_models=args.models, strict_required=not args.partial)]
    if what in ("all", "skills"):
        errs += [f"skills: {e}" for e in lint_skills(root)]
    if what in ("all", "work"):
        errs += lint_work(root)
    emit({"ok": not errs, "errors": errs}, args.json, "\n".join(errs) + ("\n" if errs else "") + ("OK" if not errs else f"FAILED: {len(errs)} error(s)"))
    return 0 if not errs else 1


def register(sub) -> None:
    p = sub.add_parser("lint", help="verify agents, skills and work projects")
    p.add_argument("what", nargs="?", default="all", choices=["all", "agents", "skills", "work"])
    p.add_argument("--resolved", help="path to a saved `opencode debug agents` JSON")
    p.add_argument("--models", action="store_true", help="check model ids via `opencode models`")
    p.add_argument("--partial", action="store_true", help="do not require every contract agent to exist")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_lint)
