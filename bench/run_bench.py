#!/usr/bin/env python3
"""Benchmark one variant of the boilerplate on a fixed task.

  python3 bench/run_bench.py --variant baseline|current [--task todo-cli] [--timeout 2700] [--agent chef]

Creates a throwaway git workspace, installs the variant's .opencode/, runs the chef agent headlessly
(`opencode run --standalone --format json --auto`), then runs the hidden acceptance checks and
collects `opencode stats --project .` for the workspace. Results go to bench/results/*.json.
Requires a working model; results vary between runs, so repeat (N>=3) and compare medians.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BENCH = REPO / "bench"


def env_for(cwd):
    """opencode resolves its project directory from $PWD, not from the process cwd.
    Popen(cwd=...) alone leaves the parent's PWD in place and the agent then edits the
    parent's directory (observed: a baseline run wrote into this repository)."""
    return dict(os.environ, PWD=str(cwd))


def sh(cmd, cwd, timeout=120):
    return subprocess.run(cmd, cwd=cwd, env=env_for(cwd), capture_output=True, text=True, timeout=timeout)


def leak_check(ws, t0):
    """Files the agent created in the workspace that also appeared in the ateam repository during the run.
    (Comparing `git status` before/after is not usable while the repository is being edited.)"""
    leaked = []
    for p in ws.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ws)
        if rel.parts[0] in (".git", ".opencode", "__pycache__") or "__pycache__" in rel.parts:
            continue
        twin = REPO / rel
        if twin.is_file() and twin.stat().st_mtime >= t0:
            leaked.append(str(rel))
    return leaked


def repo_state():
    r = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True)
    return r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", required=True, choices=["baseline", "current"])
    ap.add_argument("--task", default="todo-cli")
    ap.add_argument("--timeout", type=int, default=2700)
    ap.add_argument("--agent", default="chef")
    ap.add_argument("--opencode", default="opencode")
    ap.add_argument("--keep", action="store_true", help="keep the workspace directory")
    args = ap.parse_args()

    task_dir = BENCH / "tasks" / args.task
    request = (task_dir / "request.md").read_text(encoding="utf-8")
    # BENCH_CURRENT_SRC pins the `current` variant to a frozen copy (a directory containing .opencode/), so the
    # boilerplate can keep evolving while a benchmark series is running.
    pin_file = BENCH / "current-src.txt"
    pinned = os.environ.get("BENCH_CURRENT_SRC") or (pin_file.read_text().strip() if pin_file.exists() else None)
    src = BENCH / "baseline-v0" / ".opencode" if args.variant == "baseline" else (Path(pinned) / ".opencode" if pinned else REPO / ".opencode")
    ws = Path(tempfile.mkdtemp(prefix=f"bench-{args.variant}-"))
    shutil.copytree(src, ws / ".opencode", ignore=shutil.ignore_patterns("__pycache__"))
    sh(["git", "init", "-q"], ws)
    sh(["git", "-c", "user.name=bench", "-c", "user.email=b@b", "add", "-A"], ws)
    sh(["git", "-c", "user.name=bench", "-c", "user.email=b@b", "commit", "-qm", "init"], ws)

    prompt = request
    cmd = [args.opencode, "run", "--standalone", "--agent", args.agent, "--format", "json", "--auto", prompt]
    t0 = time.time()
    before = repo_state()
    proc = subprocess.Popen(cmd, cwd=ws, env=env_for(ws), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    events = {"types": {}, "tools": {}, "tokens": {"input": 0, "output": 0, "cache_read": 0}, "steps": 0, "sessions": set()}
    timed_out = False
    import threading

    def reader():
        for line in proc.stdout:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t = e.get("type", "?")
            events["types"][t] = events["types"].get(t, 0) + 1
            if e.get("sessionID"):
                events["sessions"].add(e["sessionID"])
            part = e.get("part", {})
            if t == "tool_use":
                events["tools"][part.get("tool", "?")] = events["tools"].get(part.get("tool", "?"), 0) + 1
            if t == "step_finish":
                tk = part.get("tokens", {})
                events["steps"] += 1
                events["tokens"]["input"] += tk.get("input", 0)
                events["tokens"]["output"] += tk.get("output", 0)
                events["tokens"]["cache_read"] += tk.get("cache", {}).get("read", 0)

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    try:
        proc.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, signal.SIGKILL)
    th.join(5)
    wall = time.time() - t0
    leaked_paths = leak_check(ws, t0)
    leaked = bool(leaked_paths)
    if leaked:
        print("ERROR: the run modified the ateam repository; result is invalid", file=sys.stderr)

    stats = None
    try:
        out = sh([args.opencode, "stats", "--all", "--json", "--project", ".", "--tools", "--models"], ws)
        stats = json.loads(out.stdout)
        for k in ("activity",):
            stats.pop(k, None)
    except Exception as exc:  # noqa: BLE001
        stats = {"error": str(exc)}

    hidden = None
    try:
        out = sh([sys.executable, str(task_dir / "hidden_check.py"), str(ws)], ws, timeout=300)
        hidden = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        hidden = {"passed": 0, "total": 0, "error": str(exc)}

    result = {
        "variant": args.variant,
        "task": args.task,
        "agent": args.agent,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "wall_s": round(wall, 1),
        "timed_out": timed_out,
        "leaked_into_repo": leaked,
        "leaked_paths": leaked_paths,
        "exit_code": proc.returncode,
        "stream": {**events, "sessions": len(events["sessions"])},
        "stats": stats,
        "hidden": hidden,
        "workspace": str(ws) if args.keep else None,
    }
    outdir = BENCH / "results"
    outdir.mkdir(exist_ok=True)
    out_path = outdir / f"{args.variant}-{args.task}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(json.dumps({k: result[k] for k in ("variant", "wall_s", "timed_out", "hidden")}, indent=2))
    print("saved", out_path)
    if not args.keep:
        shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    main()
