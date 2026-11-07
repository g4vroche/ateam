#!/usr/bin/env python3
"""Compare benchmark results: medians per variant. Usage: python3 bench/compare.py [bench/results/*.json]"""
import glob
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

files = sys.argv[1:] or sorted(glob.glob(str(Path(__file__).parent / "results" / "*.json")))
by = defaultdict(list)
for f in files:
    r = json.loads(Path(f).read_text())
    by[r["variant"]].append(r)


def tok(r, k):
    t = (r.get("stats") or {}).get("tokens") or {}
    return t.get(k, 0) if k != "cache_read" else (t.get("cache") or {}).get("read", 0)


def med(vals):
    return round(statistics.median(vals), 1) if vals else None


print(f"{'variant':10} {'n':>2} {'wall_s':>8} {'input':>9} {'output':>8} {'cache_rd':>10} {'steps':>6} {'hidden':>8} {'timeouts':>8}")
for v, rs in sorted(by.items()):
    pr = [r["hidden"]["passed"] / r["hidden"]["total"] for r in rs if r["hidden"].get("total")]
    print(
        f"{v:10} {len(rs):>2} {med([r['wall_s'] for r in rs]):>8} {med([tok(r, 'input') for r in rs]):>9} "
        f"{med([tok(r, 'output') for r in rs]):>8} {med([tok(r, 'cache_read') for r in rs]):>10} "
        f"{med([(r.get('stats') or {}).get('steps', 0) for r in rs]):>6} {med(pr) if pr else 'n/a':>8} {sum(r['timed_out'] for r in rs):>8}"
    )
