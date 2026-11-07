import json
import os
import signal
import time

import pytest

from ateam import bundle as B
from ateam import lifecycle as L
from ateam import work as W
from driver_fixture import APPROVE_ALL, FUNCS, Fixture, impl_rule


def coder_rules():
    return [impl_rule(i, fn, expr) for i, (fn, _, expr, _) in enumerate(FUNCS, start=1)]


@pytest.fixture
def fx(tmp_path):
    return Fixture(tmp_path)


def test_three_task_project_completes_with_passed_gates(fx):
    fx.script(coder_rules() + APPROVE_ALL)
    code, out, err = fx.run()
    assert code == 0, err
    assert fx.statuses() == ["completed"] * 3
    last = json.loads(out.strip().splitlines()[-1])
    assert last["status"] == "completed" and last["completed"] == "3/3"
    for i in (1, 2, 3):
        gate = json.loads((fx.pdir() / "state" / f"TASK-{i:03d}.gate.json").read_text())
        assert gate["overall"] == "passed"
    # roles: 3 coders, one final review, one tester; no per-task review (review: none)
    assert [len(fx.calls(a)) for a in ("coder", "tech-lead", "tester")] == [3, 1, 1]
    rec = fx.last_run()
    assert rec["status"] == "completed" and rec["totals"]["role_calls"] == 5 and rec["totals"]["tokens"]["output"] == 100
    assert rec["totals"]["attempts"] == 3
    assert fx.ateam("work", "validate", "calc")[0] == 0
    report = (fx.pdir() / "reports" / "001-add.md").read_text()
    assert "status: completed" in report and "Implemented add." in report and "- A calc.py" not in report and "M calc.py" in report


def test_per_task_review_runs_only_for_review_required(tmp_path):
    fx = Fixture(tmp_path, reviews={2: "required"})
    fx.script(coder_rules() + APPROVE_ALL)
    assert fx.run()[0] == 0
    # one per-task review (TASK-002) + final review
    assert len(fx.calls("tech-lead")) == 2
    assert "Review TASK-002" in fx.calls("tech-lead")[0]["prompt"]


def test_roles_run_with_project_pwd(fx):
    fx.script(coder_rules() + APPROVE_ALL)
    fx.run()
    for c in fx.calls():
        assert c["pwd"] == str(fx.root) and os.path.realpath(c["cwd"]) == os.path.realpath(fx.root)


def test_failed_gate_retries_then_succeeds_with_failure_summary(fx):
    rules = [
        impl_rule(1, "add", "0", times=1),  # wrong implementation first
        impl_rule(1, "add", "a + b"),
        impl_rule(2, "sub", "a - b"),
        impl_rule(3, "mul", "a * b"),
    ] + APPROVE_ALL
    fx.script(rules)
    code, out, err = fx.run()
    assert code == 0, err
    assert fx.state_json("TASK-001")["attempts"] == 2
    coders = fx.calls("coder")
    assert "state/TASK-001.failure.md" in coders[1]["prompt"] and "state/TASK-001.failure.md" not in coders[0]["prompt"]


def test_same_failure_twice_escalates_after_exactly_two_attempts(fx):
    fx.script([impl_rule(1, "add", "0")] + APPROVE_ALL)
    code, out, err = fx.run()
    assert code == 11, err
    assert len(fx.calls("coder")) == 2
    assert fx.status("TASK-001") == "needs_architect"
    esc = json.loads((fx.pdir() / "state" / "escalation.json").read_text())
    assert esc["kind"] == "needs_architect" and esc["task"] == "TASK-001" and "split" in esc["hint"]
    assert json.loads(out.strip().splitlines()[-1])["status"] == "needs_architect"


def test_attempts_bound_even_with_distinct_signatures(fx):
    # Three different failures (missing function, wrong result, failing extra test): no signature repeats,
    # so only max_attempts stops the loop, and the decision is `blocked`, not needs_architect.
    rules = [
        {"agent": "coder", "match": "tasks/001-", "write": {"notes.txt": "a"}, "report": "x", "times": 1},
        {"agent": "coder", "match": "tasks/001-", "write": {"calc.py": "def add(a, b):\n    return 0\n"}, "report": "x", "times": 1},
        {
            "agent": "coder",
            "match": "tasks/001-",
            "write": {
                "calc.py": "def add(a, b):\n    return a + b\n",
                "tests/test_zzz.py": "import unittest\n\n\nclass Z(unittest.TestCase):\n    def test_z(self):\n        self.fail('z')\n",
            },
            "report": "x",
            "times": 1,
        },
    ] + APPROVE_ALL
    fx.script(rules)
    code, out, err = fx.run()
    assert code == 10, err
    assert len(fx.calls("coder")) == 3
    assert fx.status("TASK-001") == "blocked"
    sigs = [h["signature"] for h in fx.state_json("TASK-001")["history"] if h["result"] == "failed"]
    assert len(set(sigs)) == 3
    assert json.loads((fx.pdir() / "state" / "escalation.json").read_text())["kind"] == "blocked"


