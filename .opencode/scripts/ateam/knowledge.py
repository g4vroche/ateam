"""Knowledge store (Dreamer pipeline).

Layout under `.ateam/knowledge/`:
  inbox/YYYY-MM-DD.md          raw durable-fact notes (capture only, git-ignored)
  drafts/*.md                  what the Dreamer writes; `upsert` validates and files them (the Dreamer cannot write elsewhere)
  <category>/KNOW-nnnn-<key>.md  the store; `INDEX.md` is generated
  .pending.json                sources found by `collect`, waiting for a successful dream
  .checkpoint.json             content-hash ledger of sources already dreamed (written only by `checkpoint`)

Invariants enforced here, not in the prompt: provenance is mandatory, secrets and conversation dumps are rejected,
`verified` needs a completed task report or a user confirmation, contradictions are flagged and never overwritten,
and an unchanged input set produces no write and no model call.
"""
from __future__ import annotations

import json
import re
import shutil
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
    learning_enabled,
    now_iso,
    read_json,
    sha256_text,
    slugify,
    today,
    write_json,
)
from .feedback import has_secret

MAX_NOTE_CHARS = 1500


def inbox_dir(root: Path) -> Path:
    return ateam_dir(root) / "knowledge" / "inbox"


def add_inbox_note(root: Path, text: str, source: str) -> Path:
    text = (text or "").strip()
    source = (source or "").strip()
    if not text:
        die("rejected: empty note")
    if not source:
        die("rejected: --source is required (task report, run id, file or user)")
    if len(text) > MAX_NOTE_CHARS:
        die(f"rejected: note longer than {MAX_NOTE_CHARS} characters; write one durable fact, not a conversation")
    if text.count("\n") > 12:
        die("rejected: looks like a conversation dump (too many lines)")
    hit = has_secret(text + " " + source)
    if hit:
        die(f"rejected: the note looks like it contains a secret ({hit}...)")
    d = inbox_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{today()}.md"
    with file_lock(d / ".lock"):
        with open(path, "a", encoding="utf-8") as fh:
            body = re.sub(r"\s*\n\s*", " ", text)
            fh.write(f"- [{now_iso()}] (source: {source}) {body}\n")
    return path


CATEGORIES = schemas.KNOWLEDGE_CATEGORIES
MAX_BODY_CHARS = 4000
MAX_BODY_LINES = 60
KNOW_FILE_RE = re.compile(r"KNOW-(\d{4,})-.*\.md$")
CONVERSATION_RE = re.compile(r"(?im)^\s*(user|assistant|human|ai|you|me)\s*[:>]")
SOURCE_RE = re.compile(r"^(report|feedback|run|inbox|task|user):(.+)$")


def kdir(root: Path) -> Path:
    return ateam_dir(root) / "knowledge"


def pending_path(root: Path) -> Path:
    return kdir(root) / ".pending.json"


def checkpoint_path(root: Path) -> Path:
    return kdir(root) / ".checkpoint.json"


# ------------------------------------------------------------------- the store
def entry_files(root: Path) -> list[Path]:
    out = []
    for c in CATEGORIES:
        d = kdir(root) / c
        if d.is_dir():
            out.extend(sorted(p for p in d.glob("KNOW-*.md") if KNOW_FILE_RE.match(p.name)))
    return out


def load_entries(root: Path) -> tuple[list[dict], list[str]]:
    entries, errs = [], []
    for p in entry_files(root):
        try:
            meta, body = fm.parse_file(p)
        except (fm.FrontmatterError, OSError) as exc:
            errs.append(f"{p.relative_to(root)}: {exc}")
            continue
        e = schemas.validate_knowledge(meta, body)
        if e:
            errs.extend(f"{p.relative_to(root)}: {x}" for x in e)
            continue
        entries.append({"meta": meta, "body": body, "path": p})
    return entries, errs


