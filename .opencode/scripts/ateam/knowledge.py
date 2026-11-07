"""Knowledge store. Phase 5 provides capture only: `knowledge note` appends to the inbox."""
from __future__ import annotations

import re
from pathlib import Path

from .common import ateam_dir, die, emit, file_lock, find_root, now_iso, today
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


def cmd_note(args) -> int:
    root = find_root(args.root)
    p = add_inbox_note(root, args.text, args.source)
    emit({"inbox": str(p.relative_to(root))}, args.json, f"noted in {p.relative_to(root)}")
    return 0


def register(sub) -> None:
    p = sub.add_parser("knowledge", help="project knowledge store")
    ks = p.add_subparsers(dest="k_cmd", required=True)
    n = ks.add_parser("note", help="append a durable fact to the inbox (capture only)")
    n.add_argument("text")
    n.add_argument("--source", required=True)
    n.add_argument("--json", action="store_true")
    n.set_defaults(func=cmd_note)
