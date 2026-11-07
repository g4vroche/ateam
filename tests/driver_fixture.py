"""Shared fixture for driver tests: a tiny Python project, a 3-task work project and a scripted fake opencode."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".opencode" / "scripts"
SHIM = REPO / "tests" / "fakes" / "fake_opencode.py"
UNITTEST = "python3 -m unittest discover -s tests -t ."

PLAN = (
    "# Objective\nCalculator functions.\n\n# Scope and Non-Goals\nadd, sub, mul.\n\n# Architectural Decisions\nOne module.\n\n"
    "# Shared Constraints\nStdlib only.\n\n# Task Graph\nTASK-001 -> TASK-002 -> TASK-003\n\n# Acceptance Criteria\nAll gates pass.\n\n# Progress\n0/3\n"
)

FUNCS = [("add", "add", "a + b", 5), ("sub", "subtract", "a - b", 1), ("mul", "multiply", "a * b", 6)]


def task_text(n: int, fn: str, expected: int, deps: list[str], review: str = "none", extra: str = "") -> str:
    d = "[" + ", ".join(deps) + "]"
    return f"""---
id: TASK-{n:03d}
title: Implement {fn}
status: pending
dependencies: {d}
priority: medium
review: {review}
max_attempts: 3
files: [calc.py, tests/]
reads: []
knowledge: []
skills: []
quality: [test]
{extra}---

# Objective
Implement {fn}(a, b) in calc.py with a unit test.

# Files to Inspect
- calc.py

# Acceptance Criteria
- AC1: {fn}(3, 2) returns {expected}. [check: {fn}]

# Verification Commands

```verify
{fn}: python3 -c "import calc; assert calc.{fn}(3, 2) == {expected}"
```
"""


def impl_rule(n: int, fn: str, expr: str, **extra) -> dict:
    rule = {
        "agent": "coder",
        "match": f"tasks/{n:03d}-",
        "append": {"calc.py": f"\n\ndef {fn}(a, b):\n    return {expr}\n"},
        "write": {
            f"tests/test_{fn}.py": f"import unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n    def test_{fn}(self):\n        self.assertEqual(calc.{fn}(3, 2), {dict((f[0], f[3]) for f in FUNCS)[fn]})\n"
        },
        "report": f"Implemented {fn}.",
    }
    rule.update(extra)
    return rule


APPROVE_ALL = [
    {"agent": "tech-lead", "verdict": "APPROVED"},
    {"agent": "tester", "verdict": "APPROVED"},
]


def git(root, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=root, check=True, capture_output=True)


class Fixture:
    def __init__(self, tmp_path: Path, reviews: dict[int, str] | None = None, config: dict | None = None):
        self.root = tmp_path / "ws"
        self.state = tmp_path / "fake-state"
        self.script_path = tmp_path / "fake-script.json"
        root = self.root
        (root / "tests").mkdir(parents=True)
        (root / "tests" / "__init__.py").write_text("")
        (root / "tests" / "test_base.py").write_text("import unittest\n\n\nclass B(unittest.TestCase):\n    def test_base(self):\n        self.assertTrue(True)\n")
        (root / "calc.py").write_text('"""calc"""\n')
        (root / ".opencode").mkdir()
        (root / ".opencode" / "scripts").symlink_to(SCRIPTS)
        git(root, "init", "-q")
        a = root / ".ateam"
        a.mkdir()
        cfg = {"opencode_bin": f"{sys.executable} {SHIM}", "role_retries": 0, "retry_backoff_s": 0, "task_timeout_s": 60, "role_timeout_s": 60, "gate_timeout_s": 60}
        cfg.update(config or {})
        (a / "config.json").write_text(json.dumps(cfg))
        (a / "quality.json").write_text(
            json.dumps({"version": 1, "categories": {"format": {"na": "fixture"}, "lint": {"na": "fixture"}, "typecheck": {"na": "fixture"}, "test": {"cmd": UNITTEST}}})
        )
        self.ateam("work", "init", "calc")
        p = a / "work" / "calc"
        (p / "plan.md").write_text(PLAN)
        reviews = reviews or {}
        deps: list[str] = []
        for i, (fn, _, _, exp) in enumerate(FUNCS, start=1):
            (p / "tasks" / f"{i:03d}-{fn}.md").write_text(task_text(i, fn, exp, list(deps), reviews.get(i, "none")))
            deps = [f"TASK-{i:03d}"]
        self.ateam("work", "sync", "calc")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "init")

    # ---------------------------------------------------------------- helpers
    def env(self, **extra):
        e = dict(os.environ, FAKE_OC_SCRIPT=str(self.script_path), FAKE_OC_STATE=str(self.state), PWD=str(self.root))
        e.pop("ATEAM_ROOT", None)
        e.update(extra)
        return e

    def script(self, rules: list[dict]) -> None:
        self.script_path.write_text(json.dumps({"rules": rules}))

    def ateam(self, *args, check=False):
        p = subprocess.run([sys.executable, str(SCRIPTS / "ateam.py"), "--root", str(self.root), *args], capture_output=True, text=True, env=self.env())
        if check:
            assert p.returncode == 0, p.stderr + p.stdout
        return p.returncode, p.stdout, p.stderr

    def popen(self, *args):
        return subprocess.Popen([sys.executable, str(SCRIPTS / "ateam.py"), "--root", str(self.root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env())

    def run(self, *args):
        return self.ateam("run", "calc", *args)

    def calls(self, agent: str | None = None) -> list[dict]:
        p = self.state / "calls.jsonl"
        if not p.exists():
            return []
        rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
        return [r for r in rows if agent is None or r["agent"] == agent]

    def pdir(self) -> Path:
        return self.root / ".ateam" / "work" / "calc"

    def status(self, tid: str) -> str:
        import re

        text = next((self.pdir() / "tasks").glob(f"{tid[5:]}-*.md")).read_text()
        return re.search(r"^status: (\S+)", text, re.M).group(1)

    def statuses(self) -> list[str]:
        return [self.status(f"TASK-{i:03d}") for i in (1, 2, 3)]

    def state_json(self, tid: str) -> dict:
        return json.loads((self.pdir() / "state" / f"{tid}.json").read_text())

    def last_run(self) -> dict:
        runs = sorted((self.root / ".ateam" / "runs").glob("RUN-*.json"))
        return json.loads(runs[-1].read_text())

    def wait_for(self, path: Path, timeout: float = 30.0) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if path.exists():
                return
            time.sleep(0.05)
        raise AssertionError(f"timeout waiting for {path}")