def test_timeout_counts_as_attempt_and_repeated_timeout_escalates(tmp_path):
    fx = Fixture(tmp_path, config={"task_timeout_s": 1})
    fx.script([{"agent": "coder", "match": "tasks/001-", "sleep": 30}] + APPROVE_ALL)
    code, out, err = fx.run()
    assert code == 11, err
    assert len(fx.calls("coder")) == 2
    hist = fx.state_json("TASK-001")["history"]
    assert [h["reason"] for h in hist if h["result"] == "failed"] == ["timeout", "timeout"]
    kinds = [s["kind"] for s in fx.last_run()["signals"]]
    assert "timeout" in kinds and "needs_architect" in kinds


def test_missing_report_counts_as_attempt(fx):
    rules = [
        {**impl_rule(1, "add", "a + b"), "report": None, "times": 1},
        impl_rule(1, "add", "a + b"),
        impl_rule(2, "sub", "a - b"),
        impl_rule(3, "mul", "a * b"),
    ] + APPROVE_ALL
    rules[0].pop("report")
    fx.script(rules)
    code, out, err = fx.run()
    assert code == 0, err
    hist = fx.state_json("TASK-001")["history"]
    assert any(h.get("reason") == "partial_report" for h in hist)
    assert fx.state_json("TASK-001")["attempts"] == 2


def test_provider_error_stops_without_burning_an_attempt(fx):
    fx.script([{"agent": "coder", "error": {"type": "provider.free-limit", "message": "Free usage limit reached", "status": 429, "retryAfterMs": 99999999}}])
    code, out, err = fx.run()
    assert code == 12
    assert fx.status("TASK-001") == "in_progress" and fx.state_json("TASK-001")["attempts"] == 0
    assert json.loads((fx.pdir() / "state" / "escalation.json").read_text())["kind"] == "provider_unavailable"


def test_task_review_rework_then_approved(tmp_path):
    fx = Fixture(tmp_path, reviews={1: "required"})
    rules = [
        {"agent": "tech-lead", "match": "Review TASK-001", "verdict": "CHANGES_REQUESTED", "findings": "- file: calc.py\n  Task: TASK-001", "times": 1},
        impl_rule(1, "add", "a + b"),
        impl_rule(2, "sub", "a - b"),
        impl_rule(3, "mul", "a * b"),
    ] + APPROVE_ALL
    fx.script(rules)
    code, out, err = fx.run()
    assert code == 0, err
    coders = fx.calls("coder")
    assert len(coders) == 4  # TASK-001 twice (rework), TASK-002, TASK-003
    assert "reviews/TASK-001-r1.md" in coders[1]["prompt"]
    assert fx.state_json("TASK-001")["review_rounds"] == 2


def test_review_rounds_are_capped_at_three(tmp_path):
    fx = Fixture(tmp_path, reviews={1: "required"})
    fx.script([{"agent": "tech-lead", "match": "Review TASK-001", "verdict": "CHANGES_REQUESTED"}, impl_rule(1, "add", "a + b")])
    code, out, err = fx.run()
    assert code == 10, err
    assert len(fx.calls("tech-lead")) == 3
    assert json.loads((fx.pdir() / "state" / "escalation.json").read_text())["kind"] == "review_exhausted"


def test_final_review_reopens_owning_task(fx):
    rules = coder_rules()
    rules.insert(0, {"agent": "tech-lead", "match": "final diff", "verdict": "CHANGES_REQUESTED", "findings": "- Task: TASK-002 handle edge case", "times": 1})
    fx.script(rules + APPROVE_ALL)
    code, out, err = fx.run()
    assert code == 0, err
    assert fx.statuses() == ["completed"] * 3
    assert fx.state_json("TASK-002")["reopened"] == 1
    assert len(fx.calls("coder")) == 4 and len(fx.calls("tech-lead")) == 2


