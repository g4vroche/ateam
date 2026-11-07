"""Phase 8: permission diff against the v0 snapshot, prompt budgets, policy, skill allowlists."""
import fnmatch
import json
import os
import re
import shutil
import subprocess

import pytest

from ateam import lint as L
from conftest import REPO

needs_opencode = pytest.mark.skipif(shutil.which("opencode") is None, reason="opencode not on PATH")
GOLDEN = REPO / "tests" / "golden"
A = "python3 .opencode/scripts/ateam.py"

EXTRA_PROBES = [
    ("edit", "src/app.py"), ("edit", "equipe/PLAN.md"), ("edit", "equipe/etapes/1.md"), ("edit", ".ateam/work/p/plan.md"),
    ("edit", ".ateam/work/p/reports/001-x.md"), ("edit", ".opencode/agents/coder.md"), ("shell", "git status"), ("shell", "git log -1"),
    ("shell", "git show HEAD"), ("shell", "rm -rf /"), ("shell", f"{A} work validate p"), ("shell", f"{A} run p"), ("read", ".env"),
    ("read", ".env.local"), ("read", "src/app.py"), ("webfetch", "https://example.com"), ("external_directory", "/etc/passwd"),
    ("subagent", "dev"), ("subagent", "coder"), ("subagent", "architect"), ("skill", "task-authoring"), ("skill", "browser"),
    ("browser", "*"), ("websearch", "x"), ("grep", "x"), ("glob", "x"), ("question", "x"),
]


@pytest.fixture(scope="module")
def resolved():
    out = subprocess.run(["opencode", "debug", "agents"], capture_output=True, text=True, cwd=REPO, env=dict(os.environ, PWD=str(REPO)))
    return {a["name"]: a for a in json.loads(out.stdout)}


def perms(a):
    return a.get("permissions") or a.get("permission") or []


def diffs(resolved):
    v0 = {a["name"]: a for a in json.loads((GOLDEN / "agents-v0.json").read_text())}
    probes = set(EXTRA_PROBES)
    for a in list(v0.values()) + list(resolved.values()):
        for p in perms(a):
            probes.add((p["action"], p["resource"]))
    out = []
    for name, a in v0.items():
        if name not in resolved:
            out.append((name, "*", "*", "present", "missing"))
            continue
        for act, res in sorted(probes):
            e0, e1 = L.evaluate(perms(a), act, res), L.evaluate(perms(resolved[name]), act, res)
            if e0 != e1:
                out.append((name, act, res, e0, e1))
    return out


def matches(change, d):
    name, act, res, e0, e1 = d
    return change["agent"] in ("*", name) and change["action"] == act and fnmatch.fnmatchcase(res, change["resource"]) and change["from"] in ("*", e0) and change["to"] in ("*", e1)


@needs_opencode
def test_every_permission_difference_from_v0_is_reviewed(resolved):
    intended = json.loads((GOLDEN / "intended-changes.json").read_text())["changes"]
    unlisted = [d for d in diffs(resolved) if not any(matches(c, d) for c in intended)]
    assert unlisted == [], "permission changes not in tests/golden/intended-changes.json:\n" + "\n".join(map(str, unlisted[:20]))


@needs_opencode
def test_intended_changes_list_has_no_stale_entries(resolved):
    ds = diffs(resolved)
    stale = [c for c in json.loads((GOLDEN / "intended-changes.json").read_text())["changes"] if not any(matches(c, d) for d in ds)]
    assert stale == [], f"intended changes that no longer happen: {stale}"


@needs_opencode
def test_models_of_existing_agents_are_unchanged(resolved):
    for name, model in json.loads((GOLDEN / "intended-changes.json").read_text())["unchanged_models"].items():
        assert resolved[name]["model"]["id"] == model, name


@needs_opencode
def test_new_agents_are_exactly_the_reviewed_set(resolved):
    v0 = {a["name"] for a in json.loads((GOLDEN / "agents-v0.json").read_text())}
    builtin = {"Build", "Compaction", "Explore", "General", "Plan", "Summary", "Title"}
    new = set(resolved) - v0 - builtin
    assert new == set(json.loads((GOLDEN / "intended-changes.json").read_text())["new_agents"])


