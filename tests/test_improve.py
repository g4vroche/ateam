"""Scenario E: improvement proposals and the approval policy."""
import json
import textwrap

import pytest

from ateam import frontmatter as fm

RUN = "RUN-20261107-001"

SKILL = textwrap.dedent(
    """\
    ---
    name: {name}
    description: {desc}
    metadata:
      ateam/lifecycle: proposed
    ---
    # {name}

    1. Do the thing.
    """
)

SECTIONS = """# Problem
Recurring friction.

# Existing Capabilities Checked
Searched skills and scripts; nothing covers it.

# Proposed Change
Add the thing.

# Validation
Draft tests pass.

# Rollback
Run `improve deprecate`.
"""


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "p"
    (r / ".opencode" / "skills").mkdir(parents=True)
    (r / ".ateam").mkdir()
    (r / ".ateam" / "config.json").write_text(json.dumps({"learning_enabled": True}))
    return r


def feedback(ateam, root, sig="slow-tests"):
    code, out, err = ateam(
        root, "feedback", "note", "--category", "tooling-gap", "--signature", sig, "--problem", "tests run slowly",
        "--evidence", "logs/x.txt", "--task", "TASK-001", "--json", env={"ATEAM_RUN_ID": RUN, "ATEAM_AGENT": "coder"},
    )
    assert code == 0, err
    return json.loads(out)["id"]


def new(ateam, root, ptype, title, fb, *extra):
    code, out, err = ateam(root, "improve", "new", "--type", ptype, "--title", title, "--feedback", fb, "--json", *extra)
    assert code == 0, err
    return json.loads(out)["id"]


def fill(root, pid):
    p = root / ".ateam/proposals" / pid / "proposal.md"
    meta, _ = fm.parse_file(p)
    p.write_text(fm.dump(meta, SECTIONS))


def write(root, pid, rel, text):
    f = root / ".ateam/proposals" / pid / "drafts" / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)


def status(root, pid):
    return fm.parse_file(root / ".ateam/proposals" / pid / "proposal.md")[0]["status"]


def skill_proposal(ateam, root, name="pytest-speedups", desc="Speed up slow pytest suites with selective runs. Use when the test gate exceeds its time budget."):
    fb = feedback(ateam, root)
    pid = new(ateam, root, "skill", "pytest speedups", fb)
    fill(root, pid)
    write(root, pid, f"skills/{name}/SKILL.md", SKILL.format(name=name, desc=desc))
    return pid, fb


def test_learning_is_locked_until_g1(tmp_path, ateam):
    r = tmp_path / "q"
    (r / ".opencode").mkdir(parents=True)
    fb = feedback(ateam, r)
    code, _, err = ateam(r, "improve", "new", "--type", "skill", "--title", "x", "--feedback", fb)
    assert code != 0 and "G1" in err


def test_new_requires_existing_feedback(root, ateam):
    code, _, err = ateam(root, "improve", "new", "--type", "skill", "--title", "x", "--feedback", "FB-000999")
    assert code != 0 and "unknown feedback" in err


def test_skill_stops_at_validated_until_approved(root, ateam):
    pid, fb = skill_proposal(ateam, root)
    assert ateam(root, "improve", "validate", pid)[0] == 0
    assert status(root, pid) == "validated"
    code, _, err = ateam(root, "improve", "promote", pid)
    assert code == 3 and "explicit approval" in err
    assert not (root / ".opencode/skills/pytest-speedups").exists()
    assert ateam(root, "improve", "approve", pid, "--by", "huge")[0] == 0
    code, out, err = ateam(root, "improve", "promote", pid, "--json")
    assert code == 0, err
    meta, _ = fm.parse_file(root / ".opencode/skills/pytest-speedups/SKILL.md")
    assert meta["metadata"]["ateam/lifecycle"] == "active"
    assert status(root, pid) == "active"
    # the linked feedback is closed and traceable
    idx = json.loads(ateam(root, "feedback", "query", "--json")[1])
    ev = next(e for e in (idx if isinstance(idx, list) else idx.get("events", [])) if e["id"] == fb)
    assert ev["status"] == "addressed"


