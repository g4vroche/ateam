"""Agent definition helpers."""
from __future__ import annotations

from .common import emit, find_root


def sync_aliases(root, check: bool = False) -> list[str]:
    """No aliases remain; always up to date."""
    return []


def cmd_sync_aliases(args) -> int:
    root = find_root(args.root)
    problems = sync_aliases(root, check=args.check)
    emit({"stale": problems}, args.json, "\n".join(problems) or "aliases up to date")
    return 1 if (problems and args.check) else 0


def register(sub) -> None:
    p = sub.add_parser("agents", help="agent definition helpers")
    ps = p.add_subparsers(dest="agents_cmd", required=True)
    sp = ps.add_parser("sync-aliases", help="verify that no agent aliases are stale (all removed)")
    sp.add_argument("--check", action="store_true", help="exit 1 when an alias is stale")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_sync_aliases)
