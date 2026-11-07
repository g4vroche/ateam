"""`ateam init`, `upgrade` and `gc`."""
import json
import os
import time

import pytest

from ateam import feedback as F
from ateam import gc as G
from ateam import init_cmd as I
from ateam.common import OPENCODE_DIR


def cli(ateam, root, *args):
    return ateam(root, *args)


def test_init_installs_boilerplate_and_skeleton(tmp_path, ateam):
    t = tmp_path / "proj"
    code, out, err = ateam(tmp_path, "init", "--target", str(t), "--json")
    assert code == 0, err
    res = json.loads(out)
    assert (t / ".opencode/agents/chef.md").exists() and (t / ".opencode/scripts/ateam.py").exists()
    assert (t / ".ateam/VERSION").read_text().strip() == (OPENCODE_DIR / "VERSION").read_text().strip()
    assert (t / ".ateam/config.json").exists() and (t / ".ateam/work").is_dir()
    gi = (t / ".gitignore").read_text()
    assert ".ateam/runs/" in gi and ".ateam/work/*/state/" in gi
    assert not list(t.rglob("__pycache__"))
    assert res["added"] and res["conflicts"] == []
    # the installed copy works on its own
    assert ateam(t, "lint", "skills")[0] == 0
    assert ateam(t, "agents", "sync-aliases", "--check")[0] == 0


def test_init_is_idempotent_for_gitignore_and_refuses_overwrite(tmp_path, ateam):
    t = tmp_path / "proj"
    t.mkdir()
    (t / ".gitignore").write_text("node_modules/\n")
    assert ateam(tmp_path, "init", "--target", str(t))[0] == 0
    assert (t / ".gitignore").read_text().startswith("node_modules/")
    code, _, err = ateam(tmp_path, "init", "--target", str(t))
    assert code != 0 and "upgrade" in err
    assert ateam(tmp_path, "init", "--target", str(t), "--force")[0] == 0
    assert (t / ".gitignore").read_text().count(I.GITIGNORE_MARK) == 1


def test_init_without_target_only_creates_skeleton(tmp_path, ateam):
    code, out, _ = ateam(tmp_path, "init", "--json")
    assert code == 0 and (tmp_path / ".ateam/config.json").exists() and not (tmp_path / ".opencode").exists()
    assert json.loads(ateam(tmp_path, "init", "--json")[1])["skeleton"] == []


def test_upgrade_keeps_user_edits_and_reports_conflicts(tmp_path, ateam, monkeypatch):
    t = tmp_path / "proj"
    ateam(tmp_path, "init", "--target", str(t))
    chef = t / ".opencode/agents/chef.md"
    mine = chef.read_text() + "\nMy own rule.\n"
    chef.write_text(mine)
    # an upstream change: pretend the source tree changed by pointing at a modified copy
    import shutil

    src = tmp_path / "src/.opencode"
    shutil.copytree(OPENCODE_DIR, src, ignore=shutil.ignore_patterns("__pycache__"))
    (src / "agents/chef.md").write_text((src / "agents/chef.md").read_text() + "\nUpstream rule.\n")
    (src / "skills/task-authoring/SKILL.md").write_text((src / "skills/task-authoring/SKILL.md").read_text() + "\nUpstream tip.\n")
    (src / "VERSION").write_text("2.1.0\n")
    monkeypatch.setattr(I, "OPENCODE_DIR", src)
    res = I.install(t, upgrade=True)
    assert res["conflicts"] == ["agents/chef.md"]
    assert chef.read_text() == mine  # untouched
    assert "Upstream rule." in (t / ".opencode/agents/chef.md.new").read_text()
    assert "Upstream tip." in (t / ".opencode/skills/task-authoring/SKILL.md").read_text()  # unmodified file updated
    assert sorted(res["updated"]) == ["VERSION", "skills/task-authoring/SKILL.md"]
    assert (t / ".ateam/VERSION").read_text().strip() == "2.1.0"


def test_upgrade_requires_installation_and_dry_run_writes_nothing(tmp_path, ateam):
    t = tmp_path / "proj"
    t.mkdir()
    assert ateam(tmp_path, "upgrade", "--target", str(t))[0] != 0
    ateam(tmp_path, "init", "--target", str(t))
    before = (t / ".ateam/installed.json").read_text()
    (t / ".opencode/agents/coder.md").write_text("changed by user\n")
    code, out, _ = ateam(tmp_path, "upgrade", "--target", str(t), "--dry-run", "--json")
    res = json.loads(out)
    assert code == 0 and res["conflicts"] == [] and res["kept_local"] == ["agents/coder.md"]  # upstream unchanged: no conflict
    assert (t / ".ateam/installed.json").read_text() == before


def test_upgrade_from_unstamped_install_prints_all_migration_notes(tmp_path, ateam):
    t = tmp_path / "proj"
    t.mkdir()
    code, out, _ = ateam(tmp_path, "upgrade", "--target", str(t))
    assert code != 0
    ateam(tmp_path, "init", "--target", str(t))
    (t / ".ateam/VERSION").unlink()
    (t / ".ateam/installed.json").unlink()
    code, out, _ = ateam(tmp_path, "upgrade", "--target", str(t), "--json")
    assert "2.0.0" in json.loads(out)["notes"]


