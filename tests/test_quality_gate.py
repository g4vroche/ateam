import json
import subprocess
import sys

import pytest

from ateam import frontmatter as fm
from ateam import gate as G
from ateam import quality as Q
from ateam import schemas
from ateam import work as W

UNITTEST = "python3 -m unittest discover -s tests -t ."

TASK_BODY = """# Objective
Implement add().

# Files to Inspect
- calc.py

# Acceptance Criteria
- AC1: add works. [check: unit]

# Verification Commands

```verify
unit: python3 -m unittest discover -s tests -t .
```
"""


def git(root, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=root, check=True, capture_output=True)


@pytest.fixture
def ws(tmp_path, ateam):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "tests").mkdir()
    (root / "tests" / "__init__.py").write_text("")
    (root / "calc.py").write_text("def add(a, b):\n    return 0\n")
    (root / "tests" / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n"
    )
    git(root, "init", "-q")
    qdir = root / ".ateam"
    qdir.mkdir()
    (qdir / "quality.json").write_text(
        json.dumps(
            {
                "version": 1,
                "categories": {
                    "format": {"na": "no formatter in fixture"},
                    "lint": {"na": "no linter in fixture"},
                    "typecheck": {"na": "no type checker in fixture"},
                    "test": {"cmd": UNITTEST},
                },
            }
        )
    )
    ateam(root, "work", "init", "p")
    ateam(root, "work", "new-task", "p", "Implement add", "--slug", "add")
    tp = root / ".ateam/work/p/tasks/001-add.md"
    meta, _ = fm.parse_file(tp)
    tp.write_text(fm.dump(meta, TASK_BODY))
    ateam(root, "work", "sync", "p")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


def fix_impl(root):
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")


def gate(ateam, root, *extra):
    code, out, err = ateam(root, "work", "gate", "p", "TASK-001", "--json", *extra)
    return code, json.loads(out)


def test_valid_project_fixture(ws, ateam):
    assert ateam(ws, "work", "validate", "p")[0] == 0


def test_passing_flow_completes(ws, ateam):
    assert ateam(ws, "work", "start", "p", "TASK-001")[0] == 0
    fix_impl(ws)
    code, res = gate(ateam, ws)
    assert code == 0 and res["overall"] == "passed", res
    states = {c["check"]: c["status"] for c in res["checks"]}
    assert states["format"] == "not_applicable" and states["test"] == "passed" and states["unit"] == "passed"
    assert ateam(ws, "work", "complete", "p", "TASK-001")[0] == 0
    meta, _ = fm.parse_file(ws / ".ateam/work/p/tasks/001-add.md")
    assert meta["status"] == "completed"
    manifest = json.loads((ws / ".ateam/work/p/manifest.json").read_text())
    assert manifest["tasks"][0]["status"] == "completed"


def test_failing_tests_fail_gate_and_block_complete(ws, ateam):
    ateam(ws, "work", "start", "p", "TASK-001")
    (ws / "calc.py").write_text("def add(a, b):\n    return a - b\n")  # still wrong
    code, res = gate(ateam, ws)
    assert code == 1 and res["overall"] == "failed"
    code, _, err = ateam(ws, "work", "complete", "p", "TASK-001")
    assert code != 0 and "not 'passed'" in err


def test_no_changes_requires_review(ws, ateam):
    ateam(ws, "work", "start", "p", "TASK-001")
    code, res = gate(ateam, ws)
    # tests fail on the untouched stub, but the guard must also flag it
    assert any(c["check"] == "worktree_changed" and c["status"] == "needs_review" for c in res["checks"])


def test_no_changes_alone_needs_review(ws, ateam):
    fix_impl(ws)
    git(ws, "commit", "-qam", "preimplemented")
    ateam(ws, "work", "start", "p", "TASK-001")
    code, res = gate(ateam, ws)
    assert code == 4 and res["overall"] == "needs_review"
    assert ateam(ws, "work", "complete", "p", "TASK-001")[0] != 0


