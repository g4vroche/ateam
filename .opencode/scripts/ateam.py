#!/usr/bin/env python3
"""ateam: deterministic tooling for the OpenCode multi-agent boilerplate.

Usage: python3 .opencode/scripts/ateam.py <area> <command> [options]
Areas: work, quality, run, feedback, knowledge, improve, lint, gc, init
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

AREAS = ["work", "quality", "run", "feedback", "knowledge", "improve", "lint", "gc", "init_cmd"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ateam", description=__doc__.split("\n")[0])
    parser.add_argument("--root", help="project root (default: nearest dir with .ateam or .opencode, or $ATEAM_ROOT)")
    sub = parser.add_subparsers(dest="area", required=True)
    for name in AREAS:
        try:
            mod = importlib.import_module(f"ateam.{name}")
        except ModuleNotFoundError as exc:
            if exc.name == f"ateam.{name}":
                continue
            raise
        mod.register(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    # allow --root anywhere: also accept it on subcommands via a pre-scan
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
