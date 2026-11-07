import json
import os
import subprocess
import sys

import pytest

from ateam import feedback as F
from ateam import frontmatter as fm
from ateam import knowledge as K
from ateam import schemas
from conftest import SCRIPTS
from driver_fixture import APPROVE_ALL, FUNCS, Fixture, impl_rule

RUN = "RUN-20261107-001"


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "p"
    (r / ".opencode").mkdir(parents=True)
    # templates are read from the installed boilerplate (common.TEMPLATES_DIR)
    return r


def note(ateam, root, *extra, env=None, sig="missing-runner", evidence="logs/x.txt", problem="pytest is not installed", task="TASK-001"):
    args = ["feedback", "note", "--category", "tooling-gap", "--signature", sig, "--problem", problem, "--json"]
    if evidence is not None:
        args += ["--evidence", evidence]
    if task:
        args += ["--task", task]
    return ateam(root, *args, *extra, env={"ATEAM_RUN_ID": RUN, "ATEAM_AGENT": "coder", **(env or {})})


def test_note_creates_valid_event_and_index(root, ateam):
    code, out, err = note(ateam, root)
    assert code == 0, err
    info = json.loads(out)
    assert info["id"] == "FB-000001" and info["path"].endswith("events/RUN-20261107-001/coder-FB-000001.md")
    meta, body = fm.parse_file(root / info["path"])
    assert schemas.validate_feedback(meta, body) == []
    assert meta["source"] == "agent" and meta["status"] == "open" and meta["task_id"] == "TASK-001"
    assert ateam(root, "feedback", "index", "--json")[0] == 0
    idx = json.loads((root / ".ateam/feedback/index.json").read_text())
    assert idx["count"] == 1 and idx["events"][0]["signature"] == "missing-runner"


def test_note_without_problem_is_rejected(root, ateam):
    code, _, err = note(ateam, root, problem="   ")
    assert code != 0 and "Problem is empty" in err
    assert not (root / ".ateam/feedback/events").exists() or not list((root / ".ateam/feedback/events").glob("*/*.md"))


def test_note_without_evidence_is_rejected(root, ateam):
    code, _, err = note(ateam, root, evidence=None)
    assert code != 0
    code, _, err = note(ateam, root, evidence="  ")
    assert code != 0 and "evidence" in err


@pytest.mark.parametrize("secret", ["my key is sk-abcdefghijklmnopqrstuvwxyz123456", "password = hunter2hunter2", "AKIAABCDEFGHIJKLMNOP", "-----BEGIN RSA PRIVATE KEY-----"])
def test_note_with_secret_is_rejected(root, ateam, secret):
    code, _, err = note(ateam, root, problem=secret)
    assert code != 0 and "secret" in err


def test_secret_in_evidence_is_rejected(root, ateam):
    code, _, err = note(ateam, root, evidence="curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789'")
    assert code != 0


def test_notes_are_capped_at_two_per_task(root, ateam):
    assert note(ateam, root, sig="a-one")[0] == 0
    assert note(ateam, root, sig="a-two")[0] == 0
    code, _, err = note(ateam, root, sig="a-three")
    assert code != 0 and "at most 2" in err
    assert note(ateam, root, sig="b-one", task="TASK-002")[0] == 0  # other task unaffected


