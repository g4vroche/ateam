"""Scenario F: the knowledge store and the Dreamer pipeline."""
import hashlib
import json
import types

import pytest

from ateam import frontmatter as fm
from ateam import knowledge as K


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "p"
    (r / ".opencode").mkdir(parents=True)
    (r / ".ateam").mkdir()
    (r / ".ateam/config.json").write_text(json.dumps({"learning_enabled": True}))
    return r


def tree_hash(root):
    h = hashlib.sha256()
    base = root / ".ateam/knowledge"
    for p in sorted(base.rglob("*")) if base.exists() else []:
        if p.is_file() and p.name not in (".lock", ".dream.lock"):
            h.update(str(p.relative_to(root)).encode() + p.read_bytes())
    return h.hexdigest()


def report(root, completed=True, name="001-x"):
    pd = root / ".ateam/work/p"
    (pd / "tasks").mkdir(parents=True, exist_ok=True)
    (pd / "reports").mkdir(parents=True, exist_ok=True)
    (pd / "tasks" / f"{name}.md").write_text(f"---\nid: TASK-001\nstatus: {'completed' if completed else 'in_progress'}\n---\n# Objective\nx\n")
    (pd / "reports" / f"{name}.md").write_text("# Report\nUnittest needs tests/__init__.py with -t .\n")
    return f"report:.ateam/work/p/reports/{name}.md"


def draft(root, key="unittest-discovery", sources=None, body="# Problem\nDiscovery fails.\n# Validated Solution\nAdd tests/__init__.py.\n", conf="hypothesis", category="troubleshooting", title="Unittest discovery needs a package", name=None):
    d = root / ".ateam/knowledge/drafts"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name or key}.md"
    meta = {"title": title, "key": key, "category": category, "confidence": conf, "sources": sources if sources is not None else []}
    p.write_text(fm.dump(meta, body))
    return p


def fail(fn, *a, **kw):
    with pytest.raises(SystemExit) as e:
        fn(*a, **kw)
    return e.value


def test_upsert_creates_entry_with_provenance(root):
    src = report(root)
    res = K.upsert(root, draft(root, sources=[src]))
    assert res["result"] == "created" and res["id"] == "KNOW-0001"
    meta, body = fm.parse_file(root / res["path"])
    assert meta["sources"] == [src] and meta["status"] == "active" and "Validated Solution" in body
    assert not (root / ".ateam/knowledge/drafts/unittest-discovery.md").exists()


def test_provenance_is_mandatory_and_must_exist(root, capsys):
    fail(K.upsert, root, draft(root, sources=[]))
    fail(K.upsert, root, draft(root, sources=["report:.ateam/nope.md"]))
    fail(K.upsert, root, draft(root, sources=["somewhere"]))
    fail(K.upsert, root, draft(root, sources=["report:../../etc/passwd"]))
    assert not list((root / ".ateam/knowledge").glob("*/KNOW-*.md"))


def test_drafts_must_live_in_the_drafts_folder(root):
    src = report(root)
    p = draft(root, sources=[src])
    other = root / "elsewhere.md"
    other.write_text(p.read_text())
    fail(K.upsert, root, other)


def test_secrets_and_conversation_dumps_are_rejected(root):
    src = report(root)
    fail(K.upsert, root, draft(root, sources=[src], body="# Problem\napi key sk-abcdefghijklmnopqrstuvwxyz123456 used\n"))
    dump = "User: do it\nAssistant: ok\nUser: and this\nAssistant: sure\n"
    fail(K.upsert, root, draft(root, key="chat", sources=[src], body=dump))
    fail(K.upsert, root, draft(root, key="huge", sources=[src], body="line\n" * 100))
    assert not list((root / ".ateam/knowledge").glob("*/KNOW-*.md"))


def test_verified_needs_a_completed_task_report(root):
    done = report(root, True, "001-x")
    open_ = report(root, False, "002-y")
    r1 = K.upsert(root, draft(root, key="a", sources=[done], conf="verified"))
    assert fm.parse_file(root / r1["path"])[0]["confidence"] == "verified"
    r2 = K.upsert(root, draft(root, key="b", sources=[open_], conf="verified"))
    assert fm.parse_file(root / r2["path"])[0]["confidence"] == "hypothesis" and "confidence_downgraded" in r2
    inbox = root / ".ateam/knowledge/inbox/2026-11-07.md"
    inbox.parent.mkdir(parents=True)
    inbox.write_text("- a fact\n")
    r3 = K.upsert(root, draft(root, key="c", sources=["inbox:.ateam/knowledge/inbox/2026-11-07.md"], conf="verified"))
    assert fm.parse_file(root / r3["path"])[0]["confidence"] == "hypothesis"