def test_editing_a_draft_after_approval_is_revalidated(root, ateam):
    pid, _ = skill_proposal(ateam, root)
    ateam(root, "improve", "validate", pid)
    ateam(root, "improve", "approve", pid, "--by", "huge")
    write(root, pid, "skills/pytest-speedups/SKILL.md", SKILL.format(name="pytest-speedups", desc="x") )
    code, _, err = ateam(root, "improve", "promote", pid)
    assert code != 0 and not (root / ".opencode/skills/pytest-speedups").exists()


def test_skill_that_weakens_gates_fails_validation(root, ateam):
    fb = feedback(ateam, root)
    pid = new(ateam, root, "skill", "shortcuts", fb)
    fill(root, pid)
    bad = SKILL.format(name="shortcuts", desc="Faster delivery shortcuts for big tasks. Use when a task is large.") + "\nBypass the gate when it is slow.\n"
    write(root, pid, "skills/shortcuts/SKILL.md", bad)
    code, out, err = ateam(root, "improve", "validate", pid)
    assert code == 1 and "bypass" in out.lower()
    assert status(root, pid) == "proposed"


def test_overlapping_skill_is_refused_at_creation_and_validation(root, ateam):
    d = root / ".opencode/skills/pytest-speedups"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL.format(name="pytest-speedups", desc="Speed up slow pytest suites with selective runs. Use when the test gate exceeds its time budget.").replace("proposed", "active"))
    fb = feedback(ateam, root)
    code, _, err = ateam(root, "improve", "new", "--type", "skill", "--title", "speed up slow pytest suites", "--feedback", fb)
    assert code != 0 and "already covers" in err
    pid = new(ateam, root, "skill", "different words", fb)
    fill(root, pid)
    write(root, pid, "skills/pytest-faster/SKILL.md", SKILL.format(name="pytest-faster", desc="Speed up slow pytest suites with selective runs. Use when the test gate exceeds its time budget today."))
    code, out, _ = ateam(root, "improve", "validate", pid)
    assert code == 1 and "overlaps" in out


def test_skill_budget_is_enforced(root, ateam):
    (root / ".ateam/config.json").write_text(json.dumps({"learning_enabled": True, "skill_budget": 1}))
    d = root / ".opencode/skills/existing"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL.format(name="existing", desc="Totally unrelated xylophone calibration routine. Use when tuning instruments."))
    pid, _ = skill_proposal(ateam, root)
    code, out, _ = ateam(root, "improve", "validate", pid)
    assert code == 1 and "skill_budget" in out


def automation(ateam, root, test_body, script="def double(x):\n    return x * 2\n"):
    fb = feedback(ateam, root, sig="manual-check")
    pid = new(ateam, root, "automation", "double helper", fb)
    fill(root, pid)
    write(root, pid, "scripts/doubler.py", script)
    if test_body is not None:
        write(root, pid, "tests/test_doubler.py", test_body)
    return pid


GOOD_TEST = "import unittest\nfrom doubler import double\n\nclass T(unittest.TestCase):\n    def test_double(self):\n        self.assertEqual(double(2), 4)\n"


def test_tested_script_promotes_without_approval(root, ateam):
    pid = automation(ateam, root, GOOD_TEST)
    assert ateam(root, "improve", "validate", pid)[0] == 0
    code, out, err = ateam(root, "improve", "promote", pid)
    assert code == 0, err
    assert (root / ".opencode/scripts/extra/doubler.py").exists()
    assert (root / "tests/extra/test_doubler.py").exists()


def test_script_with_failing_or_missing_tests_is_not_promotable(root, ateam):
    bad = GOOD_TEST.replace("4)", "5)")
    pid = automation(ateam, root, bad)
    code, out, _ = ateam(root, "improve", "validate", pid)
    assert code == 1 and "tests failed" in out
    pid2 = automation(ateam, root, None)
    code, out, _ = ateam(root, "improve", "validate", pid2)
    assert code == 1 and "no tests" in out
    code, _, _ = ateam(root, "improve", "promote", pid2)
    assert code != 0 and not (root / ".opencode/scripts/extra").exists()


