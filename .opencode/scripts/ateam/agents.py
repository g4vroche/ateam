"""Agent definition helpers: generate and verify the hidden legacy aliases."""
from __future__ import annotations

import json
import re

from .common import atomic_write, die, emit, find_root
from .lint import ALIASES


def alias_text(target_text: str, target: str) -> str:
    """Alias file = target file with `hidden: true` and a legacy description. Permissions stay identical by construction."""
    if not target_text.startswith("---\n"):
        raise ValueError("agent file has no frontmatter")
    end = target_text.index("\n---\n", 4)
    head, body = target_text[4:end].split("\n"), target_text[end + 5 :]
    out: list[str] = []
    seen_hidden = False
    for line in head:
        if line.startswith("description:"):
            desc = line[len("description:") :].strip().strip('"')
            out.append("description: " + json.dumps(f"Legacy alias of `{target}` (kept for one release). {desc}"))
        elif line.startswith("hidden:"):
            out.append("hidden: true")
            seen_hidden = True
        else:
            out.append(line)
    if not seen_hidden:
        idx = next((i for i, ln in enumerate(out) if ln.startswith("mode:")), len(out) - 1)
        out.insert(idx + 1, "hidden: true")
    return "---\n" + "\n".join(out) + "\n---\n" + body


def sync_aliases(root, check: bool = False) -> list[str]:
    adir = root / ".opencode" / "agents"
    problems: list[str] = []
    for alias, target in ALIASES.items():
        tp = adir / f"{target}.md"
        if not tp.exists():
            continue
        want = alias_text(tp.read_text(encoding="utf-8"), target)
        ap = adir / f"{alias}.md"
        have = ap.read_text(encoding="utf-8") if ap.exists() else None
        if have != want:
            problems.append(f"{alias}.md is out of date with {target}.md")
            if not check:
                atomic_write(ap, want)
    return problems


def cmd_sync_aliases(args) -> int:
    root = find_root(args.root)
    problems = sync_aliases(root, check=args.check)
    emit({"stale": problems}, args.json, "\n".join(problems) or "aliases up to date")
    return 1 if (problems and args.check) else 0


def register(sub) -> None:
    p = sub.add_parser("agents", help="agent definition helpers")
    ps = p.add_subparsers(dest="agents_cmd", required=True)
    sp = ps.add_parser("sync-aliases", help="regenerate hidden legacy aliases (architecte, dev, testeur) from their targets")
    sp.add_argument("--check", action="store_true", help="exit 1 when an alias is stale, write nothing")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_sync_aliases)
