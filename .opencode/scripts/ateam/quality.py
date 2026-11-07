"""Normalized, deterministic quality checks.

Exit codes (single category): 0 passed, 1 failed, 2 not executed / misconfigured, 3 not applicable.
`all`: 1 if any counted check failed, else 2 if any counted check was not executed, else 0.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .common import atomic_write, ateam_dir, die, emit, find_root, load_config, read_json, write_json

CATEGORIES = ("format", "lint", "typecheck", "test", "build", "security", "deps", "coverage", "architecture")
DEFAULT_PROFILE = ("format", "lint", "typecheck", "test")
EXIT = {"passed": 0, "failed": 1, "not_executed": 2, "not_applicable": 3}

ZERO_TESTS = re.compile(
    r"(no tests ran|collected 0 items|ran 0 tests|no test files|no tests found|(?<![\d.])0 tests? (?:passed|found|ran)|"
    r"tests:\s+0 total|no tests to run|running 0 tests|\b0 passing\b)",
    re.I,
)
MISSING_TOOL = re.compile(r"(command not found|not found: |No such file or directory: '|is not recognized|Cannot find module|No module named)", re.I)


# ----------------------------------------------------------------------- config
def quality_path(root: Path) -> Path:
    return ateam_dir(root) / "quality.json"


def load_quality(root: Path) -> dict:
    q = read_json(quality_path(root), None) or {}
    q.setdefault("version", 1)
    q.setdefault("categories", {})
    return q


# ------------------------------------------------------------------- detection
def _which(tool: str) -> bool:
    return shutil.which(tool) is not None


def _py_has(root: Path, module: str) -> bool:
    try:
        r = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, cwd=root, timeout=20)
        return r.returncode == 0
    except subprocess.SubprocessError:
        return False


def _read(root: Path, name: str) -> str:
    try:
        return (root / name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _entry(cmd: str, source: str, available: bool = True, **extra) -> dict:
    return {"cmd": cmd, "source": source, "available": available, **extra}


def detect(root: Path) -> dict:
    found: dict[str, dict] = {}

    def put(cat: str, entry: dict):
        found.setdefault(cat, entry)

    # Node
    pj = root / "package.json"
    if pj.exists():
        try:
            pkg = json.loads(pj.read_text(encoding="utf-8"))
        except ValueError:
            pkg = {}
        scripts = pkg.get("scripts", {}) or {}
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        runner = "pnpm run" if (root / "pnpm-lock.yaml").exists() else "yarn" if (root / "yarn.lock").exists() else "npm run"
        have_npm = _which("npm")

        def script(*names):
            for n in names:
                if n in scripts:
                    return n
            return None

        s = script("format:check", "check:format", "format-check", "prettier:check")
        if s:
            put("format", _entry(f"{runner} {s}", "package.json scripts", have_npm))
        elif "prettier" in deps:
            put("format", _entry("npx --no-install prettier --check .", "package.json deps", have_npm))
        s = script("lint")
        if s:
            put("lint", _entry(f"{runner} {s}", "package.json scripts", have_npm))
        elif "eslint" in deps:
            put("lint", _entry("npx --no-install eslint .", "package.json deps", have_npm))
        s = script("typecheck", "type-check", "tsc")
        if s:
            put("typecheck", _entry(f"{runner} {s}", "package.json scripts", have_npm))
        elif "typescript" in deps and (root / "tsconfig.json").exists():
            put("typecheck", _entry("npx --no-install tsc --noEmit", "package.json deps", have_npm))
        t = scripts.get("test")
        if t and "no test specified" not in t:
            put("test", _entry("npm test" if runner == "npm run" else f"{runner.split()[0]} test", "package.json scripts", have_npm))
        s = script("build")
        if s:
            put("build", _entry(f"{runner} {s}", "package.json scripts", have_npm))
    # Python
    pyproject = _read(root, "pyproject.toml")
    py_markers = pyproject or (root / "setup.cfg").exists() or (root / "requirements.txt").exists() or any(root.glob("*.py"))
    if py_markers:
        reqs = " ".join(_read(root, n) for n in ("requirements.txt", "requirements-dev.txt", "setup.cfg")) + pyproject
        if "[tool.ruff" in pyproject or re.search(r"\bruff\b", reqs):
            put("format", _entry("ruff format --check .", "python ruff", _which("ruff")))
            put("lint", _entry("ruff check .", "python ruff", _which("ruff")))
        elif re.search(r"\bblack\b", reqs):
            put("format", _entry("black --check .", "python black", _which("black")))
        if re.search(r"\bflake8\b", reqs) or (root / ".flake8").exists():
            put("lint", _entry("flake8", "python flake8", _which("flake8")))
        if "[tool.mypy" in pyproject or (root / "mypy.ini").exists():
            put("typecheck", _entry("mypy .", "python mypy", _which("mypy")))
        has_tests_dir = any((root / d).is_dir() for d in ("tests", "test"))
        pytest_cfg = (root / "pytest.ini").exists() or "[tool.pytest" in pyproject or re.search(r"\bpytest\b", reqs)
        if pytest_cfg or has_tests_dir:
            if pytest_cfg and _py_has(root, "pytest"):
                put("test", _entry(f"{sys.executable.split('/')[-1]} -m pytest -q", "python pytest", True))
            elif has_tests_dir:
                put("test", _entry("python3 -m unittest discover -s tests -t .", "python unittest", True))
    # Rust
    if (root / "Cargo.toml").exists():
        have = _which("cargo")
        put("format", _entry("cargo fmt --check", "cargo", have))
        put("lint", _entry("cargo clippy -- -D warnings", "cargo", have))
        put("typecheck", _entry("cargo check", "cargo", have))
        put("test", _entry("cargo test", "cargo", have))
        put("build", _entry("cargo build", "cargo", have))
    # Go
    if (root / "go.mod").exists():
        have = _which("go")
        put("format", _entry('test -z "$(gofmt -l .)"', "go", have))
        put("lint", _entry("go vet ./...", "go", have))
        put("test", _entry("go test ./...", "go", have))
        put("build", _entry("go build ./...", "go", have))
    # Makefile targets
    mk = _read(root, "Makefile")
    if mk:
        targets = set(re.findall(r"^([A-Za-z0-9_.-]+):", mk, re.M))
        have = _which("make")
        for cat, names in {
            "format": ("format-check", "fmt-check", "format", "fmt"),
            "lint": ("lint",),
            "typecheck": ("typecheck", "types"),
            "test": ("test", "tests", "check"),
            "build": ("build",),
        }.items():
            for n in names:
                if n in targets:
                    put(cat, _entry(f"make {n}", "Makefile", have))
                    break
    return found


def effective(root: Path) -> dict[str, dict]:
    """Per-category status: configured | detected | not_applicable | missing."""
    q = load_quality(root)
    det = detect(root)
    out: dict[str, dict] = {}
    for cat in CATEGORIES:
        c = (q["categories"] or {}).get(cat)
        if isinstance(c, dict) and c.get("na"):
            out[cat] = {"status": "not_applicable", "reason": c["na"]}
        elif isinstance(c, dict) and c.get("cmd"):
            out[cat] = {"status": "configured", **c}
        elif cat in det:
            out[cat] = {"status": "detected", **det[cat]}
        else:
            out[cat] = {"status": "missing"}
    return out


# --------------------------------------------------------------------- running
def changed_files(root: Path) -> list[str] | None:
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except subprocess.SubprocessError:
        return None
    files = []
    for rec in out.split("\0"):
        if len(rec) > 3:
            path = rec[3:]
            if not path.startswith((".ateam/", ".opencode/")):
                files.append(path)
    return files


def run_check(root: Path, cat: str, changed: bool = False, log_dir: Path | None = None, timeout: int | None = None) -> dict:
    cfg = load_config(root)
    eff = effective(root).get(cat, {"status": "missing"})
    base = {"check": cat, "kind": "quality"}
    if eff["status"] == "not_applicable":
        return {**base, "status": "not_applicable", "reason": eff["reason"], "duration_s": 0.0}
    if eff["status"] == "missing":
        return {**base, "status": "not_executed", "reason": f"no command configured or detected for '{cat}'", "duration_s": 0.0}
    if not eff.get("available", True):
        return {**base, "status": "not_executed", "reason": f"tool for '{eff.get('cmd')}' is not installed", "duration_s": 0.0, "command": eff.get("cmd")}
    cmd = eff["cmd"]
    scope = "full"
    if changed:
        files = changed_files(root)
        tmpl = eff.get("changed_cmd")
        if files is not None and tmpl and "{files}" in tmpl:
            exts = tuple(eff.get("exts") or ())
            sel = [f for f in files if (not exts or f.endswith(exts)) and (root / f).exists()]
            if not sel:
                return {**base, "status": "not_applicable", "reason": "no changed files of relevant type", "duration_s": 0.0, "scope": "changed"}
            cmd = tmpl.replace("{files}", " ".join(_shq(f) for f in sel))
            scope = "changed"
    return _exec(root, base, cmd, timeout or eff.get("timeout") or cfg["gate_timeout_s"], log_dir, scope, expect_tests=(cat == "test" and eff.get("expect_tests", True)))


def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def run_command(root: Path, check_id: str, cmd: str, timeout: int, log_dir: Path | None = None, expect_tests: bool | None = None) -> dict:
    """Run an arbitrary verify command (from a task) with the same classification as quality checks."""
    from .schemas import is_trivial_command

    base = {"check": check_id, "kind": "verify"}
    if is_trivial_command(cmd):
        return {**base, "status": "failed", "reason": f"trivial verify command rejected: {cmd!r}", "duration_s": 0.0, "command": cmd}
    return _exec(root, base, cmd, timeout, log_dir, "full", expect_tests=expect_tests if expect_tests is not None else bool(re.search(r"\b(test|pytest|jest|vitest|unittest|cargo test|go test)\b", cmd)))


def _exec(root: Path, base: dict, cmd: str, timeout: int, log_dir: Path | None, scope: str, expect_tests: bool) -> dict:
    t0 = time.time()
    env = dict(os.environ, CI="1", NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
        out = (p.stdout or "") + (p.stderr or "")
        code = p.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        out = ((exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")) + ((exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
        code = -1
        timed_out = True
    dur = round(time.time() - t0, 2)
    res = {**base, "command": cmd, "exit_code": code, "duration_s": dur, "scope": scope}
    log = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / f"{base['check']}.log"
        atomic_write(log, out)
        res["log"] = str(log)
    tail = [ln for ln in out.strip().splitlines() if ln.strip()][-12:]
    if timed_out:
        return {**res, "status": "failed", "reason": f"timeout after {timeout}s", "excerpt": tail}
    if code == 127 or (code != 0 and MISSING_TOOL.search(out) and len(out) < 2000):
        return {**res, "status": "not_executed", "reason": "tool or module missing", "excerpt": tail}
    if code != 0:
        first = next((ln.strip() for ln in out.splitlines() if re.search(r"(error|fail|assert|exception)", ln, re.I)), tail[-1] if tail else "")
        return {**res, "status": "failed", "reason": f"exit {code}: {first[:200]}", "excerpt": tail}
    if expect_tests and ZERO_TESTS.search(out):
        return {**res, "status": "failed", "reason": "command succeeded but collected zero tests", "excerpt": tail}
    return {**res, "status": "passed", "reason": "ok"}


def summarize(results: list[dict], counted: set[str] | None = None) -> str:
    rel = [r for r in results if counted is None or r["check"] in counted]
    if any(r["status"] == "failed" for r in rel):
        return "failed"
    if any(r["status"] == "not_executed" for r in rel):
        return "not_executed"
    return "passed"


# -------------------------------------------------------------------- commands
def fmt_result(r: dict) -> str:
    line = f"{r['check']:<12} {r['status']:<15} {r.get('duration_s', 0):>6.1f}s  {r.get('reason', '')}"
    if r["status"] == "failed" and r.get("excerpt"):
        line += "\n" + "\n".join("    | " + x for x in r["excerpt"][-8:])
    return line


def cmd_detect(args) -> int:
    root = find_root(args.root)
    eff = effective(root)
    req = [c for c in (args.require or "").split(",") if c]
    missing = [c for c in req if eff.get(c, {}).get("status") == "missing" or (eff.get(c, {}).get("status") in ("configured", "detected") and not eff[c].get("available", True))]
    data = {"categories": eff, "missing_required": missing}
    if args.json:
        emit(data, True)
    else:
        for cat in CATEGORIES:
            e = eff[cat]
            extra = e.get("cmd") or e.get("reason") or ""
            av = "" if e.get("available", True) else "  (tool not installed)"
            print(f"{cat:<13} {e['status']:<15} {extra}{av}")
        if req:
            print("missing required: " + (", ".join(missing) or "none"))
    return 1 if missing else 0


def cmd_check(args) -> int:
    root = find_root(args.root)
    cats = args.category
    log_dir = ateam_dir(root) / "runs" / "quality-logs"
    if cats == "all":
        profile = [c for c in (args.profile or ",".join(DEFAULT_PROFILE)).split(",") if c]
        eff = effective(root)
        to_run = [c for c in CATEGORIES if c in profile or eff[c]["status"] in ("configured", "detected")]
        results = [run_check(root, c, args.changed, log_dir) for c in to_run]
        status = summarize(results, set(profile))
        code = EXIT[status] if status != "passed" else 0
    else:
        results = [run_check(root, cats, args.changed, log_dir)]
        status = results[0]["status"]
        code = EXIT[status]
    if args.json:
        emit({"status": status, "results": results}, True)
    else:
        print("\n".join(fmt_result(r) for r in results))
        print(f"overall: {status}")
    return code


def cmd_set(args) -> int:
    root = find_root(args.root)
    if os.environ.get("ATEAM_ALLOW_QUALITY_SET") != "1" and not args.force:
        die("quality set is restricted to tasks with `touches_gate: true` (driver sets ATEAM_ALLOW_QUALITY_SET=1); ask the human or the architect")
    q = load_quality(root)
    if args.category not in CATEGORIES:
        die(f"unknown category {args.category!r}")
    entry: dict = {}
    if args.na:
        entry["na"] = args.na
    else:
        if not args.cmd:
            die("provide --cmd or --na REASON")
        entry["cmd"] = args.cmd
        if args.changed_cmd:
            entry["changed_cmd"] = args.changed_cmd
        if args.exts:
            entry["exts"] = [e.strip() for e in args.exts.split(",") if e.strip()]
        if args.timeout:
            entry["timeout"] = args.timeout
    q["categories"][args.category] = entry
    write_json(quality_path(root), q)
    emit({"category": args.category, **entry}, args.json, f"quality.json: {args.category} = {entry}")
    return 0


def register(sub) -> None:
    p = sub.add_parser("quality", help="normalized deterministic quality checks")
    qs = p.add_subparsers(dest="q_cmd", required=True)
    d = qs.add_parser("detect", help="report configured/detected tooling per category")
    d.add_argument("--require", help="comma list of categories that must be available (exit 1 otherwise)")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_detect)
    for cat in (*CATEGORIES, "all"):
        c = qs.add_parser(cat, help=f"run {cat} check" if cat != "all" else "run the profile (and any configured category)")
        c.add_argument("--changed", action="store_true", help="focus on changed files where the tool supports it")
        c.add_argument("--profile", help="comma list counted for the overall result (default format,lint,typecheck,test)")
        c.add_argument("--json", action="store_true")
        c.set_defaults(func=cmd_check, category=cat)
    s = qs.add_parser("set", help="write a category mapping into .ateam/quality.json (gate-touching tasks only)")
    s.add_argument("category")
    s.add_argument("--cmd")
    s.add_argument("--changed-cmd", dest="changed_cmd")
    s.add_argument("--exts")
    s.add_argument("--timeout", type=int)
    s.add_argument("--na", help="mark not applicable, with a reason")
    s.add_argument("--force", action="store_true", help="human override")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_set)
