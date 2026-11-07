import json
import shutil

import pytest

from ateam import frontmatter as fm
from ateam import schemas
from ateam import work as W


def proj_dir(root):
    return root / ".ateam" / "work" / "notes-api"


def test_golden_project_validates(project, ateam):
    code, out, _ = ateam(project, "work", "validate", "notes-api")
    assert code == 0, out


def test_graph_order_respects_dependencies(project):
    tasks = W.load_tasks(proj_dir(project))
    order = W.topo_order(tasks)
    assert order == ["TASK-001", "TASK-002", "TASK-003", "TASK-004"]


def edit_task(project, name, **updates):
    path = proj_dir(project) / "tasks" / name
    meta, body = fm.parse_file(path)
    meta.update(updates)
    path.write_text(fm.dump(meta, body))
    return path


def test_missing_dependency_detected(project, ateam):
    edit_task(project, "003-api-implementation.md", dependencies=["TASK-002", "TASK-099"])
    ateam(project, "work", "sync", "notes-api")
    code, out, _ = ateam(project, "work", "validate", "notes-api")
    assert code == 1 and "TASK-099 does not exist" in out


def test_cycle_detected(project, ateam):
    edit_task(project, "001-establish-quality-tooling.md", dependencies=["TASK-004"])
    ateam(project, "work", "sync", "notes-api")
    code, out, _ = ateam(project, "work", "validate", "notes-api")
    assert code == 1 and "dependency cycle" in out


def test_self_dependency_detected(project, ateam):
    edit_task(project, "002-domain-model.md", dependencies=["TASK-002"])
    ateam(project, "work", "sync", "notes-api")
    code, out, _ = ateam(project, "work", "validate", "notes-api")
    assert code == 1 and "depends on itself" in out


def test_duplicate_id_detected(project, ateam):
    p = proj_dir(project) / "tasks"
    shutil.copy(p / "002-domain-model.md", p / "005-copy.md")
    code, out, _ = ateam(project, "work", "validate", "notes-api")
    assert code == 1 and ("duplicate task id" in out or "does not match id" in out)


def test_manifest_drift_detected_and_synced(project, ateam):
    edit_task(project, "002-domain-model.md", status="in_progress")
    code, out, _ = ateam(project, "work", "validate", "notes-api")
    assert code == 1 and "manifest status" in out
    assert ateam(project, "work", "sync", "notes-api")[0] == 0
    assert ateam(project, "work", "validate", "notes-api")[0] == 0


def test_missing_file_and_orphan_manifest_entries(project, ateam):
    (proj_dir(project) / "tasks" / "004-integration-tests.md").unlink()
    code, out, _ = ateam(project, "work", "validate", "notes-api")
    assert code == 1 and "no task file defines it" in out


def test_manifest_with_extra_keys_rejected(project, ateam):
    mp = proj_dir(project) / "manifest.json"
    m = json.loads(mp.read_text())
    m["tasks"][0]["instructions"] = "copy of task body"
    mp.write_text(json.dumps(m))
    code, out, _ = ateam(project, "work", "validate", "notes-api")
    assert code == 1 and "index only" in out


def test_criterion_without_check_or_manual_rejected(project, ateam):
    p = proj_dir(project) / "tasks" / "004-integration-tests.md"
    p.write_text(p.read_text().replace(" [check: integration]", ""))
    code, out, _ = ateam(project, "work", "validate", "notes-api")
    assert code == 1 and "neither [check" in out


def test_unknown_check_reference_rejected(project, ateam):
    p = proj_dir(project) / "tasks" / "004-integration-tests.md"
    p.write_text(p.read_text().replace("[check: integration]", "[check: nope]"))
    code, out, _ = ateam(project, "work", "validate", "notes-api")
    assert code == 1 and "unknown check 'nope'" in out


@pytest.mark.parametrize("cmd", ["true", "echo ok", ":"])
def test_trivial_verify_commands_rejected(project, ateam, cmd):
    p = proj_dir(project) / "tasks" / "004-integration-tests.md"
    p.write_text(p.read_text().replace("npm test -- tests/integration", cmd))
    code, out, _ = ateam(project, "work", "validate", "notes-api")
    assert code == 1 and "trivial command" in out


def test_missing_required_section_rejected(project, ateam):
    p = proj_dir(project) / "tasks" / "004-integration-tests.md"
    p.write_text(p.read_text().replace("# Files to Inspect\n- tests/integration/\n- src/api/\n", ""))
    code, out, _ = ateam(project, "work", "validate", "notes-api")
    assert code == 1 and "Files to Inspect" in out


def test_unknown_skill_rejected(project, ateam):
    edit_task(project, "002-domain-model.md", skills=["ghost-skill"])
    code, out, _ = ateam(project, "work", "validate", "notes-api")
    assert code == 1 and "ghost-skill" in out


def test_template_placeholder_rejected(project, ateam):
    ateam(project, "work", "new-task", "notes-api", "Another")
    code, out, _ = ateam(project, "work", "validate", "notes-api")
    assert code == 1 and "placeholder" in out


def test_new_task_numbering_and_manifest(project, ateam):
    code, out, _ = ateam(project, "work", "new-task", "notes-api", "Docs pass", "--deps", "TASK-004")
    assert code == 0
    m = json.loads((proj_dir(project) / "manifest.json").read_text())
    assert m["tasks"][-1]["id"] == "TASK-005" and m["tasks"][-1]["dependencies"] == ["TASK-004"]


def test_completed_task_requires_report(project, ateam):
    edit_task(project, "001-establish-quality-tooling.md", status="completed")
    ateam(project, "work", "sync", "notes-api")
    code, out, _ = ateam(project, "work", "validate", "notes-api")
    assert code == 1 and "reports/001-establish-quality-tooling.md is missing" in out


def test_set_task_status_updates_task_then_manifest(project):
    W.set_task_status(project, "notes-api", "TASK-001", "in_progress")
    meta, _ = fm.parse_file(proj_dir(project) / "tasks" / "001-establish-quality-tooling.md")
    assert meta["status"] == "in_progress"
    m = json.loads((proj_dir(project) / "manifest.json").read_text())
    assert m["tasks"][0]["status"] == "in_progress"


def test_status_reports_ready_tasks(project, ateam):
    code, out, _ = ateam(project, "work", "status", "notes-api", "--json")
    data = json.loads(out)
    assert data["ready"] == ["TASK-001"] and data["total"] == 4


def test_schema_enums_exposed():
    assert "completed" in schemas.TASK_STATUSES