def _next_id(root: Path) -> str:
    n = 0
    for p in entry_files(root):
        m = KNOW_FILE_RE.match(p.name)
        if m:
            n = max(n, int(m.group(1)))
    return f"KNOW-{n + 1:04d}"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def source_exists(root: Path, src: str) -> bool:
    m = SOURCE_RE.match(src)
    if not m:
        return False
    kind, ref = m.groups()
    if kind == "user":
        return bool(ref.strip())
    path = (root / ref.split("#", 1)[0].strip()).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    return path.exists()


def source_verifies(root: Path, src: str, user_confirmed: bool) -> bool:
    """A source can justify `verified` only if it is the report of a task that passed its gate, or a user confirmation."""
    m = SOURCE_RE.match(src)
    if not m:
        return False
    kind, ref = m.groups()
    if kind == "user":
        return user_confirmed
    if kind != "report":
        return False
    rp = root / ref.split("#", 1)[0].strip()
    if rp.parent.name != "reports" or not rp.exists():
        return False
    task_file = rp.parent.parent / "tasks" / rp.name
    try:
        meta, _ = fm.parse_file(task_file)
    except (fm.FrontmatterError, OSError):
        return False
    return meta.get("status") == "completed"


def reject_text(text: str) -> str | None:
    hit = has_secret(text)
    if hit:
        return f"looks like it contains a secret ({hit}...)"
    if CONVERSATION_RE.findall(text) and len(CONVERSATION_RE.findall(text)) >= 3:
        return "looks like a conversation dump (several speaker-prefixed lines)"
    if len(text) > MAX_BODY_CHARS or text.count("\n") > MAX_BODY_LINES:
        return f"too long (cap {MAX_BODY_CHARS} chars / {MAX_BODY_LINES} lines): write one durable fact, not a transcript"
    return None


def upsert(root: Path, draft: Path, user_confirmed: bool = False, supersede: bool = False) -> dict:
    drafts = (kdir(root) / "drafts").resolve()
    draft = Path(draft)
    if not draft.is_absolute():
        draft = Path.cwd() / draft
    try:
        draft.resolve().relative_to(drafts)
    except ValueError:
        die(f"rejected: drafts must live under {drafts.relative_to(root.resolve())}/")
    try:
        meta, body = fm.parse_file(draft)
    except (fm.FrontmatterError, OSError) as exc:
        die(f"rejected: {exc}")
    meta = dict(meta)
    category = meta.get("category")
    key = str(meta.get("key", ""))
    title = str(meta.get("title", "")).strip()
    sources = meta.get("sources") if isinstance(meta.get("sources"), list) else []
    draft_meta = {**meta, "id": "KNOW-0000", "created_at": today(), "updated_at": today(), "confidence": meta.get("confidence") if meta.get("confidence") in schemas.CONFIDENCE else "hypothesis"}
    errs = schemas.validate_knowledge(draft_meta, body)
    if not sources:
        errs.append("provenance is mandatory: list sources as kind:path (report|feedback|run|inbox|task) or user:<who>")
    for s in sources:
        if not source_exists(root, str(s)):
            errs.append(f"source {s!r} is not kind:path with an existing file under the project (or user:<who>)")
        elif str(s).startswith("user:") and not user_confirmed:
            errs.append(f"source {s!r}: user confirmation can only be recorded by a human (`--confirmed-by-user`)")
    bad = reject_text(title + "\n" + body)
    if bad:
        errs.append(bad)
    if errs:
        die("rejected: " + "; ".join(errs))
    sources = [str(s) for s in sources]
    verified_ok = any(source_verifies(root, s, user_confirmed) for s in sources)
    declared = draft_meta["confidence"]
    confidence = "verified" if declared == "verified" and verified_ok else "hypothesis"
    downgraded = declared == "verified" and confidence != "verified"
    kd = kdir(root)
    kd.mkdir(parents=True, exist_ok=True)
    with file_lock(kd / ".lock"):
        entries, _ = load_entries(root)
        same = [e for e in entries if e["meta"].get("key") == key and e["meta"].get("status", "active") in ("active", "contradicted")]
        result: dict
        if same:
            old = same[0]
            om = old["meta"]
            old_sources = list(om.get("sources") or [])
            new_sources = [s for s in sources if s not in old_sources]
            same_body = _norm(old["body"]) == _norm(body)
            if same_body or not new_sources:
                # same content, or a rewording of what the same sources already said: never churn the store
                merged = old_sources + new_sources
                upgrade = om.get("confidence") != "verified" and confidence == "verified" and same_body
                if not new_sources and not upgrade:
                    result = {"result": "unchanged", "id": om["id"], "path": str(old["path"].relative_to(root))}
                else:
                    om = dict(om, sources=merged, updated_at=today(), confidence="verified" if upgrade else om.get("confidence"))
                    atomic_write(old["path"], fm.dump(om, old["body"]))
                    result = {"result": "merged", "id": om["id"], "path": str(old["path"].relative_to(root))}
            elif supersede:
                nid = _next_id(root)
                om = dict(om, status="deprecated", superseded_by=nid, updated_at=today())
                atomic_write(old["path"], fm.dump(om, old["body"]))
                result = _write_new(root, nid, category, key, title, confidence, sources, body, {"supersedes": om["id"]})
                result["result"] = "superseded"
                result["replaces"] = om["id"]
            else:
                nid = _next_id(root)
                om = dict(om, status="contradicted", contradicted_by=sorted(set(list(om.get("contradicted_by") or []) + [nid])), updated_at=today())
                atomic_write(old["path"], fm.dump(om, old["body"]))
                result = _write_new(root, nid, category, key, title, confidence, sources, body, {"status": "contradicted", "contradicts": [om["id"]]})
                result["result"] = "contradiction"
                result["conflicts_with"] = om["id"]
        else:
            result = _write_new(root, _next_id(root), category, key, title, confidence, sources, body, {})
            result["result"] = "created"
    if downgraded:
        result["confidence_downgraded"] = "no source is a completed-task report or a user confirmation"
    draft.unlink(missing_ok=True)
    return result