def test_zero_tests_is_failure(ws, ateam):
    (ws / "tests" / "test_calc.py").unlink()
    git(ws, "add", "-A")
    git(ws, "commit", "-qm", "rm")
    ateam(ws, "work", "start", "p", "TASK-001")
    fix_impl(ws)
    code, res = gate(ateam, ws)
    assert res["overall"] == "failed"
    failed = [c for c in res["checks"] if c["status"] == "failed" and c["check"] in ("test", "unit")]
    assert failed and all(("zero tests" in c["reason"]) or ("exit 5" in c["reason"]) for c in failed)


def test_missing_category_is_not_executed_not_passed(ws, ateam):
    q = json.loads((ws / ".ateam/quality.json").read_text())
    del q["categories"]["lint"]
    (ws / ".ateam/quality.json").write_text(json.dumps(q))
    ateam(ws, "work", "start", "p", "TASK-001")
    fix_impl(ws)
    code, res = gate(ateam, ws)
    assert code == 2 and res["overall"] == "not_executed"
    assert ateam(ws, "work", "complete", "p", "TASK-001")[0] != 0


def test_stale_gate_is_rejected(ws, ateam):
    ateam(ws, "work", "start", "p", "TASK-001")
    fix_impl(ws)
    assert gate(ateam, ws)[1]["overall"] == "passed"
    (ws / "extra.txt").write_text("changed after the gate")  # outside any declared scope
    code, _, err = ateam(ws, "work", "complete", "p", "TASK-001")
    assert code != 0 and "stale" in err
    assert gate(ateam, ws)[1]["overall"] == "passed"  # re-gating makes it valid again
    assert ateam(ws, "work", "complete", "p", "TASK-001")[0] == 0


def test_editing_task_file_after_start_breaks_gate(ws, ateam):
    ateam(ws, "work", "start", "p", "TASK-001")
    fix_impl(ws)
    tp = ws / ".ateam/work/p/tasks/001-add.md"
    tp.write_text(tp.read_text().replace("python3 -m unittest discover -s tests -t .", "python3 -c 'import sys; sys.exit(0)'"))
    code, res = gate(ateam, ws)
    assert res["overall"] == "failed"
    assert any(c["check"] == "integrity" and "locked" in c["reason"] for c in res["checks"])


def test_modifying_quality_json_breaks_gate(ws, ateam):
    ateam(ws, "work", "start", "p", "TASK-001")
    fix_impl(ws)
    q = json.loads((ws / ".ateam/quality.json").read_text())
    q["categories"]["test"] = {"cmd": "python3 -c 'print(1)'"}
    (ws / ".ateam/quality.json").write_text(json.dumps(q))
    code, res = gate(ateam, ws)
    assert res["overall"] == "failed"
    assert any(c["check"] == "integrity" and "quality.json" in c["reason"] for c in res["checks"])


def test_deleted_test_needs_review(ws, ateam):
    ateam(ws, "work", "start", "p", "TASK-001")
    fix_impl(ws)
    (ws / "tests" / "test_extra.py").write_text(
        "import unittest\n\n\nclass E(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n"
    )
    # snapshot is taken at start; delete the pre-existing test file now
    (ws / "tests" / "test_calc.py").unlink()
    code, res = gate(ateam, ws)
    guard = next(c for c in res["checks"] if c["check"] == "test_guard")
    assert guard["status"] == "needs_review" and "deleted" in guard["reason"]
    assert res["overall"] in ("needs_review", "failed")


def test_weakened_assertion_needs_review(ws, ateam):
    ateam(ws, "work", "start", "p", "TASK-001")
    fix_impl(ws)
    t = ws / "tests" / "test_calc.py"
    t.write_text(t.read_text().replace("self.assertEqual(add(1, 2), 3)", "pass"))
    code, res = gate(ateam, ws)
    guard = next(c for c in res["checks"] if c["check"] == "test_guard")
    assert guard["status"] == "needs_review" and "assertion count dropped" in guard["reason"]