def test_twenty_concurrent_notes_have_unique_ids(root, ateam):
    procs = []
    for i in range(20):
        env = dict(os.environ, ATEAM_RUN_ID=RUN, ATEAM_AGENT="coder")
        env.pop("ATEAM_ROOT", None)
        procs.append(
            subprocess.Popen(
                [sys.executable, str(SCRIPTS / "ateam.py"), "--root", str(root), "feedback", "note", "--category", "environment", "--signature", f"sig-{i % 4}",
                 "--problem", f"problem {i}", "--evidence", "x.log", "--task", f"TASK-{i + 1:03d}", "--json"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            )
        )
    ids = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        ids.append(json.loads(out)["id"])
    assert len(set(ids)) == 20
    files = list((root / ".ateam/feedback/events").glob("*/*.md"))
    assert len(files) == 20
    data = F.rebuild_index(root)
    assert data["count"] == 20 and data["errors"] == []


def test_query_open_and_group(root, ateam):
    for i in range(3):
        note(ateam, root, sig="missing-runner", task=f"TASK-00{i + 1}")
    note(ateam, root, sig="slow-tests", task="TASK-004")
    code, out, _ = ateam(root, "feedback", "query", "--open", "--group", "--json")
    groups = json.loads(out)
    assert groups[0]["signature"] == "missing-runner" and groups[0]["count"] == 3
    assert groups[0]["ids"] == ["FB-000001", "FB-000002", "FB-000003"] and groups[1]["signature"] == "slow-tests"
    code, out, _ = ateam(root, "feedback", "query", "--task", "TASK-004", "--json")
    assert [e["signature"] for e in json.loads(out)] == ["slow-tests"]
    ateam(root, "feedback", "mark", "FB-000001", "addressed", "--by", "PROP-001")
    code, out, _ = ateam(root, "feedback", "query", "--open", "--group", "--json")
    assert json.loads(out)[0]["count"] == 2


def test_invalid_event_is_reported_by_index(root, ateam):
    note(ateam, root)
    bad = root / ".ateam/feedback/events/RUN-20261107-001/coder-FB-000099.md"
    bad.write_text("---\nid: FB-000099\n---\n")
    code, out, _ = ateam(root, "feedback", "index", "--json")
    assert code == 1 and "FB-000099" in out


def test_signal_events_dedupe_and_map(root):
    fid = F.record_signal(root, RUN, "timeout", "TASK-002", "coder exceeded 600s")
    assert fid == "FB-000001"
    assert F.record_signal(root, RUN, "timeout", "TASK-002", "again") is None  # same run, kind, task
    assert F.record_signal(root, RUN, "timeout", "TASK-003", "other task") == "FB-000002"
    assert F.record_signal(root, RUN, "gate_failed", "TASK-002", "x") is None  # not a captured kind
    ev = F.read_events(root)[0]
    assert {e["source"] for e in ev} == {"signal"} and ev[0]["agent"] == "driver" and ev[0]["category"] == "context-overload"


def test_signal_with_secret_detail_is_sanitized(root):
    F.record_signal(root, RUN, "role_error", "TASK-001", "failed with token = abcdefghijklmnop123")
    text = next((root / ".ateam/feedback/events").glob("*/*.md")).read_text()
    assert "abcdefghijklmnop123" not in text


def test_knowledge_note_appends_and_validates(root, ateam):
    code, out, err = ateam(root, "knowledge", "note", "Tests run with python3 -m unittest, not pytest", "--source", "TASK-001 report", "--json")
    assert code == 0, err
    p = root / json.loads(out)["inbox"]
    assert "unittest" in p.read_text() and "(source: TASK-001 report)" in p.read_text()
    ateam(root, "knowledge", "note", "Second fact", "--source", "user")
    assert len(p.read_text().strip().splitlines()) == 2
    assert ateam(root, "knowledge", "note", "", "--source", "x")[0] != 0
    assert ateam(root, "knowledge", "note", "fact", "--source", "")[0] != 0
    assert ateam(root, "knowledge", "note", "api_key = abcdefghijklmnop12345", "--source", "x")[0] != 0
    assert ateam(root, "knowledge", "note", "x\n" * 30, "--source", "x")[0] != 0


# ------------------------------------------------- driver integration (Scenario D)
def test_driver_writes_signals_and_run_summary(tmp_path):
    fx = Fixture(tmp_path)
    fx.script([impl_rule(1, "add", "0")] + APPROVE_ALL)  # same failure twice -> needs_architect
    code, out, err = fx.run()
    assert code == 11
    ev, errors = F.read_events(fx.root)
    assert errors == []
    sigs = {e["signature"] for e in ev}
    assert {"needs-architect", "repeated-gate-failure"} <= sigs
    assert all(e["source"] == "signal" and e["run"] == fx.last_run()["id"] for e in ev)
    summary = json.loads((fx.root / ".ateam/runs" / fx.last_run()["id"] / "summary.json").read_text())
    assert summary["failed"] == ["TASK-001"] and summary["completed"] == []
    assert summary["metrics"]["attempts"] == 2 and "needs-architect" in summary["events_this_run"]
    assert (fx.root / ".ateam/runs" / fx.last_run()["id"] / "summary.md").exists()
    assert (fx.root / ".ateam/feedback/index.json").exists()


def test_driver_passes_run_context_env_and_clean_run_captures_no_events(tmp_path):
    fx = Fixture(tmp_path)
    fx.script([impl_rule(i, fn, expr) for i, (fn, _, expr, _) in enumerate(FUNCS, start=1)] + APPROVE_ALL)
    assert fx.run()[0] == 0
    first = fx.calls("coder")[0]["ateam_env"]
    assert first["ATEAM_RUN_ID"] == fx.last_run()["id"] and first["ATEAM_TASK"] == "TASK-001" and first["ATEAM_AGENT"] == "coder"
    assert "ATEAM_TASK" not in {k for k, v in fx.calls("tester")[0]["ateam_env"].items() if v}
    ev, _ = F.read_events(fx.root)
    assert ev == []  # a clean run captures no noise