def test_user_confirmation_is_human_only(root):
    fail(K.upsert, root, draft(root, sources=["user:huge"]))
    res = K.upsert(root, draft(root, sources=["user:huge"], conf="verified"), user_confirmed=True)
    assert fm.parse_file(root / res["path"])[0]["confidence"] == "verified"


def test_same_input_is_idempotent(root):
    src = report(root)
    K.upsert(root, draft(root, sources=[src]))
    before = tree_hash(root)
    res = K.upsert(root, draft(root, sources=[src]))
    assert res["result"] == "unchanged"
    reworded = draft(root, sources=[src], body="# Problem\nDifferent words, same sources.\n")
    assert K.upsert(root, reworded)["result"] == "unchanged"
    assert tree_hash(root) == before


def test_new_source_for_same_fact_merges_sources(root):
    s1, s2 = report(root, True, "001-x"), report(root, True, "002-y")
    K.upsert(root, draft(root, sources=[s1]))
    res = K.upsert(root, draft(root, sources=[s2]))
    assert res["result"] == "merged"
    meta, _ = fm.parse_file(root / res["path"])
    assert meta["sources"] == [s1, s2]


def test_contradiction_is_flagged_never_overwritten(root):
    s1, s2 = report(root, True, "001-x"), report(root, True, "002-y")
    first = K.upsert(root, draft(root, sources=[s1], body="# Problem\nUse tabs.\n"))
    old_text = (root / first["path"]).read_text()
    res = K.upsert(root, draft(root, sources=[s2], body="# Problem\nUse spaces.\n"))
    assert res["result"] == "contradiction" and res["conflicts_with"] == first["id"]
    assert "Use tabs." in (root / first["path"]).read_text()  # body untouched
    assert (root / first["path"]).read_text() != old_text  # but flagged
    old_meta, _ = fm.parse_file(root / first["path"])
    new_meta, _ = fm.parse_file(root / res["path"])
    assert old_meta["status"] == "contradicted" and new_meta["status"] == "contradicted"
    assert K.search(root, "tabs spaces") == []  # hidden from retrieval until a human decides
    assert "Needs review" in K.render_index(root)
    out = K.resolve(root, res["id"])
    assert out["deprecated"] == [first["id"]]
    assert fm.parse_file(root / res["path"])[0]["status"] == "active"
    assert K.lint_store(root) == []


def test_supersede_keeps_history(root):
    s1, s2 = report(root, True, "001-x"), report(root, True, "002-y")
    first = K.upsert(root, draft(root, sources=[s1], body="# Problem\nold\n"))
    res = K.upsert(root, draft(root, sources=[s2], body="# Problem\nnew\n"), supersede=True)
    assert res["result"] == "superseded"
    assert fm.parse_file(root / first["path"])[0]["status"] == "deprecated"
    assert fm.parse_file(root / res["path"])[0]["supersedes"] == first["id"]
    assert K.lint_store(root) == []


def test_ids_are_unique_and_index_is_stable(root):
    src = report(root)
    ids = [K.upsert(root, draft(root, key=f"k{i}", sources=[src], title=f"Fact {i}"))["id"] for i in range(3)]
    assert ids == ["KNOW-0001", "KNOW-0002", "KNOW-0003"]
    assert K.write_index(root) is True
    assert K.write_index(root) is False
    assert "Fact 2" in (root / ".ateam/knowledge/INDEX.md").read_text()


def test_search_returns_at_most_five(root):
    src = report(root)
    for i in range(8):
        K.upsert(root, draft(root, key=f"k{i}", sources=[src], title=f"Cache invalidation {i}", body="# Problem\ncache stale\n"))
    hits = K.search(root, "cache")
    assert len(hits) == 5 and all({"id", "title", "path"} <= set(h) for h in hits)
    assert K.search(root, "cache", category="lessons") == []


def test_collect_checkpoint_ledger(root):
    assert K.collect(root)["new"] == 0 and not K.pending_path(root).exists()
    report(root)
    inbox = root / ".ateam/knowledge/inbox/2026-11-07.md"
    inbox.parent.mkdir(parents=True)
    inbox.write_text("- fact\n")
    c = K.collect(root)
    assert c["new"] == 2 and K.pending_path(root).exists()
    K.checkpoint(root)
    assert K.collect(root)["new"] == 0
    inbox.write_text("- fact\n- another fact\n")
    assert [s["path"] for s in K.collect(root)["sources"]] == [".ateam/knowledge/inbox/2026-11-07.md"]


