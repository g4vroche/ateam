#!/usr/bin/env python3
"""Compare benchmark results (medians per variant) and print deltas.

  python3 bench/compare.py [--label LABEL] [--task todo-cli] [--runs] [files...]

`--label` keeps only `current` results with that label (baseline results are not code-dependent and always count).
Invalid runs (leaked into the repository, or no hidden-check result) are listed and excluded.
"""
import argparse
import glob
import json
import statistics
from collections import defaultdict
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("files", nargs="*")
ap.add_argument("--label")
ap.add_argument("--task", default="todo-cli")
ap.add_argument("--runs", action="store_true", help="print every run")
ap.add_argument("--json", action="store_true")
args = ap.parse_args()

files = args.files or sorted(glob.glob(str(Path(__file__).parent / "results" / "*.json")))
by = defaultdict(list)
excluded = []
for f in files:
    r = json.loads(Path(f).read_text())
    if r.get("task", args.task) != args.task:
        continue
    if r["variant"] == "current" and args.label and r.get("label") != args.label:
        continue
    if r.get("leaked_into_repo") or not (r.get("hidden") or {}).get("total"):
        excluded.append((Path(f).name, "leaked into repo" if r.get("leaked_into_repo") else "no hidden result"))
        continue
    r["_file"] = Path(f).name
    by[r["variant"]].append(r)


def tok(r, k):
    t = (r.get("stats") or {}).get("tokens") or {}
    return t.get(k, 0) if k != "cache_read" else (t.get("cache") or {}).get("read", 0)


def med(vals):
    return round(statistics.median(vals), 1) if vals else None


def pass_rate(r):
    return r["hidden"]["passed"] / r["hidden"]["total"]


def summary(rs):
    return {
        "n": len(rs),
        "wall_s": med([r["wall_s"] for r in rs]),
        "input": med([tok(r, "input") for r in rs]),
        "output": med([tok(r, "output") for r in rs]),
        "cache_read": med([tok(r, "cache_read") for r in rs]),
        "steps": med([(r.get("stats") or {}).get("steps", 0) for r in rs]),
        "pass_rate": round(statistics.mean(pass_rate(r) for r in rs), 3) if rs else None,
        "timeouts": sum(1 for r in rs if r["timed_out"]),
    }


sums = {v: summary(rs) for v, rs in sorted(by.items())}
if args.json:
    print(json.dumps({"summary": sums, "excluded": excluded}, indent=2))
    raise SystemExit
if args.runs:
    for v, rs in sorted(by.items()):
        for r in rs:
            print(f"  {v:9} {r['_file']:48} wall={r['wall_s']:>7} out={tok(r, 'output'):>7} in={tok(r, 'input'):>8} hidden={r['hidden']['passed']}/{r['hidden']['total']} timeout={r['timed_out']}")
    print()
print(f"{'variant':10} {'n':>2} {'wall_s':>8} {'input':>9} {'output':>8} {'cache_rd':>10} {'steps':>6} {'pass':>6} {'timeouts':>8}")
for v, s in sums.items():
    print(f"{v:10} {s['n']:>2} {s['wall_s']:>8} {s['input']:>9} {s['output']:>8} {s['cache_read']:>10} {s['steps']:>6} {s['pass_rate']:>6} {s['timeouts']:>8}")
if "baseline" in sums and "current" in sums:
    b, c = sums["baseline"], sums["current"]

    def delta(k):
        return f"{(c[k] - b[k]) / b[k] * 100:+.0f}%" if b[k] else "n/a"

    print(f"\ndelta (current vs baseline): wall {delta('wall_s')}, input {delta('input')}, output {delta('output')}, steps {delta('steps')}, pass rate {c['pass_rate'] - b['pass_rate']:+.2f}")
for name, why in excluded:
    print(f"excluded: {name} ({why})")