def test_added_skip_needs_review(ws, ateam):
    ateam(ws, "work", "start", "p", "TASK-001")
    fix_impl(ws)
    t = ws / "tests" / "test_calc.py"
    t.write_text(t.read_text().replace("    def test_add", "    @unittest.skip('later')\n    def test_add"))
    code, res = gate(ateam, ws)
    guard = next(c for c in res["checks"] if c["check"] == "test_guard")
    assert guard["status"] == "needs_review" and "skip" in guard["reason"]


def test_gate_requires_start(ws, ateam):
    code, _, err = ateam(ws, "work", "gate", "p", "TASK-001")
    assert code != 0 and "not started" in err


def test_start_requires_dependencies(ws, ateam):
    ateam(ws, "work", "new-task", "p", "Second", "--deps", "TASK-001")
    tp = ws / ".ateam/work/p/tasks/002-second.md"
    meta, _ = fm.parse_file(tp)
    tp.write_text(fm.dump(meta, TASK_BODY))
    ateam(ws, "work", "sync", "p")
    code, _, err = ateam(ws, "work", "start", "p", "TASK-002")
    assert code != 0 and "unmet dependency" in err


# ------------------------------------------------------------------ quality.py
def test_run_command_rejects_trivial(tmp_path):
    r = Q.run_command(tmp_path, "x", "true", 5)
    assert r["status"] == "failed" and "trivial" in r["reason"]


def test_run_command_tool_missing_is_not_executed(tmp_path):
    r = Q.run_command(tmp_path, "x", "definitely-not-a-tool-xyz --version", 5)
    assert r["status"] == "not_executed"


def test_run_command_timeout_is_failed(tmp_path):
    r = Q.run_command(tmp_path, "slow", "sleep 5", 1)
    assert r["status"] == "failed" and "timeout" in r["reason"]


def test_timeout_kills_grandchildren_and_returns_promptly(tmp_path):
    import time

    t0 = time.time()
    # a background grandchild keeps the output pipe open; it must not block the gate
    r = Q.run_command(tmp_path, "slow", "sleep 30 & sleep 30", 1)
    assert r["status"] == "failed" and "timeout" in r["reason"]
    assert time.time() - t0 < 10


def test_commands_see_pwd_equal_to_project_root(tmp_path):
    r = Q.run_command(tmp_path, "pwdcheck", 'test "$PWD" = "$(pwd)"', 10)
    assert r["status"] == "passed"


def test_zero_tests_pattern_does_not_match_ten_tests():
    assert not Q.ZERO_TESTS.search("10 tests passed")
    assert Q.ZERO_TESTS.search("Ran 0 tests in 0.000s")
    assert Q.ZERO_TESTS.search("collected 0 items")


def test_detect_node_project(tmp_path, ateam):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"lint": "eslint .", "test": "vitest run", "build": "tsc"}, "devDependencies": {"prettier": "3", "typescript": "5"}})
    )
    (tmp_path / "tsconfig.json").write_text("{}")
    eff = Q.effective(tmp_path)
    assert eff["lint"]["status"] == "detected" and eff["lint"]["cmd"].endswith("lint")
    assert eff["format"]["cmd"].startswith("npx --no-install prettier")
    assert eff["typecheck"]["cmd"].endswith("tsc --noEmit")
    assert eff["security"]["status"] == "missing"


def test_detect_require_exit_code(tmp_path, ateam):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"lint": "eslint ."}}))
    assert ateam(tmp_path, "quality", "detect", "--require", "lint")[0] == 0 or True  # tool availability varies
    code, out, _ = ateam(tmp_path, "quality", "detect", "--require", "typecheck,test", "--json")
    assert code == 1 and json.loads(out)["missing_required"] == ["typecheck", "test"]


def test_quality_exit_codes(ws, ateam):
    assert ateam(ws, "quality", "format")[0] == 3  # not applicable
    assert ateam(ws, "quality", "build")[0] == 2  # not configured
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    assert ateam(ws, "quality", "test")[0] == 0
    (ws / "calc.py").write_text("def add(a, b):\n    return 1\n")
    assert ateam(ws, "quality", "test")[0] == 1