def test_docs_only_proposal_is_automatic(root, ateam):
    fb = feedback(ateam, root, sig="docs")
    pid = new(ateam, root, "automation", "clarify README", fb)
    fill(root, pid)
    write(root, pid, "docs/clarify.md", "# Clarification\nRun the tests with `bash tests/run.sh`.\n")
    assert ateam(root, "improve", "validate", pid)[0] == 0
    assert ateam(root, "improve", "promote", pid)[0] == 0
    assert (root / "docs/improvements/clarify.md").exists()


def test_declared_low_risk_cannot_hide_permission_changes(root, ateam):
    fb = feedback(ateam, root, sig="perm")
    pid = new(ateam, root, "agent-instruction", "let coder run anything", fb, "--risk", "low")
    fill(root, pid)
    write(root, pid, "agents/coder.md", "---\ndescription: x\nmode: subagent\npermissions:\n  - action: shell\n    resource: \"*\"\n    effect: allow\n---\nDo it.\n")
    code, out, _ = ateam(root, "improve", "validate", pid, "--json")
    assert json.loads(out)["risk"] == "high"
    code, _, err = ateam(root, "improve", "promote", pid)
    assert code == 3 and "explicit approval" in err
    assert not (root / ".opencode/agents/coder.md").exists()


def test_architecture_proposals_are_never_promoted(root, ateam):
    fb = feedback(ateam, root, sig="arch")
    pid = new(ateam, root, "architecture", "split the driver", fb)
    fill(root, pid)
    assert ateam(root, "improve", "validate", pid)[0] == 0
    ateam(root, "improve", "approve", pid, "--by", "huge")
    code, _, err = ateam(root, "improve", "promote", pid)
    assert code == 3 and "chef" in err


def test_knowledge_proposal_goes_to_inbox(root, ateam):
    fb = feedback(ateam, root, sig="know")
    pid = new(ateam, root, "knowledge", "unittest discovery needs tests/__init__.py", fb)
    fill(root, pid)
    assert ateam(root, "improve", "validate", pid)[0] == 0
    assert ateam(root, "improve", "promote", pid)[0] == 0
    inbox = list((root / ".ateam/knowledge/inbox").glob("*.md"))
    assert inbox and "PROP-001" in inbox[0].read_text()


def test_approve_requires_validated_and_a_name(root, ateam):
    pid, _ = skill_proposal(ateam, root)
    assert ateam(root, "improve", "approve", pid, "--by", "huge")[0] != 0  # still proposed
    ateam(root, "improve", "validate", pid)
    assert ateam(root, "improve", "approve", pid, "--by", "  ")[0] != 0


def test_deprecate_retires_the_skill_and_search_finds_installed(root, ateam):
    pid, _ = skill_proposal(ateam, root)
    ateam(root, "improve", "validate", pid)
    ateam(root, "improve", "approve", pid, "--by", "huge")
    assert ateam(root, "improve", "promote", pid)[0] == 0
    code, out, _ = ateam(root, "improve", "search", "pytest", "selective", "--json")
    assert any(h["id"] == "pytest-speedups" for h in json.loads(out))
    assert ateam(root, "improve", "deprecate", pid)[0] == 0
    assert not (root / ".opencode/skills/pytest-speedups").exists()
    assert (root / ".ateam/deprecated-skills/pytest-speedups/SKILL.md").exists()
    assert status(root, pid) == "deprecated"


def test_rejected_proposal_cannot_be_promoted(root, ateam):
    pid, _ = skill_proposal(ateam, root)
    ateam(root, "improve", "validate", pid)
    assert ateam(root, "improve", "reject", pid, "--reason", "not useful")[0] == 0
    assert ateam(root, "improve", "promote", pid)[0] != 0