def test_test_guard_needs_review_and_reviewer_override(tmp_path):
    fx = Fixture(tmp_path)
    # TASK-001 deletes the pre-existing test file and adds its own: guard fires, reviewer approves
    rules = [
        {**impl_rule(1, "add", "a + b"), "delete": ["tests/test_base.py"]},
        impl_rule(2, "sub", "a - b"),
        impl_rule(3, "mul", "a * b"),
        {"agent": "tech-lead", "match": "Gate guard findings", "verdict": "APPROVED"},
    ] + APPROVE_ALL
    fx.script(rules)
    code, out, err = fx.run()
    assert code == 0, err
    assert any("test file deleted" in p["prompt"] for p in fx.calls("tech-lead"))
    assert fx.state_json("TASK-001")["history"][-1]["reviewed_override"] is True


def test_adhoc_fast_path_end_to_end(tmp_path):
    fx = Fixture(tmp_path)
    fx.script([{"agent": "coder", "write": {"hello.txt": "hi"}, "report": "created hello"}, {"agent": "tech-lead", "verdict": "APPROVED"}])
    code, out, err = fx.ateam("run", "--adhoc", "Create hello.txt containing hi")
    assert code == 0, err
    assert len(fx.calls("architect")) == 0 and len(fx.calls("coder")) == 1 and len(fx.calls("tester")) == 0
    assert len(fx.calls("tech-lead")) == 1  # final review also judges the manual criterion
    assert (fx.root / "hello.txt").exists()
    assert fx.ateam("work", "validate", json.loads(out.strip().splitlines()[-1])["project"])[0] == 0


def test_run_lock_prevents_concurrent_runs(tmp_path):
    fx = Fixture(tmp_path)
    ann = tmp_path / "started"
    fx.script([{"agent": "coder", "match": "tasks/001-", "sleep": 8, "announce": str(ann)}])
    p = fx.popen("run", "calc")
    fx.wait_for(ann)
    code, out, err = fx.run()
    assert code == 3 and "another run is active" in err
    p.send_signal(signal.SIGTERM)
    p.communicate(timeout=30)