def test_checkpoint_without_collect_is_an_error(root):
    fail(K.checkpoint, root)


class FakeRes:
    def __init__(self, ok=True):
        self.ok = ok


def make_agent(root, ok=True, create=True):
    calls = []

    def agent(root_, binary, name, prompt, timeout, log, standalone, **kw):
        calls.append(name)
        if create:
            src = ".ateam/knowledge/inbox/2026-11-07.md"
            K.upsert(root_, draft(root_, key="dreamed", sources=[f"inbox:{src}"], title="Dreamed fact"))
        return FakeRes(ok)

    agent.calls = calls
    return agent


def seed_inputs(root):
    inbox = root / ".ateam/knowledge/inbox/2026-11-07.md"
    inbox.parent.mkdir(parents=True)
    inbox.write_text("- a durable fact\n")


def test_second_dream_on_unchanged_inputs_is_a_noop_with_no_model_call(root):
    seed_inputs(root)
    agent = make_agent(root)
    first = K.dream(root, run_agent=agent)
    assert first["status"] == "dreamed" and agent.calls == ["dreamer"]
    assert (root / ".ateam/knowledge/INDEX.md").exists()
    before = tree_hash(root)
    second = K.dream(root, run_agent=agent)
    assert second == {"status": "nothing-new", "model_called": False}
    assert agent.calls == ["dreamer"]  # still one call
    assert tree_hash(root) == before


def test_failed_dream_does_not_checkpoint_and_is_retried(root):
    seed_inputs(root)
    bad = make_agent(root, ok=False, create=False)
    res = K.dream(root, run_agent=bad)
    assert res["status"] == "failed" and not K.checkpoint_path(root).exists()
    good = make_agent(root)
    assert K.dream(root, run_agent=good)["status"] == "dreamed"


def test_dream_with_invalid_store_is_not_checkpointed(root):
    seed_inputs(root)
    d = root / ".ateam/knowledge/troubleshooting"
    d.mkdir(parents=True)
    (d / "KNOW-0001-bad.md").write_text(fm.dump({"id": "KNOW-0001", "title": "x", "key": "bad", "category": "troubleshooting", "created_at": "2026-11-07", "updated_at": "2026-11-07", "confidence": "verified", "status": "active", "sources": ["inbox:x"]}, "# Problem\nx\n"))
    res = K.dream(root, run_agent=make_agent(root, create=False))
    assert res["status"] == "failed" and "verified" in res["reason"]
    assert not K.checkpoint_path(root).exists()


def test_dream_cli_is_locked_without_g1(tmp_path, ateam):
    r = tmp_path / "q"
    (r / ".opencode").mkdir(parents=True)
    code, _, err = ateam(r, "knowledge", "dream")
    assert code == 3 and "G1" in err


def test_dream_cli_with_nothing_new_makes_no_call(root, ateam):
    (root / ".ateam/config.json").write_text(json.dumps({"learning_enabled": True, "opencode_bin": "/nonexistent/opencode"}))
    code, out, err = ateam(root, "knowledge", "dream", "--json")
    assert code == 0 and json.loads(out)["model_called"] is False


def test_collect_require_new_exit_code(root, ateam):
    assert ateam(root, "knowledge", "collect", "--require-new")[0] == 20
    seed_inputs(root)
    assert ateam(root, "knowledge", "collect", "--require-new")[0] == 0


def test_resolve_paths_for_task_knowledge_refs(root):
    src = report(root)
    r = K.upsert(root, draft(root, sources=[src]))
    ok, bad = K.resolve_paths(root, [r["id"], "KNOW-9999"])
    assert ok == [r["path"]] and bad == ["KNOW-9999"]


def test_cron_script_skips_when_nothing_new(root):
    import subprocess
    from conftest import SCRIPTS

    env = {"PATH": "/usr/bin:/bin", "ATEAM_ROOT": str(root)}
    (root / ".opencode/scripts").mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copytree(SCRIPTS / "ateam", root / ".opencode/scripts/ateam", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(SCRIPTS / "ateam.py", root / ".opencode/scripts/ateam.py")
    shutil.copytree(SCRIPTS.parent / "templates", root / ".opencode/templates", dirs_exist_ok=True)
    p = subprocess.run(["bash", str(SCRIPTS / "dream-cron.sh")], capture_output=True, text=True, env=env, cwd=root)
    assert p.returncode == 0 and "nothing new" in p.stdout
