"""Gate G1 evaluation and the learning lock."""
import json

import pytest

from ateam import feedback as F
from ateam import g1
from ateam.common import learning_enabled

RUN = "RUN-20261107-%03d"


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "p"
    (r / ".opencode").mkdir(parents=True)
    (r / ".ateam").mkdir()
    return r


def bench(root, variant, i, wall, out, passed=5, total=5, timed_out=False):
    d = root / "bench/results"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{variant}-{i}.json").write_text(json.dumps({"variant": variant, "wall_s": wall, "timed_out": timed_out, "hidden": {"passed": passed, "total": total}, "stats": {"tokens": {"output": out}}}))


def good_bench(root):
    for i in range(2):
        bench(root, "baseline", i, 1000, 30000)
        bench(root, "current", i, 600, 20000)


def runs(root, n, real=True, with_events=True, sig=lambda i: "slow-tests"):
    for i in range(1, n + 1):
        rid = RUN % i
        d = root / ".ateam/runs" / rid
        d.mkdir(parents=True, exist_ok=True)
        (d / "summary.json").write_text(json.dumps({"run": rid, "real": real}))
        if with_events:
            ev = root / ".ateam/feedback/events" / rid
            ev.mkdir(parents=True, exist_ok=True)
            (ev / f"coder-FB-{i:06d}.md").write_text(
                F.render_event(f"FB-{i:06d}", rid, "TASK-001", "coder", "tooling-gap", "low", "agent", sig(i), "tests were slow", ["logs/x.txt"], "", "", "")
            )


def test_no_data_is_no_go(root):
    res = g1.evaluate(root)
    assert res["decision"] == "no-go" and len(res["reasons"]) == 3


def test_all_criteria_met_is_go(root):
    good_bench(root)
    runs(root, 5)
    res = g1.evaluate(root)
    assert res["decision"] == "go", res["reasons"]


def test_fake_runs_do_not_count(root):
    good_bench(root)
    runs(root, 6, real=False)
    res = g1.evaluate(root)
    assert res["decision"] == "no-go" and any("real runs" in r for r in res["reasons"])


def test_runs_without_events_do_not_count(root):
    good_bench(root)
    runs(root, 6, with_events=False)
    assert g1.evaluate(root)["decision"] == "no-go"


def test_unique_signatures_are_not_recurring_patterns(root):
    good_bench(root)
    runs(root, 5, sig=lambda i: f"sig-{i}")
    res = g1.evaluate(root)
    assert res["decision"] == "no-go" and any("recurring" in r for r in res["reasons"])


def test_benchmark_without_gain_is_no_go(root):
    for i in range(2):
        bench(root, "baseline", i, 1000, 30000)
        bench(root, "current", i, 980, 29500)
    runs(root, 5)
    assert any(r.startswith("benchmark") for r in g1.evaluate(root)["reasons"])


def test_gain_with_lower_correctness_is_no_go(root):
    for i in range(2):
        bench(root, "baseline", i, 1000, 30000, passed=5)
        bench(root, "current", i, 500, 10000, passed=4)
    runs(root, 5)
    res = g1.evaluate(root)
    assert res["decision"] == "no-go" and res["criteria"][0]["correctness_not_lower"] is False


def test_leaked_or_invalid_bench_runs_are_ignored(root):
    d = root / "bench/results"
    d.mkdir(parents=True)
    (d / "x.json").write_text(json.dumps({"variant": "current", "leaked_into_repo": True, "wall_s": 1, "hidden": {"passed": 5, "total": 5}}))
    (d / "y.json").write_text(json.dumps({"variant": "current", "wall_s": 1, "hidden": {"passed": 0, "total": 0}}))
    assert g1.evaluate(root)["criteria"][0]["n"] == {"baseline": 0, "current": 0}


def test_lock_follows_g1_file_and_explicit_config(root, ateam):
    assert learning_enabled(root) is False
    (root / ".ateam/g1.json").write_text(json.dumps({"decision": "go"}))
    assert learning_enabled(root) is True
    (root / ".ateam/config.json").write_text(json.dumps({"learning_enabled": False}))
    assert learning_enabled(root) is False


def test_cli_writes_decision_and_exit_code(root, ateam):
    code, out, _ = ateam(root, "g1", "--json")
    assert code == 10 and json.loads(out)["decision"] == "no-go"
    assert json.loads((root / ".ateam/g1.json").read_text())["decision"] == "no-go"
    code, out, err = ateam(root, "improve", "new", "--type", "skill", "--title", "x", "--feedback", "FB-000001")
    assert code != 0 and "G1" in err