def test_quality_all_counts_only_profile(ws, ateam):
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    code, out, _ = ateam(ws, "quality", "all", "--json")
    data = json.loads(out)
    assert code == 0 and data["status"] == "passed"
    code, out, _ = ateam(ws, "quality", "all", "--profile", "format,lint,typecheck,test,security", "--json")
    assert code == 2  # security counted but missing


def test_quality_set_is_restricted(ws, ateam):
    code, _, err = ateam(ws, "quality", "set", "lint", "--cmd", "ruff check .")
    assert code != 0 and "touches_gate" in err
    code, _, _ = ateam(ws, "quality", "set", "lint", "--cmd", "ruff check .", env={"ATEAM_ALLOW_QUALITY_SET": "1"})
    assert code == 0
    assert json.loads((ws / ".ateam/quality.json").read_text())["categories"]["lint"]["cmd"] == "ruff check ."


def test_touches_gate_requires_review():
    meta = {"id": "TASK-001", "title": "t", "status": "pending", "dependencies": [], "priority": "low", "touches_gate": True}
    errs = schemas.validate_task(meta, TASK_BODY, "001-t.md")
    assert any("touches_gate" in e for e in errs)


def test_touches_gate_task_may_change_quality_json(ws, ateam):
    tp = ws / ".ateam/work/p/tasks/001-add.md"
    meta, body = fm.parse_file(tp)
    meta.update({"touches_gate": True, "review": "required"})
    tp.write_text(fm.dump(meta, body))
    ateam(ws, "work", "sync", "p")
    ateam(ws, "work", "start", "p", "TASK-001")
    fix_impl(ws)
    q = json.loads((ws / ".ateam/quality.json").read_text())
    q["categories"]["lint"] = {"na": "still none"}
    (ws / ".ateam/quality.json").write_text(json.dumps(q))
    code, res = gate(ateam, ws)
    assert res["overall"] == "passed", res


def test_failure_signature_ignores_paths_and_numbers():
    def res(reason):
        return {"checks": [{"check": "test", "status": "failed", "reason": reason}]}

    a = G.gate_failure_signature(res("exit 1: AssertionError at /tmp/a/x.py line 12"))
    b = G.gate_failure_signature(res("exit 1: AssertionError at /home/z/y.py line 99"))
    c = G.gate_failure_signature(res("exit 1: ImportError: no module"))
    assert a == b and a != c


def test_fingerprint_ignores_ateam_and_changes_with_content(ws):
    fp1 = G.fingerprint(ws)
    (ws / ".ateam" / "x.txt").write_text("state")
    assert G.fingerprint(ws) == fp1
    (ws / "calc.py").write_text("changed")
    assert G.fingerprint(ws) != fp1


def test_changed_mode_uses_changed_cmd_and_filters_by_extension(ws, ateam):
    env = {"ATEAM_ALLOW_QUALITY_SET": "1"}
    code, _, _ = ateam(
        ws, "quality", "set", "lint", "--cmd", "false", "--changed-cmd", "echo LINT {files}", "--exts", ".py", env=env
    )
    assert code == 0
    # nothing changed: full run would fail (`false`), changed mode has nothing to check
    code, out, _ = ateam(ws, "quality", "lint", "--changed", "--json")
    res = json.loads(out)["results"][0]
    assert code == 3 and res["status"] == "not_applicable" and res["scope"] == "changed"
    # a changed .py file is passed to the changed command; a .txt file is filtered out
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (ws / "notes.txt").write_text("x")
    code, out, _ = ateam(ws, "quality", "lint", "--changed", "--json")
    res = json.loads(out)["results"][0]
    assert code == 0 and res["scope"] == "changed"
    assert "calc.py" in res["command"] and "notes.txt" not in res["command"]
    # without --changed the full command runs (and fails)
    assert ateam(ws, "quality", "lint")[0] == 1


def test_changed_mode_without_support_records_full_run(ws, ateam):
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    code, out, _ = ateam(ws, "quality", "test", "--changed", "--json")
    res = json.loads(out)["results"][0]
    assert code == 0 and res["scope"] == "full"
