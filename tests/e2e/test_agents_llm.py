"""Opt-in end-to-end checks with a real model (`pytest -m llm`). One scenario per agent; each is slow, run them one at a time."""
import json

import pytest

from ateam import feedback as F
from e2e.llm_helpers import make_workspace, run_agent

pytestmark = pytest.mark.llm

HELLO = "Create hello.py that prints 'hello world' when run, and tests/test_hello.py with one unittest that checks the output. Use only the standard library."


def test_chef_fast_path_runs_driver_coder_and_reviewer(tmp_path, ateam):
    """chef -> driver (adhoc) -> coder -> tech-lead. The reply to a French request must be in French."""
    ws = make_workspace(tmp_path)
    res = run_agent(ws, "chef", "Crée hello.py qui affiche 'hello world', avec un test unitaire dans tests/test_hello.py (bibliothèque standard uniquement).", timeout=1800)
    assert res["code"] == 0
    assert (ws / "hello.py").exists()
    runs = sorted((ws / ".ateam" / "runs").glob("RUN-*/summary.json"))
    assert runs and json.loads(runs[-1].read_text())["completed"]
    french = ("créé", "crée", "fichier", "tests", "terminé", "le ", "la ", "les ", "est ", "été")
    assert sum(w in res["text"].lower() for w in french) >= 3, res["text"][:400]
    assert "the task was completed" not in res["text"].lower()


def test_tester_writes_findings_with_verdict(tmp_path, ateam):
    ws = make_workspace(tmp_path, {"hello.py": "print('hello world')\n"})
    findings = ws / ".ateam/work/p/reviews/exploratory-r1.md"
    findings.parent.mkdir(parents=True)
    res = run_agent(ws, "tester", f"Test the project once. Findings file: {findings.relative_to(ws)}. Scripts folder: .ateam/work/p/reviews/exploratory/. The program is hello.py (prints a greeting).", timeout=900)
    assert res["code"] == 0 and findings.exists()
    assert findings.read_text().strip().splitlines()[-1].startswith("VERDICT: ")


def test_skillsmith_writes_a_valid_proposal_only_under_proposals(tmp_path, ateam):
    ws = make_workspace(tmp_path)
    (ws / ".ateam").mkdir(exist_ok=True)
    (ws / ".ateam/config.json").write_text(json.dumps({"learning_enabled": True}))
    for i, run in enumerate(("RUN-20261107-001", "RUN-20261107-002"), start=1):
        F.add_note(ws, category="tooling-gap", severity="medium", signature="unittest-needs-package-init", problem="`unittest discover -t .` fails until tests/__init__.py exists; cost 4 failed commands",
                   evidence=[".ateam/runs/x.log"], task=f"TASK-00{i}", run_id=run, agent="coder", context="", workaround="touch tests/__init__.py", improvement="document or auto-create it")
    def snapshot():
        return {p for p in ws.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts}

    before = snapshot()
    res = run_agent(ws, "skillsmith", "Review the open feedback and write at most one proposal.", timeout=1200)
    assert res["code"] == 0
    changed = snapshot() - before
    assert all(".ateam/proposals/" in str(p.relative_to(ws)) or ".ateam/feedback/" in str(p.relative_to(ws)) for p in changed), changed
    props = sorted((ws / ".ateam/proposals").glob("PROP-*"))
    if props:  # an agent may legitimately decide nothing recurs enough; if it wrote one, it must validate
        assert ateam(ws, "improve", "validate", props[0].name)[0] == 0


def test_dreamer_files_knowledge_through_upsert(tmp_path, ateam):
    ws = make_workspace(tmp_path)
    (ws / ".ateam").mkdir(exist_ok=True)
    (ws / ".ateam/config.json").write_text(json.dumps({"learning_enabled": True}))
    ateam(ws, "knowledge", "note", "Tests are run with `bash tests/run.sh`; it bootstraps pytest into .pytest-lib.", "--source", "user")
    assert ateam(ws, "knowledge", "dream", "--json")[0] == 0
    assert ateam(ws, "knowledge", "lint")[0] == 0
    code, out, _ = ateam(ws, "knowledge", "dream", "--json")
    assert json.loads(out)["status"] == "nothing-new"
