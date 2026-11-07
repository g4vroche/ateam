import pytest

from e2e.llm_helpers import make_workspace, run_agent

pytestmark = pytest.mark.llm

REQUEST = (
    "Project id: notes-cli. Build a small Python 3 command-line notes manager (stdlib only): add, list, delete and search "
    "notes, stored in a JSON file, with unit tests. Decompose it for the team."
)


def test_architect_produces_valid_project(tmp_path, ateam):
    ws = make_workspace(tmp_path)
    res = run_agent(ws, "architect", REQUEST, timeout=1200)
    assert res["code"] == 0
    code, out, _ = ateam(ws, "work", "validate", "notes-cli")
    assert code == 0, out
    code, out, _ = ateam(ws, "work", "status", "notes-cli", "--json")
    assert sum(__import__("json").loads(out)["counts"].values()) >= 3