def test_upgrade_does_not_touch_project_data(tmp_path, ateam):
    t = tmp_path / "proj"
    ateam(tmp_path, "init", "--target", str(t))
    data = t / ".ateam/work/p/plan.md"
    data.parent.mkdir(parents=True)
    data.write_text("# mine\n")
    ateam(tmp_path, "upgrade", "--target", str(t))
    assert data.read_text() == "# mine\n"


# ------------------------------------------------------------------------- gc
def age(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "p"
    (r / ".opencode").mkdir(parents=True)
    (r / ".ateam").mkdir()
    return r


def mkrun(root, n, status="completed", pid=None):
    rid = f"RUN-20261101-{n:03d}"
    d = root / ".ateam/runs" / rid
    d.mkdir(parents=True)
    (d / "summary.json").write_text("{}")
    (root / ".ateam/runs" / f"{rid}.json").write_text(json.dumps({"id": rid, "status": status, "pid": pid}))
    return rid


def test_gc_is_dry_run_by_default_and_keeps_newest_runs(root, ateam):
    (root / ".ateam/config.json").write_text(json.dumps({"retention": {"keep_runs": 2}}))
    ids = [mkrun(root, i) for i in range(1, 6)]
    code, out, _ = ateam(root, "gc", "--json")
    res = json.loads(out)
    assert code == 0 and res["applied"] is False and res["removed"] == 0
    assert all((root / ".ateam/runs" / i).exists() for i in ids)
    victims = {i["path"] for i in res["items"]}
    assert f".ateam/runs/{ids[0]}" in victims and f".ateam/runs/{ids[-1]}" not in victims and f".ateam/runs/{ids[-2]}" not in victims
    assert ateam(root, "gc", "--apply")[0] == 0
    assert [i for i in ids if (root / ".ateam/runs" / i).exists()] == ids[-2:]
    assert not (root / ".ateam/runs" / f"{ids[0]}.json").exists()


def test_gc_never_removes_a_live_run(root):
    (root / ".ateam/config.json").write_text(json.dumps({"retention": {"keep_runs": 1}}))
    live = mkrun(root, 1, "running", os.getpid())
    dead = mkrun(root, 2, "running", 999999999)
    mkrun(root, 3)
    victims = {i["path"] for i in G.plan(root)}
    assert f".ateam/runs/{live}" not in victims and f".ateam/runs/{dead}" in victims


def test_gc_state_of_completed_projects_keeps_immutability_hashes(root):
    pdir = root / ".ateam/work/p"
    (pdir / "tasks").mkdir(parents=True)
    (pdir / "state/TASK-001.logs").mkdir(parents=True)
    (pdir / "tasks/001-x.md").write_text("---\nid: TASK-001\nstatus: completed\n---\n# Objective\nx\n")
    for f in ("TASK-001.json", "TASK-001.gate.json", "final.json", "TASK-001.baseline.json", "run-baseline.json"):
        (pdir / "state" / f).write_text("{}")
        age(pdir / "state" / f, 90)
    (pdir / "state/TASK-001.logs/a.log").write_text("x")
    age(pdir / "state/TASK-001.logs", 90)
    victims = {i["path"].split("/")[-1] for i in G.plan(root)}
    assert victims == {"TASK-001.logs", "TASK-001.baseline.json", "run-baseline.json"}


def test_gc_leaves_state_of_unfinished_projects(root):
    pdir = root / ".ateam/work/p"
    (pdir / "tasks").mkdir(parents=True)
    (pdir / "state").mkdir()
    (pdir / "tasks/001-x.md").write_text("---\nid: TASK-001\nstatus: in_progress\n---\n# Objective\nx\n")
    (pdir / "state/TASK-001.baseline.json").write_text("{}")
    age(pdir / "state/TASK-001.baseline.json", 90)
    assert G.plan(root) == []


def test_gc_feedback_only_when_addressed_and_old(root, ateam):
    def mk(sig):
        fid, path = F.add_note(root, category="tooling-gap", severity="low", signature=sig, problem="p", evidence=["x"], task=f"TASK-00{ord(sig) - 96}", run_id="RUN-20261101-001", agent="coder", context="", workaround="", improvement="")
        return fid, path

    f1, p1 = mk("a")
    f2, p2 = mk("b")
    f3, p3 = mk("c")
    F.set_status(root, f1, "addressed", "PROP-001")
    F.set_status(root, f2, "dismissed")
    for p in (p1, p2, p3):
        age(p, 200)
    victims = {i["path"] for i in G.plan(root)}
    assert str(p1.relative_to(root)) in victims and str(p2.relative_to(root)) in victims
    assert str(p3.relative_to(root)) not in victims  # open feedback is never collected
    assert ateam(root, "gc", "--apply")[0] == 0
    assert p3.exists() and not p1.exists()
    assert [e["id"] for e in F.read_events(root)[0]] == [f3]


def test_gc_inbox_only_after_it_was_dreamed(root):
    from ateam import knowledge as K

    inbox = root / ".ateam/knowledge/inbox"
    inbox.mkdir(parents=True)
    f = inbox / "2026-10-01.md"
    f.write_text("- fact\n")
    age(f, 60)
    assert G.plan(root) == []  # not dreamed yet
    K.collect(root)
    K.checkpoint(root)
    age(f, 60)
    assert [i["kind"] for i in G.plan(root)] == ["inbox"]
