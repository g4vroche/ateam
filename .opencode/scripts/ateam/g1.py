"""Gate G1: go/no-go for the learning agents (SkillSmith and Dreamer).

`ateam.py g1` evaluates three criteria from evidence on disk and writes `.ateam/g1.json`:
  1. benchmark: the current variant is faster or cheaper than baseline-v0 (>= 10% in wall time or output tokens)
     with a hidden-test pass rate that is not lower, from at least `g1_min_bench` runs per variant (default 2).
  2. real runs: at least `g1_min_runs` (default 5) real runs (not fake-model test runs) that produced captured events.
  3. patterns: at least one feedback signature recurs in two or more distinct real runs.
`improve new|approve|promote|deprecate` and `knowledge dream` refuse to run unless the decision is `go`
(or `learning_enabled` is set explicitly in `.ateam/config.json`).
"""
from __future__ import annotations

import glob
import json
import statistics
from pathlib import Path

from . import feedback as F
from .common import ateam_dir, emit, find_root, load_config, now_iso, read_json, write_json

GAIN = 0.10


def _tok(r: dict, key: str) -> float:
    t = ((r.get("stats") or {}).get("tokens")) or {}
    return float(t.get(key, 0) or 0)


def bench_criterion(root: Path, bench_dir: Path | None, min_runs: int) -> dict:
    bdir = bench_dir or root / "bench" / "results"
    by: dict[str, list[dict]] = {"baseline": [], "current": []}
    for f in sorted(glob.glob(str(bdir / "*.json"))):
        try:
            r = json.loads(Path(f).read_text())
        except (OSError, ValueError):
            continue
        if r.get("variant") not in by or r.get("leaked_into_repo") or not (r.get("hidden") or {}).get("total"):
            continue
        by[r["variant"]].append(r)
    out: dict = {"name": "benchmark", "n": {k: len(v) for k, v in by.items()}}
    if min(len(v) for v in by.values()) < min_runs:
        out.update(met=False, status="insufficient-data", detail=f"need >= {min_runs} valid runs per variant, have {out['n']}")
        return out

    def stats(rs):
        return {
            "wall_s": statistics.median(r["wall_s"] for r in rs),
            "output_tokens": statistics.median(_tok(r, "output") for r in rs),
            "pass_rate": statistics.mean(r["hidden"]["passed"] / r["hidden"]["total"] for r in rs),
            "timeouts": sum(1 for r in rs if r.get("timed_out")),
        }

    b, c = stats(by["baseline"]), stats(by["current"])
    faster = c["wall_s"] <= b["wall_s"] * (1 - GAIN)
    cheaper = b["output_tokens"] > 0 and c["output_tokens"] <= b["output_tokens"] * (1 - GAIN)
    no_loss = c["pass_rate"] >= b["pass_rate"] and c["timeouts"] <= b["timeouts"]
    met = (faster or cheaper) and no_loss
    out.update(met=met, status="met" if met else "not-met", baseline=b, current=c, faster=faster, cheaper=cheaper, correctness_not_lower=no_loss,
               detail=("gain without lower correctness" if met else "no >=10% gain in wall time or output tokens, or correctness/timeouts got worse"))
    return out


def runs_criterion(root: Path, min_runs: int) -> tuple[dict, dict]:
    events = F.read_events(root)[0]
    by_run: dict[str, list[dict]] = {}
    real_runs, total = [], 0
    for p in sorted((ateam_dir(root) / "runs").glob("RUN-*/summary.json")) if (ateam_dir(root) / "runs").is_dir() else []:
        s = read_json(p, {}) or {}
        total += 1
        if s.get("real"):
            real_runs.append(s["run"])
    for e in events:
        if e["run"] in real_runs:
            by_run.setdefault(e["run"], []).append(e)
    met = len(by_run) >= min_runs
    return (
        {"name": "real runs with captured events", "met": met, "status": "met" if met else "not-met", "real_runs": len(real_runs), "with_events": len(by_run), "summaries_total": total, "required": min_runs,
         "detail": f"{len(by_run)} of {len(real_runs)} real runs produced events (need {min_runs})"},
        by_run,
    )


def pattern_criterion(by_run: dict[str, list[dict]]) -> dict:
    flat = [e for evs in by_run.values() for e in evs]
    recurring = [g for g in F.group(flat) if len(g["runs"]) >= 2]
    met = bool(recurring)
    return {
        "name": "recurring patterns", "met": met, "status": "met" if met else "not-met",
        "recurring": [{"signature": g["signature"], "runs": len(g["runs"]), "count": g["count"]} for g in recurring[:10]],
        "detail": f"{len(recurring)} signature(s) recur across >= 2 real runs",
    }


def evaluate(root: Path, bench_dir: Path | None = None) -> dict:
    cfg = load_config(root)
    c1 = bench_criterion(root, bench_dir, cfg.get("g1_min_bench", 2))
    c2, by_run = runs_criterion(root, cfg["g1_min_runs"])
    c3 = pattern_criterion(by_run)
    crits = [c1, c2, c3]
    if all(c["met"] for c in crits):
        decision = "go"
    else:
        decision = "no-go"
    reasons = [f"{c['name']}: {c['detail']}" for c in crits if not c["met"]]
    return {"decision": decision, "at": now_iso(), "criteria": crits, "reasons": reasons,
            "next": "tune the core loop, collect more real runs, then re-run `ateam.py g1`" if decision != "go" else "SkillSmith (/improve) and Dreamer (/dream) are unlocked"}


def cmd_g1(args) -> int:
    root = find_root(args.root)
    res = evaluate(root, Path(args.bench_dir) if args.bench_dir else None)
    if not args.dry_run:
        write_json(ateam_dir(root) / "g1.json", res)
    lines = [f"G1 decision: {res['decision'].upper()}"]
    for c in res["criteria"]:
        lines.append(f"  [{'x' if c['met'] else ' '}] {c['name']}: {c['detail']}")
    lines.append(res["next"])
    emit(res, args.json, "\n".join(lines))
    return 0 if res["decision"] == "go" else 10


def register(sub) -> None:
    p = sub.add_parser("g1", help="evaluate gate G1 (unlocks SkillSmith and Dreamer)")
    p.add_argument("--bench-dir", help="directory of bench result JSON files (default bench/results)")
    p.add_argument("--dry-run", action="store_true", help="do not write .ateam/g1.json")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_g1)
