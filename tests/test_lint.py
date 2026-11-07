import json

from ateam import lint as L
from conftest import REPO

GOLDEN = json.loads((REPO / "tests" / "golden" / "agents-v0.json").read_text())


def test_evaluate_last_match_wins():
    perms = [
        {"action": "edit", "resource": "*", "effect": "deny"},
        {"action": "edit", "resource": "docs/*", "effect": "allow"},
    ]
    assert L.evaluate(perms, "edit", "docs/a.md") == "allow"
    assert L.evaluate(perms, "edit", "src/a.py") == "deny"
    assert L.evaluate(perms, "shell", "ls") == "ask"


def test_golden_v0_snapshot_has_legacy_agents():
    ids = {a["id"] for a in GOLDEN}
    assert {"chef", "architecte", "dev", "tech-lead", "testeur"} <= ids


def test_v0_known_defect_documented_architecte_cannot_write_etapes():
    arch = next(a for a in GOLDEN if a["id"] == "architecte")
    assert L.evaluate(arch["permissions"], "edit", "equipe/PLAN.md") == "allow"
    assert L.evaluate(arch["permissions"], "edit", "equipe/etapes/01.md") == "deny"


def test_skills_lint_ok_on_fixture(project):
    assert L.lint_skills(project) == []


def test_skills_lint_flags_bad_skill(project):
    d = project / ".opencode" / "skills" / "Bad_Skill"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: Bad\ndescription: short\n---\nbody `references/x.md`\n")
    errs = L.lint_skills(project)
    assert any("kebab-case" in e for e in errs)
    assert any("description" in e for e in errs)
    assert any("references/x.md" in e for e in errs)


def test_skills_overlap_detected(project):
    for name in ("alpha-skill", "beta-skill"):
        d = project / ".opencode" / "skills" / name
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: X\ndescription: Establish missing quality tooling for projects. Use when quality detect reports missing categories.\n---\nbody\n"
        )
    errs = L.lint_skills(project)
    assert any("overlap" in e for e in errs)