def _write_new(root, nid, category, key, title, confidence, sources, body, extra) -> dict:
    meta = {
        "id": nid,
        "title": title,
        "key": key,
        "category": category,
        "created_at": today(),
        "updated_at": today(),
        "confidence": confidence,
        "status": "active",
        "sources": sources,
    }
    meta.update(extra)
    d = kdir(root) / category
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{nid}-{slugify(key, 40)}.md"
    atomic_write(path, fm.dump(meta, body.strip() + "\n"))
    return {"id": nid, "path": str(path.relative_to(root))}


def resolve(root: Path, keep: str) -> dict:
    entries, _ = load_entries(root)
    by_id = {e["meta"]["id"]: e for e in entries}
    if keep not in by_id:
        die(f"{keep} not found")
    k = by_id[keep]
    rivals = set(k["meta"].get("contradicts") or []) | set(k["meta"].get("contradicted_by") or [])
    rivals |= {i for i, e in by_id.items() if keep in (e["meta"].get("contradicts") or []) or keep in (e["meta"].get("contradicted_by") or [])}
    if not rivals:
        die(f"{keep} has no recorded contradiction")
    for rid in sorted(rivals):
        e = by_id.get(rid)
        if e:
            m = dict(e["meta"], status="deprecated", updated_at=today(), superseded_by=keep)
            atomic_write(e["path"], fm.dump(m, e["body"]))
    m = {x: v for x, v in k["meta"].items() if x not in ("contradicts", "contradicted_by")}
    m["status"] = "active"
    m["updated_at"] = today()
    atomic_write(k["path"], fm.dump(m, k["body"]))
    return {"kept": keep, "deprecated": sorted(rivals)}


