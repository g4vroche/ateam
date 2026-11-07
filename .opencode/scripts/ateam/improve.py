"""Improvement proposals (SkillSmith workflow): create, validate, approve, promote, deprecate.

SkillSmith writes only under `.ateam/proposals/**`. Everything that reaches `.opencode/` goes through `promote`, which
enforces the approval policy in code:
  automatic     documentation, and new deterministic scripts/validators whose tests pass
  approval      every skill (it costs description tokens on each step) and every high-risk change
  never         architecture proposals are reported to the chef; nothing is promoted
High risk = permissions, orchestration, agent prompts, commands, policy or quality gates. The effective risk is the
maximum of the declared risk and the risk computed from the drafts, so a draft cannot declare itself low-risk.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import feedback as F
from . import frontmatter as fm
from . import schemas
from . import skills_lint as SL
from .common import (
    atomic_write,
    ateam_dir,
    die,
    emit,
    file_lock,
    find_root,
    learning_enabled,
    load_config,
    load_template,
    now_iso,
    read_json,
    write_json,
)

EXIT_APPROVAL = 3
HIGH_RISK_DIRS = ("agents", "policy", "commands", "quality", "config")
PERMISSION_TEXT = re.compile(r"(?im)^\s*(?:effect:\s*allow|permissions?:|permission:|\"?allow\"?\s*:)|opencode\.jsonc|\.ateam/quality\.json")
SCRIPT_DEST = ".opencode/scripts/extra"
TEST_DEST = "tests/extra"
DOC_DEST = "docs/improvements"


def proposals_dir(root: Path) -> Path:
    return ateam_dir(root) / "proposals"


def prop_dir(root: Path, pid: str) -> Path:
    if not schemas.PROP_ID_RE.match(pid):
        die(f"invalid proposal id {pid!r}")
    d = proposals_dir(root) / pid
    if not d.is_dir():
        die(f"proposal {pid} not found")
    return d


def require_learning(root: Path, action: str) -> None:
    if not learning_enabled(root):
        die(
            f"{action} is locked: gate G1 has not passed. Run `python3 .opencode/scripts/ateam.py g1` "
            "or set learning_enabled=true in .ateam/config.json after reviewing the evidence",
            EXIT_APPROVAL,
        )


def load_proposal(d: Path) -> tuple[dict, str]:
    return fm.parse_file(d / "proposal.md")


def save_proposal(d: Path, meta: dict, body: str) -> None:
    atomic_write(d / "proposal.md", fm.dump(meta, body))


def draft_files(d: Path) -> list[Path]:
    dd = d / "drafts"
    return sorted(p for p in dd.rglob("*") if p.is_file()) if dd.is_dir() else []


# ------------------------------------------------------------------------ risk
def computed_risk(d: Path, ptype: str) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if ptype in ("agent-instruction", "architecture"):
        reasons.append(f"type {ptype} changes orchestration or agent behaviour")
    dd = d / "drafts"
    for p in draft_files(d):
        rel = p.relative_to(dd)
        if rel.parts and rel.parts[0] in HIGH_RISK_DIRS:
            reasons.append(f"draft {rel} touches {rel.parts[0]}")
        if rel.suffix in (".md", ".json", ".jsonc", ".py", ".yaml", ".yml") and rel.parts[:1] != ("skills",):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if PERMISSION_TEXT.search(text):
                reasons.append(f"draft {rel} mentions permissions or gate configuration")
        if rel.parts[:1] == ("skills",) and rel.name == "SKILL.md":
            text = p.read_text(encoding="utf-8", errors="replace")
            for pat, why in SL.SCOPE_PATTERNS:
                if pat.search(text):
                    reasons.append(f"skill draft {why}")
    return ("high" if reasons else "low"), reasons


def effective_risk(d: Path, meta: dict) -> tuple[str, list[str]]:
    computed, reasons = computed_risk(d, meta.get("type", ""))
    return ("high" if "high" in (computed, meta.get("risk")) else "low"), reasons


# -------------------------------------------------------------------- creation
def next_prop_id(root: Path) -> str:
    n = 0
    pd = proposals_dir(root)
    if pd.is_dir():
        for p in pd.iterdir():
            m = re.fullmatch(r"PROP-(\d{3,})", p.name)
            if m:
                n = max(n, int(m.group(1)))
    return f"PROP-{n + 1:03d}"


def search_capabilities(root: Path, query: str, limit: int = 5) -> list[dict]:
    tokens = set(re.findall(r"[a-z]{3,}", query.lower()))
    hits = []
    sdir = root / ".opencode" / "skills"
    if sdir.is_dir():
        for d in sorted(p for p in sdir.iterdir() if p.is_dir()):
            try:
                meta, _ = fm.parse_file(d / "SKILL.md")
            except (fm.FrontmatterError, OSError):
                continue
            words = set(re.findall(r"[a-z]{3,}", (d.name.replace("-", " ") + " " + str(meta.get("description", ""))).lower()))
            score = len(tokens & words)
            if score:
                hits.append({"kind": "skill", "id": d.name, "path": str(d.relative_to(root)), "score": score, "lifecycle": SL.lifecycle(meta), "jaccard": round(score / (len(tokens | words) or 1), 2), "coverage": round(score / (len(tokens) or 1), 2)})
    sc = root / ".opencode" / "scripts"
    if sc.is_dir():
        for p in sorted(sc.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            try:
                head = p.read_text(encoding="utf-8", errors="replace")[:600]
            except OSError:
                continue
            m = re.match(r'\s*(?:#![^\n]*\n)?\s*"""(.*?)"""', head, re.S)
            words = set(re.findall(r"[a-z]{3,}", (p.stem.replace("_", " ") + " " + (m.group(1) if m else "")).lower()))
            score = len(tokens & words)
            if score:
                hits.append({"kind": "script", "id": p.stem, "path": str(p.relative_to(root)), "score": score, "jaccard": round(score / (len(tokens | words) or 1), 2)})
    hits.sort(key=lambda h: (-h["score"], h["id"]))
    return hits[:limit]


def create_proposal(root: Path, ptype: str, title: str, feedback_ids: list[str], risk: str | None = None, allow_overlap: bool = False) -> tuple[str, Path]:
    if ptype not in schemas.PROPOSAL_TYPES:
        die(f"type must be one of {schemas.PROPOSAL_TYPES}")
    if not title.strip():
        die("title is required")
    if not feedback_ids:
        die("link at least one feedback id (--feedback FB-000001,...)")
    known = {e["id"] for e in F.read_events(root)[0]}
    missing = [f for f in feedback_ids if f not in known]
    if missing:
        die(f"unknown feedback id(s): {', '.join(missing)} (run `feedback query --open --group`)")
    if ptype == "skill" and not allow_overlap:
        for h in search_capabilities(root, title):
            if h["kind"] == "skill" and h["score"] >= 3 and h["coverage"] >= 0.7 and h["lifecycle"] != "deprecated":
                die(f"existing skill {h['id']} already covers this ({h['path']}); extend it, or pass --allow-overlap with a justification in the proposal")
    pdir = proposals_dir(root)
    pdir.mkdir(parents=True, exist_ok=True)
    with file_lock(pdir / ".lock"):
        pid = next_prop_id(root)
        d = pdir / pid
        (d / "drafts").mkdir(parents=True)
        text = (
            load_template("proposal.md")
            .replace("__ID__", pid)
            .replace("__TYPE__", ptype)
            .replace("__RISK__", risk or ("high" if ptype in ("agent-instruction", "architecture") else "low"))
            .replace("__FEEDBACK__", "\n".join(f"  - {f}" for f in feedback_ids))
            .replace("__TITLE__", fm._fmt_scalar(title.strip()))
        )
        atomic_write(d / "proposal.md", text)
    return pid, d


# ------------------------------------------------------------------ validation
def _py_ok(path: Path) -> str | None:
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except (SyntaxError, ValueError) as exc:
        return str(exc)[:200]
    return None


def _run_draft_tests(d: Path) -> tuple[bool, str]:
    dd = d / "drafts"
    tests = dd / "tests"
    if not tests.is_dir() or not list(tests.glob("test_*.py")):
        return False, "no tests in drafts/tests (scripts need passing tests before they can be promoted)"
    env = dict(os.environ, PYTHONPATH=str(dd / "scripts") + os.pathsep + os.environ.get("PYTHONPATH", ""), PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(tests), "-t", str(tests)], capture_output=True, text=True, cwd=dd, env=env, timeout=300)
    out = (p.stdout + p.stderr)
    if p.returncode != 0:
        return False, "draft tests failed: " + " | ".join(out.strip().splitlines()[-3:])
    if re.search(r"Ran 0 tests", out):
        return False, "draft tests collected zero tests"
    return True, out.strip().splitlines()[-1] if out.strip() else "ok"


def validate_proposal(root: Path, pid: str) -> dict:
    d = prop_dir(root, pid)
    errors: list[str] = []
    try:
        meta, body = load_proposal(d)
    except fm.FrontmatterError as exc:
        return {"ok": False, "errors": [f"proposal.md: {exc}"], "risk": None}
    errors.extend(schemas.validate_proposal(meta, body))
    sections = schemas.parse_sections(body)
    for need in ("Problem", "Existing Capabilities Checked", "Proposed Change", "Validation", "Rollback"):
        if not sections.get(need, "").strip():
            errors.append(f"proposal section '# {need}' is empty")
    known = {e["id"] for e in F.read_events(root)[0]}
    for f in meta.get("feedback") or []:
        if f not in known:
            errors.append(f"linked feedback {f} does not exist")
    ptype = meta.get("type")
    dd = d / "drafts"
    files = draft_files(d)
    if ptype == "skill":
        skills = sorted(p for p in (dd / "skills").glob("*") if p.is_dir()) if (dd / "skills").is_dir() else []
        if len(skills) != 1:
            errors.append("a skill proposal needs exactly one drafts/skills/<id>/ directory")
        for sd in skills:
            if (root / ".opencode" / "skills" / sd.name).exists():
                errors.append(f"skill {sd.name} is already installed; use a new id or extend the installed skill through a doc proposal")
            others = SL.installed_descriptions(root)
            errors.extend(SL.lint_skill_dir(sd, others))
            try:
                smeta, _ = fm.parse_file(sd / "SKILL.md")
                if SL.lifecycle(smeta) != "proposed":
                    errors.append(f"{sd.name}: draft must declare metadata.ateam/lifecycle: proposed")
            except (fm.FrontmatterError, OSError):
                pass
            if len(others) + 1 > load_config(root)["skill_budget"]:
                errors.append(f"installing {sd.name} would exceed skill_budget {load_config(root)['skill_budget']} (advertised skills cost tokens on every step)")
    elif ptype == "automation":
        scripts = sorted((dd / "scripts").glob("*.py")) if (dd / "scripts").is_dir() else []
        docs = sorted((dd / "docs").glob("*.md")) if (dd / "docs").is_dir() else []
        if not scripts and not docs:
            errors.append("an automation proposal needs drafts/scripts/*.py (with tests) or drafts/docs/*.md")
        for s in scripts:
            bad = _py_ok(s)
            if bad:
                errors.append(f"{s.name}: does not compile: {bad}")
        if scripts:
            ok, msg = _run_draft_tests(d)
            if not ok:
                errors.append(msg)
        others = [p for p in files if p.relative_to(dd).parts[0] not in ("scripts", "tests", "docs")]
        if others:
            errors.append("automation drafts may only contain scripts/, tests/ and docs/: " + ", ".join(str(p.relative_to(dd)) for p in others[:3]))
    elif ptype == "agent-instruction":
        if not [p for p in files if p.relative_to(dd).parts[0] == "agents"]:
            errors.append("an agent-instruction proposal needs drafts/agents/<name>.md")
    elif ptype in ("knowledge", "architecture") and files:
        errors.append(f"{ptype} proposals carry no drafts")
    risk, reasons = effective_risk(d, meta)
    result = {"ok": not errors, "errors": errors, "risk": risk, "risk_reasons": reasons, "at": now_iso()}
    write_json(d / "validation.json", result)
    if not errors and meta.get("status") == "proposed":
        meta["status"] = "validated"
        meta["risk"] = risk
        save_proposal(d, meta, body)
    elif not errors:
        meta["risk"] = risk
        save_proposal(d, meta, body)
    return result


# --------------------------------------------------------------- approve/promote
def needs_approval(meta: dict, risk: str) -> str | None:
    if meta["type"] == "skill":
        return "skills always need explicit approval (each active skill costs description tokens on every step)"
    if risk == "high":
        return "high-risk change (permissions, orchestration, agent prompts, commands, policy or quality gates)"
    return None


def approve(root: Path, pid: str, by: str) -> None:
    require_learning(root, "improve approve")
    d = prop_dir(root, pid)
    meta, body = load_proposal(d)
    if not by.strip():
        die("--by <name> is required: approval is a human decision")
    if meta.get("status") != "validated":
        die(f"{pid} is {meta.get('status')}; only validated proposals can be approved (run `improve validate`)")
    res = validate_proposal(root, pid)
    if not res["ok"]:
        die("validation failed: " + "; ".join(res["errors"][:3]))
    meta, body = load_proposal(d)
    meta["approved_by"] = by.strip()
    meta["approved_at"] = now_iso()
    save_proposal(d, meta, body)


def _copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))


def promote(root: Path, pid: str) -> dict:
    require_learning(root, "improve promote")
    d = prop_dir(root, pid)
    meta, body = load_proposal(d)
    if meta["type"] == "architecture":
        die("architecture proposals are reported to the chef and never promoted automatically", EXIT_APPROVAL)
    if meta.get("status") != "validated":
        die(f"{pid} is {meta.get('status')}; promote requires a validated proposal", EXIT_APPROVAL)
    res = validate_proposal(root, pid)  # revalidate: drafts may have changed since
    if not res["ok"]:
        die("validation failed: " + "; ".join(res["errors"][:3]), EXIT_APPROVAL)
    meta, body = load_proposal(d)
    why = needs_approval(meta, res["risk"])
    if why and not meta.get("approved_by"):
        die(f"{pid} cannot be promoted without explicit approval: {why}. A human runs `improve approve {pid} --by <name>`.", EXIT_APPROVAL)
    dd = d / "drafts"
    installed: list[str] = []
    backups: dict[Path, Path | None] = {}
    tmpdir = Path(tempfile.mkdtemp(prefix="promote-"))

    def backup(target: Path) -> None:
        if target in backups:
            return
        if target.exists():
            b = tmpdir / str(len(backups))
            shutil.copytree(target, b) if target.is_dir() else shutil.copy2(target, b)
            backups[target] = b
        else:
            backups[target] = None

    def restore() -> None:
        for target, b in backups.items():
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
            if b is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(b, target) if b.is_dir() else shutil.copy2(b, target)

    try:
        t = meta["type"]
        if t == "skill":
            sd = next((dd / "skills").iterdir())
            target = root / ".opencode" / "skills" / sd.name
            backup(target)
            _copy_tree(sd, target)
            smeta, sbody = fm.parse_file(target / "SKILL.md")
            md = dict(smeta.get("metadata") or {})
            md["ateam/lifecycle"] = "active"
            smeta["metadata"] = md
            atomic_write(target / "SKILL.md", fm.dump(smeta, sbody))
            installed.append(str(target.relative_to(root)))
        elif t == "automation":
            for sub, dest, pattern in (("scripts", SCRIPT_DEST, "*.py"), ("tests", TEST_DEST, "test_*.py"), ("docs", DOC_DEST, "*.md")):
                src = dd / sub
                if src.is_dir():
                    for f in sorted(src.glob(pattern)):
                        target = root / dest / f.name
                        backup(target)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, target)
                        installed.append(str(target.relative_to(root)))
        elif t == "agent-instruction":
            for f in sorted((dd / "agents").glob("*.md")):
                target = root / ".opencode" / "agents" / f.name
                backup(target)
                shutil.copy2(f, target)
                installed.append(str(target.relative_to(root)))
            from . import agents as A

            A.sync_aliases(root)
            if shutil.which("opencode"):
                from . import lint as Lint

                errs = Lint.lint_agents(Lint.load_resolved(), strict_required=True)
                if errs:
                    raise RuntimeError("agent permission contract broken after promotion: " + "; ".join(errs[:3]))
        elif t == "knowledge":
            from .knowledge import add_inbox_note

            add_inbox_note(root, schemas.parse_sections(body).get("Proposed Change", "").strip(), f"{pid}")
            installed.append(".ateam/knowledge/inbox")
    except Exception as exc:  # noqa: BLE001 - restore everything, then report
        restore()
        shutil.rmtree(tmpdir, ignore_errors=True)
        die(f"promotion failed and was rolled back: {exc}")
    shutil.rmtree(tmpdir, ignore_errors=True)
    meta["status"] = "active"
    meta["promoted_at"] = now_iso()
    save_proposal(d, meta, body)
    for fid in meta.get("feedback") or []:
        try:
            F.set_status(root, fid, "addressed", pid)
        except SystemExit:
            pass
    F.rebuild_index(root)
    return {"id": pid, "status": "active", "installed": installed, "approved_by": meta.get("approved_by")}


def deprecate(root: Path, pid: str) -> dict:
    require_learning(root, "improve deprecate")
    d = prop_dir(root, pid)
    meta, body = load_proposal(d)
    if meta.get("status") != "active":
        die(f"{pid} is {meta.get('status')}; only active proposals can be deprecated")
    moved = []
    if meta["type"] == "skill":
        sd = next(iter((d / "drafts" / "skills").iterdir()))
        live = root / ".opencode" / "skills" / sd.name
        if live.exists():
            dst = root / ".ateam" / "deprecated-skills" / sd.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(live), str(dst))
            smeta, sbody = fm.parse_file(dst / "SKILL.md")
            smeta["metadata"] = {**(smeta.get("metadata") or {}), "ateam/lifecycle": "deprecated"}
            atomic_write(dst / "SKILL.md", fm.dump(smeta, sbody))
            moved.append(str(dst.relative_to(root)))
    meta["status"] = "deprecated"
    save_proposal(d, meta, body)
    return {"id": pid, "status": "deprecated", "moved": moved}


# ---------------------------------------------------------------------- commands
def cmd_new(args) -> int:
    root = find_root(args.root)
    require_learning(root, "improve new")
    fb = [x.strip() for x in (args.feedback or "").split(",") if x.strip()]
    pid, d = create_proposal(root, args.type, args.title, fb, args.risk, args.allow_overlap)
    emit({"id": pid, "dir": str(d.relative_to(root))}, args.json, f"{pid} {d.relative_to(root)}")
    return 0


def cmd_list(args) -> int:
    root = find_root(args.root)
    rows = []
    pd = proposals_dir(root)
    for p in sorted(pd.glob("PROP-*")) if pd.is_dir() else []:
        try:
            meta, body = load_proposal(p)
        except (fm.FrontmatterError, OSError):
            rows.append({"id": p.name, "status": "invalid"})
            continue
        if args.status and meta.get("status") != args.status:
            continue
        rows.append({"id": meta["id"], "type": meta.get("type"), "status": meta.get("status"), "risk": meta.get("risk"), "title": meta.get("title"), "approved_by": meta.get("approved_by"), "feedback": meta.get("feedback")})
    emit(rows, args.json, "\n".join(f"{r['id']}  {r.get('status'):<10} {r.get('type', ''):<18} {r.get('risk', ''):<5} {r.get('title', '')}" for r in rows) or "(no proposals)")
    return 0


def cmd_show(args) -> int:
    root = find_root(args.root)
    d = prop_dir(root, args.id)
    meta, body = load_proposal(d)
    risk, reasons = effective_risk(d, meta)
    data = {"meta": meta, "effective_risk": risk, "risk_reasons": reasons, "drafts": [str(p.relative_to(d)) for p in draft_files(d)], "validation": read_json(d / "validation.json", None)}
    emit(data, args.json, json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def cmd_search(args) -> int:
    root = find_root(args.root)
    hits = search_capabilities(root, " ".join(args.query))
    emit(hits, args.json, "\n".join(f"{h['kind']:<7} {h['id']:<28} score={h['score']} {h['path']}" for h in hits) or "(nothing similar installed)")
    return 0


def cmd_validate(args) -> int:
    root = find_root(args.root)
    res = validate_proposal(root, args.id)
    emit(res, args.json, ("OK" if res["ok"] else "FAILED\n" + "\n".join(f"  - {e}" for e in res["errors"])) + f"\nrisk: {res['risk']}")
    return 0 if res["ok"] else 1


def cmd_approve(args) -> int:
    root = find_root(args.root)
    approve(root, args.id, args.by)
    emit({"id": args.id, "approved_by": args.by}, args.json, f"{args.id} approved by {args.by}")
    return 0


def cmd_promote(args) -> int:
    root = find_root(args.root)
    res = promote(root, args.id)
    emit(res, args.json, f"{args.id} promoted: {', '.join(res['installed'])}")
    return 0


def cmd_reject(args) -> int:
    root = find_root(args.root)
    d = prop_dir(root, args.id)
    meta, body = load_proposal(d)
    if meta.get("status") == "active":
        die("an active proposal must be deprecated, not rejected")
    meta["status"] = "rejected"
    meta["reject_reason"] = args.reason or ""
    save_proposal(d, meta, body)
    emit({"id": args.id, "status": "rejected"}, args.json, f"{args.id} rejected")
    return 0


def cmd_deprecate(args) -> int:
    root = find_root(args.root)
    res = deprecate(root, args.id)
    emit(res, args.json, f"{args.id} deprecated")
    return 0


def register(sub) -> None:
    p = sub.add_parser("improve", help="improvement proposals: skills, scripts, docs (SkillSmith workflow)")
    ps = p.add_subparsers(dest="improve_cmd", required=True)

    def add(name, fn, help_):
        sp = ps.add_parser(name, help=help_)
        sp.add_argument("--json", action="store_true")
        sp.set_defaults(func=fn)
        return sp

    sp = add("new", cmd_new, "create PROP-NNN linked to feedback ids")
    sp.add_argument("--type", required=True, choices=list(schemas.PROPOSAL_TYPES))
    sp.add_argument("--title", required=True)
    sp.add_argument("--feedback", required=True, help="comma-separated FB ids")
    sp.add_argument("--risk", choices=["low", "high"])
    sp.add_argument("--allow-overlap", action="store_true")
    sp = add("list", cmd_list, "list proposals")
    sp.add_argument("--status")
    sp = add("show", cmd_show, "show a proposal with its effective risk")
    sp.add_argument("id")
    sp = add("search", cmd_search, "search installed skills and scripts before proposing something new")
    sp.add_argument("query", nargs="+")
    sp = add("validate", cmd_validate, "lint drafts, run draft tests, compute risk; proposed -> validated")
    sp.add_argument("id")
    sp = add("approve", cmd_approve, "record human approval (required for skills and high-risk changes)")
    sp.add_argument("id")
    sp.add_argument("--by", required=True)
    sp = add("promote", cmd_promote, "install a validated proposal under .opencode/ (enforces the approval policy)")
    sp.add_argument("id")
    sp = add("reject", cmd_reject, "reject a proposal")
    sp.add_argument("id")
    sp.add_argument("--reason")
    sp = add("deprecate", cmd_deprecate, "retire an active proposal (skills move out of .opencode/skills)")
    sp.add_argument("id")
