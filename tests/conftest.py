import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".opencode" / "scripts"
sys.path.insert(0, str(SCRIPTS))

GOLDEN_PROJECT = REPO / "tests" / "fixtures" / "golden-project"


def pytest_configure(config):
    config.addinivalue_line("markers", "llm: end-to-end tests that call a real model (opt-in: pytest -m llm)")


def pytest_collection_modifyitems(config, items):
    if "llm" in (config.getoption("-m") or ""):
        return
    skip = pytest.mark.skip(reason="LLM e2e test: run with `pytest -m llm`")
    for item in items:
        if "llm" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def project(tmp_path):
    """A writable copy of the golden project root (with .opencode/skills fixture)."""
    dst = tmp_path / "proj"
    shutil.copytree(GOLDEN_PROJECT, dst)
    return dst


@pytest.fixture
def ateam():
    """Run the CLI in-process-ish via subprocess; returns (code, stdout, stderr)."""

    def run(root, *args, env=None):
        import os

        e = dict(os.environ)
        e.pop("ATEAM_ROOT", None)
        if env:
            e.update(env)
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "ateam.py"), "--root", str(root), *args],
            capture_output=True,
            text=True,
            env=e,
        )
        return p.returncode, p.stdout, p.stderr

    return run


FILLED_PLAN = (
    "# Objective\nTest objective.\n\n# Scope and Non-Goals\nTest scope.\n\n# Architectural Decisions\nNone.\n\n"
    "# Shared Constraints\nNone.\n\n# Task Graph\nTASK-001\n\n# Acceptance Criteria\nGate passes.\n\n# Progress\n0/1 completed.\n"
)


def fill_plan(root, project):
    (root / ".ateam" / "work" / project / "plan.md").write_text(FILLED_PLAN)