def render_index(root: Path) -> str:
    entries, _ = load_entries(root)
    lines = ["# Knowledge index", "", "Generated by `ateam.py knowledge index`. Do not edit.", ""]
    review = [e for e in entries if e["meta"].get("status") == "contradicted"]
    if review:
        lines += ["## Needs review (contradictions)", ""]
        for e in sorted(review, key=lambda e: e["meta"]["id"]):
            m = e["meta"]
            lines.append(f"- {m['id']} [{m['category']}] {m['title']} (`{e['path'].relative_to(kdir(root))}`) conflicts with {', '.join((m.get('contradicts') or []) + (m.get('contradicted_by') or []))}")
        lines.append("")
    for c in CATEGORIES:
        rows = [e for e in entries if e["meta"]["category"] == c and e["meta"].get("status", "active") == "active"]
        if not rows:
            continue
        lines += [f"## {c}", ""]
        for e in sorted(rows, key=lambda e: e["meta"]["id"]):
            m = e["meta"]
            lines.append(f"- {m['id']} {m['title']} [{m['confidence']}] (`{e['path'].relative_to(kdir(root))}`)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_index(root: Path) -> bool:
    path = kdir(root) / "INDEX.md"
    text = render_index(root)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    kdir(root).mkdir(parents=True, exist_ok=True)
    atomic_write(path, text)
    return True


def search(root: Path, query: str, category: str | None = None, include_contradicted: bool = False, limit: int = 5) -> list[dict]:
    tokens = set(re.findall(r"[a-z0-9]{3,}", query.lower()))
    entries, _ = load_entries(root)
    hits = []
    for e in entries:
        m = e["meta"]
        status = m.get("status", "active")
        if status == "deprecated" or (status == "contradicted" and not include_contradicted):
            continue
        if category and m["category"] != category:
            continue
        head = set(re.findall(r"[a-z0-9]{3,}", (m["title"] + " " + m["key"].replace("-", " ").replace(".", " ")).lower()))
        body = set(re.findall(r"[a-z0-9]{3,}", e["body"].lower()))
        score = 3 * len(tokens & head) + len(tokens & body)
        if score:
            hits.append({"id": m["id"], "title": m["title"], "path": str(e["path"].relative_to(root)), "category": m["category"], "confidence": m["confidence"], "status": status, "score": score})
    hits.sort(key=lambda h: (-h["score"], h["id"]))
    return hits[:limit]


def resolve_paths(root: Path, ids: list[str]) -> tuple[list[str], list[str]]:
    """KNOW ids -> readable paths. Missing, deprecated or contradicted ids are returned separately."""
    entries, _ = load_entries(root)
    by_id = {e["meta"]["id"]: e for e in entries}
    ok, bad = [], []
    for i in ids:
        e = by_id.get(i)
        if e and e["meta"].get("status", "active") == "active":
            ok.append(str(e["path"].relative_to(root)))
        else:
            bad.append(i)
    return ok, bad


def lint_store(root: Path) -> list[str]:
    entries, errs = load_entries(root)
    seen_id: dict[str, Path] = {}
    seen_key: dict[str, str] = {}
    for e in entries:
        m = e["meta"]
        if m["id"] in seen_id:
            errs.append(f"{m['id']} is used by two files")
        seen_id[m["id"]] = e["path"]
        if not e["path"].name.startswith(m["id"]):
            errs.append(f"{e['path'].relative_to(root)}: file name does not start with {m['id']}")
        if m.get("status", "active") == "active":
            if m["key"] in seen_key:
                errs.append(f"key {m['key']} is active in {seen_key[m['key']]} and {m['id']}")
            seen_key[m["key"]] = m["id"]
        bad = reject_text(e["body"])
        if bad:
            errs.append(f"{m['id']}: {bad}")
        for s in m.get("sources") or []:
            if not SOURCE_RE.match(str(s)):
                errs.append(f"{m['id']}: source {s!r} is not kind:path")
        if m.get("confidence") == "verified" and not any(source_verifies(root, str(s), True) for s in m.get("sources") or []):
            errs.append(f"{m['id']}: verified without a completed-task report or user confirmation")
    return errs


# --------------------------------------------------------- collect / checkpoint
def source_files(root: Path) -> list[tuple[str, Path]]:
    a = ateam_dir(root)
    out: list[tuple[str, Path]] = []
    for p in sorted((a / "runs").glob("RUN-*/summary.json")) if (a / "runs").is_dir() else []:
        out.append(("run", p))
    for p in sorted((a / "work").glob("*/reports/*.md")) if (a / "work").is_dir() else []:
        out.append(("report", p))
    for p in sorted((a / "feedback" / "events").glob("*/*.md")) if (a / "feedback" / "events").is_dir() else []:
        out.append(("feedback", p))
    for p in sorted((a / "knowledge" / "inbox").glob("*.md")) if (a / "knowledge" / "inbox").is_dir() else []:
        out.append(("inbox", p))
    return out


def collect(root: Path, since: str | None = None) -> dict:
    ledger = (read_json(checkpoint_path(root), {}) or {}).get("sources", {})
    new = []
    for kind, p in source_files(root):
        rel = str(p.relative_to(root))
        try:
            digest = sha256_text(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if ledger.get(rel) == digest:
            continue
        if since:
            from datetime import datetime

            if datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d") < since:
                continue
        new.append({"kind": kind, "path": rel, "sha": digest})
    pp = pending_path(root)
    if new:
        write_json(pp, {"collected_at": now_iso(), "sources": new})
    elif pp.exists():
        pp.unlink()
    return {"new": len(new), "sources": new, "pending": str(pp.relative_to(root)) if new else None}


def checkpoint(root: Path) -> dict:
    pp = pending_path(root)
    pending = read_json(pp, None)
    if not pending:
        die("nothing pending: run `knowledge collect` first")
    with file_lock(kdir(root) / ".lock"):
        cp = read_json(checkpoint_path(root), {}) or {}
        sources = dict(cp.get("sources", {}))
        for s in pending["sources"]:
            sources[s["path"]] = s["sha"]
        write_json(checkpoint_path(root), {"checkpoint_at": now_iso(), "sources": sources})
        pp.unlink()
    return {"checkpointed": len(pending["sources"])}


# ------------------------------------------------------------------------ dream
def dream(root: Path, run_agent=None) -> dict:
    """collect -> (no model call when nothing is new) -> Dreamer -> validate store -> index -> checkpoint."""
    from . import opencode as OC
    from .common import load_config

    c = collect(root)
    if c["new"] == 0:
        return {"status": "nothing-new", "model_called": False}
    drafts = kdir(root) / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    cfg = load_config(root)
    prompt = (
        f"Dream over the {c['new']} new sources listed in .ateam/knowledge/.pending.json. "
        "Write drafts to .ateam/knowledge/drafts/ and file each with `knowledge upsert`. Then run `knowledge index`. Stop when done."
    )
    res = (run_agent or OC.run_role)(root, cfg["opencode_bin"], "dreamer", prompt, cfg["role_timeout_s"], kdir(root) / "dream.log.jsonl", cfg.get("standalone", True), env_extra={"ATEAM_AGENT": "dreamer"})
    if not res.ok:
        return {"status": "failed", "model_called": True, "reason": "dreamer did not finish cleanly; nothing was checkpointed (sources will be collected again)"}
    errs = lint_store(root)
    leftover = sorted(p.name for p in drafts.glob("*.md"))
    if errs:
        return {"status": "failed", "model_called": True, "reason": "store lint failed: " + "; ".join(errs[:3])}
    write_index(root)
    cp = checkpoint(root)
    return {"status": "dreamed", "model_called": True, "undrafted": leftover, **cp}


# --------------------------------------------------------------------------- CLI
def cmd_note(args) -> int:
    root = find_root(args.root)
    p = add_inbox_note(root, args.text, args.source)
    emit({"inbox": str(p.relative_to(root))}, args.json, f"noted in {p.relative_to(root)}")
    return 0


def cmd_collect(args) -> int:
    root = find_root(args.root)
    res = collect(root, args.since)
    emit(res, args.json, f"{res['new']} new source(s)" + (f", pending in {res['pending']}" if res["pending"] else ""))
    return 20 if args.require_new and res["new"] == 0 else 0


def cmd_upsert(args) -> int:
    root = find_root(args.root)
    res = upsert(root, Path(args.file), args.confirmed_by_user, args.supersede)
    emit(res, args.json, f"{res['result']}: {res.get('id')} {res.get('path', '')}")
    return 0


def cmd_index(args) -> int:
    root = find_root(args.root)
    changed = write_index(root)
    emit({"changed": changed}, args.json, "INDEX.md updated" if changed else "INDEX.md already up to date")
    return 0


def cmd_checkpoint(args) -> int:
    root = find_root(args.root)
    res = checkpoint(root)
    emit(res, args.json, f"checkpointed {res['checkpointed']} source(s)")
    return 0


def cmd_search(args) -> int:
    root = find_root(args.root)
    hits = search(root, " ".join(args.query), args.category, args.include_contradicted)
    emit(hits, args.json, "\n".join(f"{h['id']}  {h['title']}  ({h['path']})" for h in hits) or "(no matching knowledge)")
    return 0


def cmd_resolve(args) -> int:
    root = find_root(args.root)
    res = resolve(root, args.keep)
    emit(res, args.json, f"kept {res['kept']}, deprecated {', '.join(res['deprecated'])}")
    return 0


def cmd_lint(args) -> int:
    root = find_root(args.root)
    errs = lint_store(root)
    emit({"ok": not errs, "errors": errs}, args.json, "\n".join(errs) + ("\n" if errs else "") + ("OK" if not errs else f"FAILED: {len(errs)} error(s)"))
    return 0 if not errs else 1


def cmd_dream(args) -> int:
    root = find_root(args.root)
    if not learning_enabled(root):
        die("knowledge dream is locked: gate G1 has not passed (see `ateam.py g1`)", 3)
    res = dream(root)
    emit(res, args.json, f"{res['status']}" + (f": {res.get('reason')}" if res.get("reason") else ""))
    return 0 if res["status"] in ("nothing-new", "dreamed") else 12


def register(sub) -> None:
    p = sub.add_parser("knowledge", help="project knowledge store")
    ks = p.add_subparsers(dest="k_cmd", required=True)

    def add(name, fn, help_):
        sp = ks.add_parser(name, help=help_)
        sp.add_argument("--json", action="store_true")
        sp.set_defaults(func=fn)
        return sp

    n = add("note", cmd_note, "append a durable fact to the inbox (capture only)")
    n.add_argument("text")
    n.add_argument("--source", required=True)
    c = add("collect", cmd_collect, "list sources changed since the last checkpoint (writes .pending.json)")
    c.add_argument("--since", help="YYYY-MM-DD: ignore sources last modified before")
    c.add_argument("--require-new", action="store_true", help="exit 20 when nothing is new (no model call needed)")
    u = add("upsert", cmd_upsert, "validate a draft and file it (dedupe by key, flag contradictions)")
    u.add_argument("file")
    u.add_argument("--supersede", action="store_true", help="the draft replaces the stored entry for the same key (needs a new source)")
    u.add_argument("--confirmed-by-user", action="store_true", help="human only: the fact was confirmed by the user")
    add("index", cmd_index, "regenerate INDEX.md")
    add("checkpoint", cmd_checkpoint, "record collected sources as dreamed (only after a successful dream)")
    s = add("search", cmd_search, "find up to 5 entries by keywords")
    s.add_argument("query", nargs="+")
    s.add_argument("--category", choices=list(CATEGORIES))
    s.add_argument("--include-contradicted", action="store_true")
    r = add("resolve", cmd_resolve, "human: keep one side of a contradiction")
    r.add_argument("--keep", required=True)
    add("lint", cmd_lint, "validate every entry and the store invariants")
    add("dream", cmd_dream, "manual dream: collect, run the dreamer if anything is new, index, checkpoint")