# ---------------------------------------------------------------- Scenario G
def test_kill_driver_mid_task_and_resume_does_not_rerun_completed_tasks(tmp_path):
    fx = Fixture(tmp_path)
    ann = tmp_path / "t2-started"
    slow = {**impl_rule(2, "sub", "a - b"), "sleep": 60, "announce": str(ann), "times": 1}
    fx.script([impl_rule(1, "add", "a + b"), slow, impl_rule(2, "sub", "a - b"), impl_rule(3, "mul", "a * b")] + APPROVE_ALL)
    p = fx.popen("run", "calc")
    fx.wait_for(ann)
    assert fx.status("TASK-001") == "completed" and fx.status("TASK-002") == "in_progress"
    role_pid = fx.calls("coder")[-1]["pid"]
    os.kill(p.pid, signal.SIGKILL)
    p.communicate(timeout=30)
    # the role process must die with the driver (PR_SET_PDEATHSIG), otherwise it would keep editing files
    end = time.time() + 15
    while time.time() < end:
        try:
            os.kill(role_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("role process survived the driver")
    before = len(fx.calls("coder"))
    code, out, err = fx.run("--resume")
    assert code == 0, err
    coders = fx.calls("coder")[before:]
    assert [("tasks/001-" in c["prompt"]) for c in coders] == [False, False]  # TASK-001 not re-run
    assert fx.statuses() == ["completed"] * 3
    assert fx.state_json("TASK-001")["attempts"] == 1
    assert fx.state_json("TASK-002")["attempts"] == 1  # resumed attempt was not counted twice
    runs = sorted((fx.root / ".ateam" / "runs").glob("RUN-*.json"))
    first = json.loads(runs[0].read_text())
    assert first["status"] == "interrupted"


def test_resume_reuses_gate_only_when_fingerprint_matches(tmp_path):
    fx = Fixture(tmp_path)
    fx.script(coder_rules() + APPROVE_ALL)
    # simulate a crash after the gate passed: start + implement + gate, then no complete
    fx.ateam("work", "start", "calc", "TASK-001", check=True)
    (fx.root / "calc.py").write_text('"""calc"""\n\ndef add(a, b):\n    return a + b\n')
    (fx.root / "tests" / "test_add.py").write_text("import unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(3, 2), 5)\n")
    assert fx.ateam("work", "gate", "calc", "TASK-001")[0] == 0
    rp = fx.pdir() / "reports" / "001-add.md"
    rp.parent.mkdir(exist_ok=True)
    rp.write_text("---\ntask: TASK-001\nstatus: partial\nattempt: 1\n---\n\n# Files Changed\n- M calc.py\n\n# Decisions Made\nx\n\n# Verification Results\n- ok\n\n# Outputs for Dependent Tasks\n\n# Known Limitations\n")
    code, out, err = fx.run("--resume")
    assert code == 0, err
    assert not any("tasks/001-" in c["prompt"] for c in fx.calls("coder"))  # gate reused, no coder run

    # now a stale gate: tree changed after the gate -> the coder must run again
    fx2 = Fixture(tmp_path / "second")
    fx2.script(coder_rules() + APPROVE_ALL)
    fx2.ateam("work", "start", "calc", "TASK-001", check=True)
    (fx2.root / "calc.py").write_text('"""calc"""\n\ndef add(a, b):\n    return a + b\n')
    (fx2.root / "tests" / "test_add.py").write_text("import unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(3, 2), 5)\n")
    fx2.ateam("work", "gate", "calc", "TASK-001")
    (fx2.root / "calc.py").write_text('"""calc"""\n\ndef add(a, b):\n    return a + b  # edited after the gate\n')
    code, out, err = fx2.run("--resume")
    assert code == 0, err
    assert any("tasks/001-" in c["prompt"] for c in fx2.calls("coder"))


# ----------------------------------------------------------------- Scenario B
def test_bundle_contains_only_task_dependency_reports_scope_and_skills(fx):
    fx.script(coder_rules() + APPROVE_ALL)
    fx.run()
    root = fx.root
    tasks = W.task_index(W.load_tasks(fx.pdir()))
    t3 = tasks["TASK-003"]
    b = B.coder_bundle(root, "calc", t3, 1, 3)
    assert b.paths["task"].endswith("tasks/003-mul.md")
    assert b.paths["dependency_reports"] == [".ateam/work/calc/reports/002-sub.md"]
    for forbidden in ("plan.md", "manifest.json", "001-add", "002-sub.md"):
        assert forbidden not in b.prompt.replace("reports/002-sub.md", "")
    assert "tasks/001" not in b.prompt and "tasks/002" not in b.prompt
    assert len(b.prompt) < 1500
    assert b.paths["scope"] == ["calc.py", "tests/"]


def test_bundle_lists_skills_with_at_syntax(fx):
    p = fx.pdir() / "tasks" / "001-add.md"
    (fx.root / ".opencode" / "skills" / "quality-bootstrap").mkdir(parents=True)
    (fx.root / ".opencode" / "skills" / "quality-bootstrap" / "SKILL.md").write_text("---\nname: q\ndescription: Use when testing things that need it\n---\nx\n")
    p.write_text(p.read_text().replace("skills: []", "skills: [quality-bootstrap]"))
    t = W.task_index(W.load_tasks(fx.pdir()))["TASK-001"]
    b = B.coder_bundle(fx.root, "calc", t, 1, 3)
    assert "Skills: @quality-bootstrap" in b.prompt


# ------------------------------------------------------------ verdict parsing
@pytest.mark.parametrize(
    "text,want",
    [
        ("blah\nVERDICT: APPROVED\n", "APPROVED"),
        ("VERDICT: CHANGES_REQUESTED", "CHANGES_REQUESTED"),
        ("x\n**VERDICT: FAILED**\n", "FAILED"),
        ("VERDICT : VALIDÉ", "APPROVED"),
        ("VERDICT : À REFAIRE", "CHANGES_REQUESTED"),
        ("VERDICT : RECALÉ", "FAILED"),
        ("VERDICT: APPROVED\nmore\nVERDICT: CHANGES_REQUESTED", "CHANGES_REQUESTED"),
        ("no verdict here", None),
        ("VERDICT: MAYBE", None),
    ],
)
def test_parse_verdict(text, want):
    assert B.parse_verdict(text) == want


def test_review_task_refs():
    assert B.review_task_refs("- Task: TASK-002\n* **Task**: TASK-010 foo\nTask: TASK-002") == ["TASK-002", "TASK-010"]


# ------------------------------------------------------------ lifecycle unit
def test_next_split_reopen_commands(fx):
    code, out, _ = fx.ateam("work", "next", "calc", "--json")
    assert json.loads(out) == {"action": "run", "task": "TASK-001", "status": "pending"}
    fx.script(coder_rules() + APPROVE_ALL)
    assert fx.run()[0] == 0
    assert json.loads(fx.ateam("work", "next", "calc", "--json")[1]) == {"action": "done"}
    # completed tasks cannot be split
    assert fx.ateam("work", "split", "calc", "TASK-001")[0] != 0
    # reopen is the explicit exception
    assert fx.ateam("work", "reopen", "calc", "TASK-003", "--reason", "x")[0] == 0
    assert json.loads(fx.ateam("work", "next", "calc", "--json")[1])["task"] == "TASK-003"


def test_split_retires_pending_task_and_validate_flags_dependents(fx):
    code, out, err = fx.ateam("work", "split", "calc", "TASK-002")
    assert code == 0, err
    assert (fx.pdir() / "tasks" / "superseded" / "002-sub.md").exists()
    code, out, _ = fx.ateam("work", "validate", "calc")
    assert code == 1 and "dependency TASK-002 does not exist" in out


def test_completed_task_is_immutable(fx):
    fx.script(coder_rules() + APPROVE_ALL)
    fx.run()
    p = fx.pdir() / "tasks" / "001-add.md"
    p.write_text(p.read_text().replace("Implement add(a, b)", "Implement add(a, b) differently"))
    code, out, _ = fx.ateam("work", "validate", "calc")
    assert code == 1 and "immutable" in out


def test_fail_task_decisions(fx):
    fx.ateam("work", "start", "calc", "TASK-001", check=True)
    d = json.loads(fx.ateam("work", "fail", "calc", "TASK-001", "--reason", "r1", "--signature", "s1", "--json")[1])
    assert d["decision"] == "retry"
    fx.ateam("work", "start", "calc", "TASK-001", check=True)
    d = json.loads(fx.ateam("work", "fail", "calc", "TASK-001", "--reason", "r2", "--signature", "s1", "--json")[1])
    assert d["decision"] == "needs_architect" and d["repeat"] == 2


def test_migrate_legacy(tmp_path, ateam):
    root = tmp_path / "p"
    (root / "equipe" / "etapes").mkdir(parents=True)
    (root / "equipe" / "PLAN.md").write_text("# Plan\n\nBuild a thing that does stuff.\n\n## Etapes\n1. a\n")
    (root / "equipe" / "REVUE.md").write_text("Revue\nVERDICT : VALIDÉ\n")
    (root / "equipe" / "etapes" / "01.md").write_text("step one")
    code, out, err = ateam(root, "work", "migrate-legacy", "--json")
    assert code == 0, err
    pdir = root / ".ateam" / "work" / "legacy"
    assert (pdir / "legacy" / "PLAN.md").exists() and (pdir / "reviews" / "legacy-REVUE.md").exists() and (pdir / "legacy" / "etapes" / "01.md").exists()
    assert "Build a thing" in (pdir / "plan.md").read_text()
    assert (root / "equipe" / "PLAN.md").exists()  # copy by default
    assert ateam(root, "work", "validate", "legacy")[0] == 0
    assert ateam(root, "work", "migrate-legacy", "--json")[0] != 0  # refuses to overwrite


def test_status_command(fx):
    fx.script(coder_rules() + APPROVE_ALL)
    fx.run()
    code, out, _ = fx.ateam("status", "calc", "--json")
    d = json.loads(out)
    assert d["counts"] == {"completed": 3} and d["run"]["status"] == "completed" and d["next"] == {"action": "done"}


def test_quality_set_env_only_for_touches_gate_tasks(tmp_path):
    fx = Fixture(tmp_path)
    p = fx.pdir() / "tasks" / "002-sub.md"
    p.write_text(p.read_text().replace("review: none", "review: required").replace("quality: [test]", "quality: [test]\ntouches_gate: true"))
    fx.ateam("work", "sync", "calc", check=True)
    fx.script(coder_rules() + APPROVE_ALL)
    code, out, err = fx.run()
    assert code == 0, err
    envs = [c["allow_quality_set"] for c in fx.calls("coder")]
    assert envs == [None, "1", None]
