"""`ateam init` and `ateam upgrade`: install or update the boilerplate in a project.

Installed files are tracked in `.ateam/installed.json` (path -> sha256 at install time). An upgrade only overwrites
files the user has not modified; a modified file that also changed upstream is left alone and the new version is
written next to it as `<file>.new`. Project data under `.ateam/` (work, feedback, knowledge, proposals) is never touched.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .common import OPENCODE_DIR, atomic_write, die, emit, find_root, read_json, sha256_file, write_json

IGNORE = ("__pycache__", ".pytest_cache", ".pytest-lib")
GITIGNORE_MARK = "# ateam runtime data (managed by `ateam init`)"
GITIGNORE_BLOCK = f"""{GITIGNORE_MARK}
.ateam/work/*/state/
.ateam/feedback/
.ateam/knowledge/inbox/
.ateam/knowledge/drafts/
.ateam/knowledge/.checkpoint.json
.ateam/knowledge/.pending.json
.ateam/knowledge/.lock
.ateam/knowledge/.dream.lock
.ateam/knowledge/dream.log.jsonl
.ateam/runs/
.ateam/installed.json
__pycache__/
*.pyc
"""


def source_version(src: Path | None = None) -> str:
    p = (src or OPENCODE_DIR) / "VERSION"
    return p.read_text().strip() if p.exists() else "0"


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)


def source_files(src: Path) -> dict[str, Path]:
    out = {}
    for p in sorted(src.rglob("*")):
        if p.is_file() and not any(part in IGNORE for part in p.relative_to(src).parts) and not p.name.endswith(".pyc"):
            out[str(p.relative_to(src))] = p
    return out


def sync_files(src: Path, dst: Path, recorded: dict, force: bool, dry: bool) -> dict:
    res = {"added": [], "updated": [], "unchanged": [], "conflicts": [], "removed": [], "kept_orphans": [], "kept_local": []}
    new_record = dict(recorded)
    files = source_files(src)
    for rel, sp in files.items():
        tp = dst / rel
        sh = sha256_file(sp)
        if not tp.exists():
            res["added"].append(rel)
        else:
            th = sha256_file(tp)
            if th == sh:
                res["unchanged"].append(rel)
                new_record[rel] = sh
                continue
            if force or recorded.get(rel) == th:
                res["updated"].append(rel)
            elif recorded.get(rel) == sh:
                res["kept_local"].append(rel)  # upstream did not change; your edit stays
                continue
            else:
                res["conflicts"].append(rel)
                if not dry:
                    shutil.copy2(sp, tp.with_name(tp.name + ".new"))
                continue
        if not dry:
            tp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sp, tp)
        new_record[rel] = sh
    for rel in sorted(set(recorded) - set(files)):
        tp = dst / rel
        if not tp.exists():
            new_record.pop(rel, None)
        elif sha256_file(tp) == recorded[rel]:
            res["removed"].append(rel)
            if not dry:
                tp.unlink()
            new_record.pop(rel, None)
        else:
            res["kept_orphans"].append(rel)
    res["_record"] = new_record
    return res


def skeleton(target: Path, dry: bool = False) -> list[str]:
    made = []
    a = target / ".ateam"
    for d in ("work",):
        if not (a / d).is_dir():
            made.append(f".ateam/{d}/")
            if not dry:
                (a / d).mkdir(parents=True, exist_ok=True)
    if not (a / "config.json").exists():
        made.append(".ateam/config.json")
        if not dry:
            write_json(a / "config.json", {"version": 1})
    if not (a / "VERSION").exists():
        made.append(".ateam/VERSION")
        if not dry:
            atomic_write(a / "VERSION", source_version() + "\n")
    gi = target / ".gitignore"
    text = gi.read_text() if gi.exists() else ""
    if GITIGNORE_MARK not in text:
        made.append(".gitignore (ateam block)")
        if not dry:
            atomic_write(gi, (text.rstrip("\n") + "\n\n" if text.strip() else "") + GITIGNORE_BLOCK)
    return made


def migration_notes(installed: str, current: str) -> str:
    path = OPENCODE_DIR / "MIGRATIONS.md"
    if not path.exists():
        return ""
    chunks = re.split(r"(?m)^## ", path.read_text())[1:]
    keep = []
    for c in chunks:
        m = re.match(r"(\d+(?:\.\d+)*)", c)
        if m and _vtuple(installed) < _vtuple(m.group(1)) <= _vtuple(current):
            keep.append("## " + c.strip())
    return "\n\n".join(keep)


def install(target: Path, upgrade: bool, force: bool = False, dry: bool = False) -> dict:
    dst = target / ".opencode"
    a = target / ".ateam"
    if upgrade and not dst.is_dir():
        die(f"{target} has no .opencode/; use `ateam init --target {target}` first")
    if not upgrade and dst.exists() and not force:
        die(f"{dst} already exists: use `ateam upgrade --target {target}` (or `init --force` to overwrite)")
    recorded = (read_json(a / "installed.json", {}) or {}).get("files", {})
    installed_v = (a / "VERSION").read_text().strip() if (a / "VERSION").exists() else "0"
    res = sync_files(OPENCODE_DIR, dst, recorded, force, dry)
    record = res.pop("_record")
    res["skeleton"] = skeleton(target, dry)
    cur = source_version()
    res["from_version"], res["to_version"] = installed_v, cur
    res["notes"] = migration_notes(installed_v, cur) if upgrade else ""
    if not dry:
        write_json(a / "installed.json", {"version": cur, "files": record})
        atomic_write(a / "VERSION", cur + "\n")
    return res


def _summary(res: dict, dry: bool) -> str:
    lines = [f"{'(dry-run) ' if dry else ''}{res['from_version']} -> {res['to_version']}"]
    for k in ("added", "updated", "removed", "conflicts", "kept_local", "kept_orphans"):
        if res[k]:
            lines.append(f"{k}: {len(res[k])}" + (" " + ", ".join(res[k][:6]) + (" ..." if len(res[k]) > 6 else "")))
    if res["conflicts"]:
        lines.append("conflicts: you modified these files; the new upstream version was written next to each as <file>.new")
    if res["skeleton"]:
        lines.append("created: " + ", ".join(res["skeleton"]))
    if res.get("notes"):
        lines += ["", res["notes"]]
    return "\n".join(lines)


def _target(args) -> Path:
    return Path(args.target).resolve() if args.target else find_root(args.root)


def cmd_init(args) -> int:
    if not args.target:
        res = {"skeleton": skeleton(Path(args.root or ".").resolve()), "from_version": "-", "to_version": source_version(), "notes": "", "added": [], "updated": [], "removed": [], "conflicts": [], "kept_orphans": [], "kept_local": []}
        emit(res, args.json, "created: " + (", ".join(res["skeleton"]) or "nothing (already initialised)"))
        return 0
    target = _target(args)
    target.mkdir(parents=True, exist_ok=True)
    res = install(target, upgrade=False, force=args.force, dry=args.dry_run)
    emit(res, args.json, _summary(res, args.dry_run))
    return 0


def cmd_upgrade(args) -> int:
    target = _target(args)
    res = install(target, upgrade=True, force=args.force, dry=args.dry_run)
    emit(res, args.json, _summary(res, args.dry_run))
    return 20 if res["conflicts"] else 0


def register(sub) -> None:
    p = sub.add_parser("init", help="create the .ateam skeleton here, or install the boilerplate into --target")
    p.add_argument("--target", help="project directory to install into (copies .opencode/)")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_init)
    u = sub.add_parser("upgrade", help="update an installed boilerplate without overwriting your edits")
    u.add_argument("--target", help="project directory (default: the current project)")
    u.add_argument("--force", action="store_true", help="overwrite files even when you modified them")
    u.add_argument("--dry-run", action="store_true")
    u.add_argument("--json", action="store_true")
    u.set_defaults(func=cmd_upgrade)
