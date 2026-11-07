"""Resolved-agent checks (need `opencode` on PATH; skipped otherwise)."""
import json
import os
import shutil
import subprocess

import pytest

from ateam import lint as L
from conftest import REPO

needs_opencode = pytest.mark.skipif(shutil.which("opencode") is None, reason="opencode not on PATH")


@pytest.fixture(scope="module")
def resolved():
    out = subprocess.run(["opencode", "debug", "agents"], capture_output=True, text=True, cwd=REPO, env=dict(os.environ, PWD=str(REPO)))
    return json.loads(out.stdout)


@needs_opencode
def test_contract_holds_for_all_resolved_agents(resolved):
    assert L.lint_agents(resolved) == []


@needs_opencode
def test_coder_cannot_touch_its_own_task_gate_or_scripts(resolved):
    coder = next(a for a in resolved if a["id"] == "coder")["permissions"]
    for path in (".ateam/work/p/tasks/001-x.md", ".ateam/work/p/manifest.json", ".ateam/quality.json", ".ateam/work/p/state/TASK-001.gate.json", ".opencode/scripts/ateam/gate.py", ".opencode/agents/coder.md"):
        assert L.evaluate(coder, "edit", path) == "deny", path
    assert L.evaluate(coder, "edit", ".ateam/work/p/reports/001-x.md") == "allow"
    for cmd in ("work complete p TASK-001", "work gate p TASK-001", "run p", "work start p TASK-001", "work fail p TASK-001 --reason x"):
        assert L.evaluate(coder, "shell", f"python3 .opencode/scripts/ateam.py {cmd}") == "deny", cmd
    assert L.evaluate(coder, "read", ".ateam/work/p/plan.md") == "deny"


@needs_opencode
def test_chef_has_no_edit_and_only_narrow_shell(resolved):
    chef = next(a for a in resolved if a["id"] == "chef")["permissions"]
    assert L.evaluate(chef, "edit", "src/a.py") == "deny"
    assert L.evaluate(chef, "shell", "ls") == "deny"
    assert L.evaluate(chef, "shell", "python3 .opencode/scripts/ateam.py run p") == "allow"
    assert L.evaluate(chef, "subagent", "dev") == "deny"


@needs_opencode
def test_legacy_aliases_are_hidden_and_identical(resolved):
    by = {a["id"]: a for a in resolved}
    for alias, target in L.ALIASES.items():
        assert by[alias]["hidden"] is True
        assert L._norm_perms(by[alias]["permissions"]) == L._norm_perms(by[target]["permissions"])
        assert by[alias]["model"] == by[target]["model"]


@needs_opencode
def test_models_unchanged_from_v0(resolved):
    golden = {a["id"]: a for a in json.load(open(REPO / "tests/golden/agents-v0.json"))}
    by = {a["id"]: a for a in resolved}
    pairs = {"chef": "chef", "architect": "architecte", "coder": "dev", "tester": "testeur", "tech-lead": "tech-lead"}
    for new, old in pairs.items():
        assert by[new]["model"] == golden[old]["model"], new