@needs_opencode
def test_no_custom_agent_can_ask_or_read_secrets_unattended(resolved):
    """`--auto` approves everything that is not explicitly denied, so an `ask` outcome is never safe."""
    v0 = {a["name"] for a in json.loads((GOLDEN / "agents-v0.json").read_text())}
    builtin = {"Build", "Compaction", "Explore", "General", "Plan", "Summary", "Title"}
    for name, a in resolved.items():
        if name in builtin:
            continue
        # opencode's built-in defaults contain `ask` rules for .env; our later deny rules must win (last match wins)
        assert L.evaluate(perms(a), "read", ".env") == "deny", name
        assert L.evaluate(perms(a), "read", "config/.env.production") == "deny", name
        assert L.evaluate(perms(a), "read", ".env.example") == "allow", name


@needs_opencode
def test_each_agent_sees_only_its_own_skills(resolved):
    allowed = {"architect": {"task-authoring"}, "chef": {"manual-orchestration"}, "coder": {"quality-bootstrap", "feedback-recording"}, "tech-lead": set(), "tester": set(), "skillsmith": {"skill-authoring"}, "dreamer": {"knowledge-lookup"}}
    skills = {d.name for d in (REPO / ".opencode" / "skills").iterdir() if d.is_dir()}
    for agent, ok in allowed.items():
        for s in skills:
            want = "allow" if s in ok else "deny"
            assert L.evaluate(perms(resolved[agent]), "skill", s) == want, (agent, s)


# ----------------------------------------------------------------- static checks
def split_agent(path):
    """Agent files carry a list of maps (permissions) that the strict project parser does not support."""
    text = path.read_text()
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    head, body = m.group(1), m.group(2)
    meta = {k: v.strip().strip('"') for k, v in re.findall(r"(?m)^([a-z_]+):[ \t]*(.*)$", head)}
    return meta, body, head


def agent_files():
    return sorted(p for p in (REPO / ".opencode" / "agents").glob("*.md"))


def tokens(text):
    return len(text) // 4 + 1


BUDGET = {"architect": 1300, "chef": 1100, "coder": 1000, "tech-lead": 800, "tester": 800, "skillsmith": 1000, "dreamer": 1000}


def test_prompt_token_budgets():
    for name, budget in BUDGET.items():
        _, body, _h = split_agent(REPO / ".opencode" / "agents" / f"{name}.md")
        assert tokens(body) <= budget, f"{name} prompt is ~{tokens(body)} tokens (budget {budget})"


def test_policy_is_short_and_loaded_through_instructions():
    text = (REPO / ".opencode" / "policy" / "core.md").read_text()
    assert tokens(text) <= 600 and len(text.splitlines()) <= 40
    cfg = (REPO / ".opencode" / "opencode.jsonc").read_text()
    cfg = re.sub(r"(?m)^\s*//.*$", "", cfg)
    assert json.loads(cfg)["instructions"] == ["./.opencode/policy/core.md"]


def test_policy_states_the_universal_rules():
    text = (REPO / ".opencode" / "policy" / "core.md").read_text().lower()
    for needle in ("user's language", "english", "never claim", "credentials", "verdict:", "task: task-nnn"):
        assert needle in text, needle


def test_prompts_do_not_repeat_the_policy():
    for name in BUDGET:
        _, body, _h = split_agent(REPO / ".opencode" / "agents" / f"{name}.md")
        assert "never claim" not in body.lower() and "assert nothing" not in body.lower(), name
        assert "in english" not in body.lower().replace("and prompts you", ""), name


def test_agent_frontmatter_is_native_v2_without_temperature():
    for p in agent_files():
        meta, _, head = split_agent(p)
        assert "temperature" not in meta, p.name
        assert "permissions" in meta, p.name
        assert meta.get("mode") in ("primary", "subagent", "all"), p.name
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", str(meta.get("color", "#000000"))), p.name


def test_no_prompt_mentions_legacy_french_paths():
    for name in BUDGET:
        _, body, _h = split_agent(REPO / ".opencode" / "agents" / f"{name}.md")
        for word in ("equipe/", "etapes", "PLAN.md"):
            assert word not in body.replace("`plan.md`", ""), (name, word)


def test_chef_reply_language_rule_is_present():
    _, body, _h = split_agent(REPO / ".opencode" / "agents" / "chef.md")
    assert "in their language" in body or "user's language" in body
