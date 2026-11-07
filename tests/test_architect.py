import json
import shutil
import subprocess

import pytest

from ateam import agents as A
from ateam import lint as L
from ateam import schemas
from ateam import work as W
from conftest import REPO

needs_opencode = pytest.mark.skipif(shutil.which("opencode") is None, reason="opencode not on PATH")


def resolved_agents():
    out = subprocess.run(["opencode", "debug", "agents"], capture_output=True, text=True, cwd=REPO, env=_env())
    return json.loads(out.stdout)


def _env():
    import os

    return dict(os.environ, PWD=str(REPO))


# --------------------------------------------------------------- adhoc fast path
def test_adhoc_creates_valid_one_task_project(tmp_path, ateam):
    root = tmp_path / "p"
    (root / ".opencode").mkdir(parents=True)
    code, out, err = ateam(root, "work", "adhoc", "Fix the typo in README.md\nand the title", "--json")
    assert code == 0, err
    info = json.loads(out)
    assert info["task"] == "TASK-001" and info["project"].startswith("adhoc-")
    code, out, _ = ateam(root, "work", "validate", info["project"])
    assert code == 0, out
    task = (root / ".ateam/work" / info["project"] / "tasks/001-request.md").read_text()
    assert "> Fix the typo in README.md" in task and "[manual]" in task
    assert "adhoc: true" in task


def test_adhoc_uses_detected_quality_profile(tmp_path, ateam):
    root = tmp_path / "p"
    root.mkdir()
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    code, out, _ = ateam(root, "work", "adhoc", "add a thing", "--json")
    info = json.loads(out)
    task = (root / ".ateam/work" / info["project"] / "tasks/001-request.md").read_text()
    assert "quality: [test]" in task
    assert "[check: test]" in task
    assert ateam(root, "work", "validate", info["project"])[0] == 0


def test_adhoc_rejects_empty_request(tmp_path, ateam):
    code, _, err = ateam(tmp_path, "work", "adhoc", "   ")
    assert code != 0 and "empty" in err


# ------------------------------------------------------------------ plan checks
def test_empty_plan_template_is_rejected(project, ateam):
    ateam(project, "work", "init", "fresh")
    code, out, _ = ateam(project, "work", "validate", "fresh")
    assert code == 1
    assert "plan section '# Objective' is empty" in out


def test_missing_plan_section_is_rejected():
    errs = schemas.validate_plan("# Objective\nx\n")
    assert any("Task Graph" in e for e in errs)


def test_oversized_task_is_an_error(project, ateam):
    tp = project / ".ateam/work/notes-api/tasks/002-domain-model.md"
    tp.write_text(tp.read_text() + "\n# Context\n" + ("word " * 3000))
    ateam(project, "work", "sync", "notes-api")
    code, out, _ = ateam(project, "work", "validate", "notes-api")
    assert code == 1 and "hard limit" in out


def test_quality_gap_warning_without_gate_task(tmp_path, ateam):
    root = tmp_path / "p"
    root.mkdir()
    ateam(root, "work", "init", "x")
    (root / ".ateam/work/x/plan.md").write_text(
        "# Objective\na\n\n# Scope and Non-Goals\na\n\n# Architectural Decisions\na\n\n# Shared Constraints\na\n\n# Task Graph\na\n\n# Acceptance Criteria\na\n\n# Progress\na\n"
    )
    ateam(root, "work", "new-task", "x", "Do it")
    tp = root / ".ateam/work/x/tasks/001-do-it.md"
    tp.write_text(
        tp.read_text()
        .replace("<observable outcome>", "works")
        .replace("<deterministic command>", "python3 -c 'import sys; sys.exit(0 if 1 else 1)'")
        .replace("# Objective\n", "# Objective\nDo it\n")
        .replace("# Files to Inspect\n", "# Files to Inspect\n- a.py\n")
    )
    ateam(root, "work", "sync", "x")
    code, out, _ = ateam(root, "work", "validate", "x")
    assert code == 0, out
    assert "no earlier touches_gate task establishes it" in out


def test_golden_project_has_no_quality_gap_warning_with_gate_task(project, ateam):
    code, out, _ = ateam(project, "work", "validate", "notes-api")
    assert code == 0
    assert "TASK-002: needs quality" not in out


# ---------------------------------------------------------------------- aliases
def test_aliases_are_generated_and_current():
    assert A.sync_aliases(REPO, check=True) == [], "run: python3 .opencode/scripts/ateam.py agents sync-aliases"


def test_alias_text_marks_hidden_and_keeps_permissions():
    src = "---\ndescription: X\nmode: subagent\nmodel: a/b\npermissions:\n  - action: edit\n    resource: '*'\n    effect: deny\n---\nbody\n"
    out = A.alias_text(src, "t")
    assert "hidden: true" in out and "Legacy alias of `t`" in out
    assert out.split("---\n")[1].count("action: edit") == 1 and out.endswith("body\n")


# ------------------------------------------------------------------ resolved agent
@needs_opencode
def test_architect_resolved_permissions_and_alias_parity():
    res = resolved_agents()
    by = {a["id"]: a for a in res}
    arch = by["architect"]
    assert L.evaluate(arch["permissions"], "edit", ".ateam/work/p/tasks/002-y.md") == "allow"
    assert L.evaluate(arch["permissions"], "edit", "src/app.py") == "deny"
    assert L.evaluate(arch["permissions"], "shell", L.ATEAM + " work validate p") == "allow"
    assert L.evaluate(arch["permissions"], "shell", "ls") == "deny"
    assert by["architecte"]["hidden"] is True
    assert L._norm_perms(by["architecte"]["permissions"]) == L._norm_perms(arch["permissions"])
    errs = L.lint_agents(res, strict_required=False)
    assert not [e for e in errs if e.startswith("architect")], errs
