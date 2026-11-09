"""`ateam run`: the deterministic driver. Zero LLM tokens of its own.

Loop: next task -> coder (fresh headless session, timeout) -> hardened gate -> complete | fail (bounded retries).
Then once per run: integration gate, final review by the tech-lead, exploratory testing. Every stop is an escalation
with an evidence file; every state change goes through the same `work` code the CLI uses, so a killed run resumes.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

from . import bundle as B
from . import gate as G
from . import lifecycle as L
from . import opencode as OC
from . import quality as Q
from . import work as W
from .common import (
    atomic_write,
    ateam_dir,
    die,
    emit,
    file_lock,
    find_root,
    load_config,
    now_iso,
    read_json,
    sha256_text,
    today,
    write_json,
)

EXIT_OK = 0
EXIT_BLOCKED = 10
EXIT_ARCHITECT = 11
EXIT_FAILED = 12
EXIT_INTERRUPTED = 130
RECENT_LINES = 6
PROGRESS_SAVE_S = 3.0
KIND_EXIT = {
    "needs_architect": EXIT_ARCHITECT,
    "blocked": EXIT_BLOCKED,
    "review_exhausted": EXIT_BLOCKED,
    "review_no_verdict": EXIT_BLOCKED,
    "tester_exhausted": EXIT_BLOCKED,
    "integration_failed": EXIT_FAILED,
    "provider_unavailable": EXIT_FAILED,
    "invalid": EXIT_FAILED,
    "stuck": EXIT_FAILED,
}
HINTS = {
    "needs_architect": "Run the architect to re-scope: `work split <task>` for the failing task, create replacement tasks, `work sync`, `work validate`; then `ateam run <project> --resume`.",
    "blocked": "Attempts are exhausted. Ask the user how to proceed (relax the task, fix the environment, or abandon it).",
    "review_exhausted": "The reviewer still requested changes after the maximum number of rounds. Ask the user to decide.",
    "review_no_verdict": "A reviewer produced no parseable verdict twice. Inspect the review file and ask the user.",
    "tester_exhausted": "The tester still found defects after the maximum number of rounds. Ask the user to decide.",
    "integration_failed": "Every task passed but the integration gate failed. Add a fix task with the architect (`work new-task`), then `ateam run <project> --resume`.",
    "provider_unavailable": "The model provider is unavailable (quota, auth or outage). Nothing was lost: fix access, then `ateam run <project> --resume`.",
    "invalid": "The work project is invalid. Run the architect to fix it (`work validate <project>`).",
    "stuck": "No task is runnable although tasks remain. Run `work graph <project>`; the architect must fix dependencies.",
}


class Interrupted(BaseException):
    pass


class Stop(Exception):
    def __init__(self, kind: str, task: str | None = None, reason: str = "", evidence: list[str] | None = None, extra: dict | None = None):
        super().__init__(f"{kind}: {reason}")
        self.kind, self.task, self.reason, self.evidence, self.extra = kind, task, reason, evidence or [], extra or {}


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def runs_dir(root: Path) -> Path:
    return ateam_dir(root) / "runs"


def new_run_id(root: Path) -> str:
    rd = runs_dir(root)
    rd.mkdir(parents=True, exist_ok=True)
    day = today().replace("-", "")
    n = 1
    while True:
        rid = f"RUN-{day}-{n:03d}"
        try:
            fd = os.open(rd / f"{rid}.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            n += 1
            continue
        os.close(fd)
        return rid


def mark_stale_runs(root: Path, project: str) -> None:
    for p in sorted(runs_dir(root).glob("RUN-*.json")):
        rec = read_json(p, None) if p.stat().st_size else None
        if rec and rec.get("project") == project and rec.get("status") == "running" and not pid_alive(rec.get("pid", 0)):
            rec["status"] = "interrupted"
            rec["finished"] = now_iso()
            write_json(p, rec)


class Driver:
    def __init__(self, root: Path, project: str, resume: bool = False, adhoc: bool = False, out=None):
        self.root, self.project, self.resume, self.adhoc = root, project, resume, adhoc
        self.cfg = load_config(root)
        self.binary = os.environ.get("ATEAM_OPENCODE_BIN") or self.cfg["opencode_bin"]
        self.pdir = W.require_project(root, project)
        self.out = out or sys.stderr
        self.run_id = new_run_id(root)
        self.rundir = runs_dir(root) / self.run_id
        self.rundir.mkdir(parents=True, exist_ok=True)
        self.record = {
            "id": self.run_id,
            "project": project,
            "adhoc": adhoc,
            "resume": resume,
            # a run is "real" only when it drove a genuine model; fake shims used by tests never count toward gate G1
            "real": not (os.environ.get("FAKE_OC_SCRIPT") or "fake_opencode" in str(self.binary)),
            "pid": os.getpid(),
            "started": now_iso(),
            "finished": None,
            "status": "running",
            "tasks": {},
            "roles": [],
            "gates": [],
            "signals": [],
            "totals": {"role_calls": 0, "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}, "role_s": 0.0, "gate_s": 0.0, "attempts": 0, "retries": 0},
            "escalation": None,
            "exit_code": None,
            "parent_session": None,
            "current": None,
            "recent": [],
        }
        self.t0 = time.time()
        self.parent: str | None = None
        self._saved_at = 0.0
        self.save()

    # ------------------------------------------------------------------ plumbing
    def say(self, msg: str) -> None:
        print(f"[{self.run_id}] {msg}", file=self.out, flush=True)
        self.record["recent"] = (self.record["recent"] + [msg[:200]])[-RECENT_LINES:]
        self.save()

    def attach_parent(self) -> None:
        """Run roles as child sessions of the chef so the OpenCode UI shows them live; else stay standalone."""
        sid = os.environ.get("ATEAM_PARENT_SESSION")
        if not sid and self.cfg["attach_to_chef"]:
            sid = OC.find_parent_session(self.root, self.binary)
        self.parent = sid or None
        self.record["parent_session"] = self.parent
        self.say(f"roles run as child sessions of {self.parent}" if self.parent else "roles run in standalone sessions (no active chef session found)")

    def save(self) -> None:
        self._saved_at = time.time()
        write_json(runs_dir(self.root) / f"{self.run_id}.json", self.record)

    def task(self, tid: str) -> W.Task:
        t = W.task_index(W.load_tasks(self.pdir)).get(tid)
        if t is None:
            raise Stop("invalid", tid, f"task {tid} vanished")
        return t

    def tasks(self) -> list[W.Task]:
        return [t for t in W.load_tasks(self.pdir) if not t.errors]

    def tstate(self, tid: str) -> dict:
        return G.load_state(self.pdir, tid)

    def update_state(self, tid: str, **kv) -> dict:
        with file_lock(self.pdir / "state" / ".lock"):
            st = G.load_state(self.pdir, tid)
            st.update(kv)
            G.save_state(self.pdir, tid, st)
        return st

    def adjust_attempts(self, tid: str, delta: int) -> None:
        with file_lock(self.pdir / "state" / ".lock"):
            st = G.load_state(self.pdir, tid)
            st["attempts"] = max(0, st["attempts"] + delta)
            G.save_state(self.pdir, tid, st)

    def trec(self, tid: str) -> dict:
        return self.record["tasks"].setdefault(tid, {"attempts": 0, "coder_s": 0.0, "gate_s": 0.0, "review_rounds": 0, "outcome": None})

    def signal(self, kind: str, task: str | None, detail: str, **extra) -> None:
        ev = {"kind": kind, "task": task, "detail": detail[:300], "at": now_iso(), **extra}
        self.record["signals"].append(ev)
        try:
            from . import feedback

            feedback.record_signal(self.root, self.run_id, kind, task, detail, **extra)
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001 - capture must never break the run
            self.say(f"warning: could not record signal {kind}: {exc}")

    def on_event(self, e: dict) -> None:
        cur = self.record["current"]
        if cur is None:
            return
        if e.get("sessionID"):
            cur["session"] = e["sessionID"]
        if e.get("type") == "step_finish":
            cur["steps"] += 1
        act = OC.activity(e)
        if act:
            cur["last"] = act
        if act or time.time() - self._saved_at >= PROGRESS_SAVE_S:
            self.save()

    def role(self, agent: str, prompt: str, timeout: int, phase: str, task: str | None = None, session: str | None = None, env_extra: dict | None = None, label: str = "") -> OC.RoleResult:
        n = len(self.record["roles"]) + 1
        env_extra = {"ATEAM_RUN_ID": self.run_id, "ATEAM_AGENT": agent, **({"ATEAM_TASK": task} if task else {}), **(env_extra or {})}
        log = self.rundir / f"{n:03d}-{phase}-{task or 'project'}.jsonl"
        title = " - ".join(x for x in (task or self.project, agent, label) if x)
        self.record["current"] = {"agent": agent, "phase": phase, "task": task, "label": label, "since": now_iso(), "t0": time.time(), "steps": 0, "last": None, "session": session}
        self.save()
        try:
            res = OC.run_role(
                self.root, self.binary, agent, prompt, timeout, log, standalone=self.cfg["standalone"], session=session,
                retries=self.cfg["role_retries"], backoff_s=self.cfg["retry_backoff_s"], env_extra=env_extra,
                parent=self.parent, title=title, on_event=self.on_event,
            )
        finally:
            self.record["current"] = None
        entry = {"phase": phase, "task": task, **res.summary()}
        self.record["roles"].append(entry)
        t = self.record["totals"]
        t["role_calls"] += 1
        t["role_s"] = round(t["role_s"] + res.duration_s, 1)
        t["retries"] += max(0, res.attempts - 1)
        for k, v in res.tokens.items():
            t["tokens"][k] += v
        self.save()
        if res.tool_calls and res.tool_failures / max(1, res.tool_calls) >= 0.3 and res.tool_calls >= 5:
            self.signal("tool_failure_rate", task, f"{res.tool_failures}/{res.tool_calls} tool calls failed in {phase}", agent=agent, tools=res.tools)
        return res

    def check_provider(self, res: OC.RoleResult, task: str | None, phase: str) -> None:
        if res.fatal_provider_error or (res.errors and res.transient_error):
            self.signal("provider_error", task, f"{phase}: {res.errors[0]['message']}")
            raise Stop("provider_unavailable", task, res.errors[0]["message"], [rel(self.root, Path(res.log))] if res.log else [])

    # ------------------------------------------------------------------- phases
    def execute(self) -> int:
        try:
            self.begin()
            self.run_phases()
            return self.finish("completed", EXIT_OK)
        except Stop as stop:
            return self.escalate(stop)
        except Interrupted:
            self.record["status"] = "interrupted"
            self.say("interrupted; the current task stays in_progress. Resume with `ateam run --resume`.")
            return self.finish("interrupted", EXIT_INTERRUPTED)

    def begin(self) -> None:
        errors, _ = W.validate_project(self.root, self.project)
        if errors:
            raise Stop("invalid", None, errors[0], extra={"errors": errors[:10]})
        base = self.pdir / "state" / "run-baseline.json"
        if not base.exists():
            write_json(base, G.fingerprint_map(self.root))
        sp = self.pdir / "state" / "escalation.json"
        if sp.exists():
            sp.unlink()
        self.attach_parent()

    def run_phases(self) -> None:
        while True:
            self.run_tasks()
            self.integration()
            if self.cfg["final_review"] and not self.approved("review"):
                if self.review_final() == "rework":
                    continue
            if self.tester_enabled() and not self.approved("tester"):
                if self.test_final() == "rework":
                    continue
            return

    def tester_enabled(self) -> bool:
        return bool(self.cfg["adhoc_tester"] if self.adhoc else self.cfg["tester"])

    def run_tasks(self) -> None:
        done_now = 0
        while True:
            nxt = L.next_action(self.root, self.project)
            if nxt["action"] == "done":
                return
            if nxt["action"] == "stop":
                raise Stop(nxt["reason"], (nxt.get("tasks") or [None])[0], nxt.get("detail", nxt["reason"]), extra={"tasks": nxt.get("tasks")})
            self.do_task(nxt["task"])
            done_now += 1

    # ----------------------------------------------------------------- one task
    def do_task(self, tid: str) -> None:
        cfg = self.cfg
        task = self.task(tid)
        max_attempts = int(task.meta.get("max_attempts", cfg["max_attempts"]))
        rec = self.trec(tid)
        resuming = task.status == "in_progress"
        reuse = None
        if task.status in ("in_progress", "needs_review"):
            ok, _ = G.check_gate_valid(self.root, self.project, tid, allow_needs_review=True)
            if ok:
                reuse = read_json(G.gate_path(self.pdir, tid))
                self.say(f"{tid}: resuming with a gate that is still valid ({reuse['overall']})")
        while True:
            task = self.task(tid)
            if reuse is None:
                gate = self.attempt(task, max_attempts, resuming)
                resuming = False
            else:
                gate, reuse = reuse, None
            overall = gate["overall"]
            if overall in ("passed", "needs_review"):
                rp = B.report_path(self.pdir, task)
                if overall == "passed" and B.report_is_partial(rp):
                    self.signal("partial_report", tid, "gate passed but the report has no coder content")
                    self.after_fail(task, "partial_report", None, gate, "report sections are empty")
                    continue
                if task.meta.get("review") == "required" or overall == "needs_review":
                    if self.review_task(task, gate) == "rework":
                        continue
                G.complete_task(self.root, self.project, tid, reviewed=(overall == "needs_review"))
                self.update_state(tid, pending_review=None)
                fp = B.failure_path(self.pdir, tid)
                if fp.exists():
                    fp.unlink()
                rec["outcome"] = "completed"
                self.save()
                self.say(f"{tid}: completed (attempt {self.tstate(tid)['attempts']})")
                return
            sig = G.gate_failure_signature(gate)
            self.signal("gate_failed", tid, f"{overall}: " + ", ".join(c["check"] for c in gate["checks"] if c["status"] in ("failed", "not_executed")), signature=sig)
            if any(c["check"] == "test_guard" and c["status"] == "needs_review" for c in gate["checks"]):
                self.signal("test_guard", tid, "test guard fired")
            self.after_fail(task, f"gate_{overall}", sig, gate, "")

    def attempt(self, task: W.Task, max_attempts: int, resuming: bool) -> dict:
        tid = task.id
        rec = self.trec(tid)
        st = G.begin_attempt(self.root, self.project, tid, resume=resuming)
        attempt = st["attempts"]
        rec["attempts"] += 1
        self.record["totals"]["attempts"] += 1
        B.update_report(self.root, self.project, task, attempt)
        review = st.get("pending_review")
        review_file = (self.root / review) if review else None
        bundle = B.coder_bundle(self.root, self.project, task, attempt, max_attempts, resume=resuming, review_file=review_file)
        session = None
        if self.cfg["coder_session"] == "resume" and st.get("sessions"):
            session = st["sessions"][-1]
        self.say(f"{tid}: attempt {attempt}/{max_attempts} -> coder")
        t0 = time.time()
        # Only tasks that declare touches_gate (and therefore need a review) may run `quality set`.
        env_extra = {"ATEAM_ALLOW_QUALITY_SET": "1"} if task.meta.get("touches_gate") else None
        res = self.role("coder", bundle.prompt, self.cfg["task_timeout_s"], "coder", tid, session, env_extra, f"attempt {attempt}/{max_attempts}")
        rec["coder_s"] = round(rec["coder_s"] + res.duration_s, 1)
        if res.session:
            self.update_state(tid, sessions=(st.get("sessions") or []) + [res.session])
        try:
            self.check_provider(res, tid, "coder")
        except Stop:
            self.adjust_attempts(tid, -1)  # not the task's fault
            raise
        reason = None
        if res.timed_out:
            reason = "timeout"
            self.signal("timeout", tid, f"coder exceeded {self.cfg['task_timeout_s']}s")
        elif res.context_exhausted:
            reason = "context"
            self.signal("context_exhausted", tid, res.errors[0]["message"])
        elif res.exit_code != 0 or res.errors:
            reason = "role_error"
            self.signal("role_error", tid, (res.errors[0]["message"] if res.errors else f"exit {res.exit_code}"))
        if reason:
            B.update_report(self.root, self.project, task, attempt, None, time.time() - t0)
            self.after_fail(task, reason, None, None, res.errors[0]["message"] if res.errors else "")
            return self.attempt(self.task(tid), max_attempts, False)
        g0 = time.time()
        gate = G.run_gate(self.root, self.project, tid)
        gdur = time.time() - g0
        rec["gate_s"] = round(rec["gate_s"] + gdur, 1)
        self.record["totals"]["gate_s"] = round(self.record["totals"]["gate_s"] + gdur, 1)
        self.record["gates"].append({"task": tid, "attempt": attempt, "overall": gate["overall"], "duration_s": gate["duration_s"]})
        B.update_report(self.root, self.project, task, attempt, gate, time.time() - t0)
        self.say(f"{tid}: gate {gate['overall']} ({gate['duration_s']}s)")
        return gate

    def after_fail(self, task: W.Task, reason: str, signature: str | None, gate: dict | None, detail: str) -> None:
        tid = task.id
        dec = L.fail_task(self.root, self.project, tid, reason, signature, detail)
        st = self.tstate(tid)
        fp = B.write_failure_summary(self.pdir, tid, gate, reason, st["history"])
        self.say(f"{tid}: failed ({reason}) -> {dec['decision']}")
        if dec["repeat"] >= 2:
            self.signal("repeated_gate_failure", tid, f"{reason} repeated {dec['repeat']} times", signature=dec["signature"])
        if dec["decision"] == "retry":
            return
        self.trec(tid)["outcome"] = dec["decision"]
        ev = [rel(self.root, fp)]
        if gate:
            ev.append(rel(self.root, G.gate_path(self.pdir, tid)))
        if dec["decision"] == "needs_architect":
            self.signal("needs_architect", tid, f"same failure ({reason}) {dec['repeat']} times", signature=dec["signature"])
        else:
            self.signal("blocked", tid, f"attempts exhausted ({dec['attempt']}/{dec['max_attempts']})", signature=dec["signature"])
        raise Stop(dec["decision"], tid, reason, ev, {"decision": dec})

    # ------------------------------------------------------------------- review
    def run_reviewer(self, agent: str, bundle: B.Bundle, phase: str, task: str | None, label: str = "") -> str:
        """Run a verdict-producing role. One retry when no verdict can be parsed."""
        out = self.root / bundle.paths["review_file"]
        for attempt in (1, 2):
            res = self.role(agent, bundle.prompt, self.cfg["role_timeout_s"], phase, task, label=label)
            self.check_provider(res, task, phase)
            text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
            verdict = B.parse_verdict(text) or B.parse_verdict(res.text)
            if verdict:
                return verdict
            self.say(f"{phase}: no verdict (attempt {attempt})")
        self.signal("no_verdict", task, f"{agent} produced no parseable verdict twice")
        return "NONE"

    def review_task(self, task: W.Task, gate: dict) -> str:
        tid = task.id
        st = self.tstate(tid)
        rounds = st.get("review_rounds", 0) + 1
        max_rounds = int(self.cfg["max_review_rounds"])
        if rounds > max_rounds:
            W.set_task_status(self.root, self.project, tid, "blocked")
            self.signal("review_exhausted", tid, f"{max_rounds} review rounds used")
            raise Stop("review_exhausted", tid, f"{max_rounds} review rounds used", [])
        self.update_state(tid, review_rounds=rounds)
        self.trec(tid)["review_rounds"] = rounds
        needs = [f"{c['check']}: {c['reason']}" for c in gate["checks"] if c["status"] == "needs_review"]
        changed = G.changed_since_start(self.root, self.pdir, tid)
        bundle = B.reviewer_bundle(self.root, self.project, tid, [task], rounds, max_rounds, changed, [G.gate_path(self.pdir, tid)], needs)
        self.say(f"{tid}: review round {rounds}/{max_rounds} -> tech-lead")
        verdict = self.run_reviewer("tech-lead", bundle, "review", tid, f"review round {rounds}/{max_rounds}")
        if verdict == "APPROVED":
            return "approved"
        if verdict == "NONE":
            W.set_task_status(self.root, self.project, tid, "blocked")
            raise Stop("review_no_verdict", tid, "no verdict", [bundle.paths["review_file"]])
        self.say(f"{tid}: reviewer verdict {verdict}")
        self.update_state(tid, pending_review=bundle.paths["review_file"])
        self.adjust_attempts(tid, -1)  # review rework is bounded by review rounds, not by failed attempts
        W.set_task_status(self.root, self.project, tid, "gate_failed")
        return "rework"

    # ------------------------------------------------------------- final phases
    def final_path(self) -> Path:
        return self.pdir / "state" / "final.json"

    def final_state(self) -> dict:
        s = read_json(self.final_path(), {}) or {}
        s.setdefault("review_rounds", 0)
        s.setdefault("tester_rounds", 0)
        s.setdefault("review_digest", None)
        s.setdefault("tester_digest", None)
        return s

    def tasks_digest(self) -> str:
        parts = []
        for t in sorted(self.tasks(), key=lambda x: x.id):
            hist = [h.get("at") for h in self.tstate(t.id)["history"] if h.get("result") == "completed"]
            parts.append(f"{t.id}:{hist[-1] if hist else ''}")
        return sha256_text("|".join(parts))

    def approved(self, which: str) -> bool:
        return self.final_state().get(f"{which}_digest") == self.tasks_digest()

    def integration(self) -> None:
        tasks = self.tasks()
        cats: list[str] = []
        for t in tasks:
            for c in (t.meta.get("quality") if t.meta.get("quality") is not None else Q.DEFAULT_PROFILE):
                if c not in cats:
                    cats.append(c)
        if not cats:
            self.say("integration gate skipped: no quality categories required")
            return
        fp = G.fingerprint(self.root)
        gp = self.pdir / "state" / "integration.gate.json"
        prev = read_json(gp, None)
        if prev and prev.get("fingerprint") == fp and prev.get("overall") == "passed" and prev.get("categories") == cats:
            self.say("integration gate: reusing result for the unchanged tree")
            return
        t0 = time.time()
        status, results = Q.run_all(self.root, cats, False, self.pdir / "state" / "integration.logs", self.cfg["gate_timeout_s"])
        data = {"fingerprint": fp, "overall": status, "categories": cats, "duration_s": round(time.time() - t0, 1), "checks": results, "at": now_iso()}
        write_json(gp, data)
        self.record["gates"].append({"task": "integration", "overall": status, "duration_s": data["duration_s"]})
        self.record["totals"]["gate_s"] = round(self.record["totals"]["gate_s"] + data["duration_s"], 1)
        self.save()
        self.say(f"integration gate: {status} ({data['duration_s']}s)")
        if status != "passed":
            self.signal("integration_failed", None, ", ".join(r["check"] for r in results if r["status"] in ("failed", "not_executed")))
            raise Stop("integration_failed", None, status, [rel(self.root, gp)])

    def run_baseline_changes(self) -> list[tuple[str, str]]:
        base = read_json(self.pdir / "state" / "run-baseline.json", None)
        return G.diff_maps(base, G.fingerprint_map(self.root)) if base is not None else []

    def rework_from(self, review_file: str, text: str, why: str) -> None:
        by_id = W.task_index(self.tasks())
        refs = [r for r in B.review_task_refs(text) if r in by_id and by_id[r].status == "completed"]
        if not refs:
            order = [i for i in W.topo_order(self.tasks()) if by_id[i].status == "completed"]
            refs = order[-1:]
        for tid in refs:
            L.reopen_task(self.root, self.project, tid, why, review_file)
            self.update_state(tid, pending_review=review_file)
            self.say(f"{tid}: reopened by {why}")

    def review_final(self) -> str:
        fin = self.final_state()
        rounds = fin["review_rounds"] + 1
        max_rounds = int(self.cfg["max_review_rounds"])
        if rounds > max_rounds:
            self.signal("review_exhausted", None, "final review rounds exhausted")
            raise Stop("review_exhausted", None, f"{max_rounds} final review rounds used", [])
        fin["review_rounds"] = rounds
        write_json(self.final_path(), fin)
        tasks = self.tasks()
        bundle = B.reviewer_bundle(self.root, self.project, "final", tasks, rounds, max_rounds, self.run_baseline_changes(), [G.gate_path(self.pdir, t.id) for t in tasks], [])
        self.say(f"final review round {rounds}/{max_rounds} -> tech-lead")
        verdict = self.run_reviewer("tech-lead", bundle, "review", None, f"final review round {rounds}/{max_rounds}")
        if verdict == "APPROVED":
            fin["review_digest"] = self.tasks_digest()
            write_json(self.final_path(), fin)
            return "approved"
        if verdict == "NONE":
            raise Stop("review_no_verdict", None, "no verdict", [bundle.paths["review_file"]])
        text = (self.root / bundle.paths["review_file"]).read_text(encoding="utf-8", errors="replace") if (self.root / bundle.paths["review_file"]).exists() else ""
        fin["review_digest"] = fin["tester_digest"] = None
        write_json(self.final_path(), fin)
        self.rework_from(bundle.paths["review_file"], text, f"final review {verdict}")
        return "rework"

    def test_final(self) -> str:
        fin = self.final_state()
        rounds = fin["tester_rounds"] + 1
        max_rounds = int(self.cfg["max_review_rounds"])
        if rounds > max_rounds:
            self.signal("tester_exhausted", None, "tester rounds exhausted")
            raise Stop("tester_exhausted", None, f"{max_rounds} tester rounds used", [])
        fin["tester_rounds"] = rounds
        write_json(self.final_path(), fin)
        bundle = B.tester_bundle(self.root, self.project, rounds, max_rounds, self.run_baseline_changes(), self.tasks())
        self.say(f"tester round {rounds}/{max_rounds} -> tester")
        verdict = self.run_reviewer("tester", bundle, "tester", None, f"test round {rounds}/{max_rounds}")
        if verdict == "APPROVED":
            fin["tester_digest"] = self.tasks_digest()
            write_json(self.final_path(), fin)
            return "approved"
        if verdict == "NONE":
            raise Stop("review_no_verdict", None, "tester gave no verdict", [bundle.paths["review_file"]])
        text = (self.root / bundle.paths["review_file"]).read_text(encoding="utf-8", errors="replace") if (self.root / bundle.paths["review_file"]).exists() else ""
        fin["tester_digest"] = None
        write_json(self.final_path(), fin)
        self.rework_from(bundle.paths["review_file"], text, f"tester {verdict}")
        return "rework"

    # --------------------------------------------------------------- completion
    def counts(self) -> tuple[int, int]:
        ts = self.tasks()
        return sum(1 for t in ts if t.status == "completed"), len(ts)

    def finish(self, status: str, code: int) -> int:
        done, total = self.counts()
        self.record.update({"status": status, "finished": now_iso(), "exit_code": code, "wall_s": round(time.time() - self.t0, 1), "completed": done, "total": total})
        self.save()
        try:
            from . import feedback

            feedback.finish_run(self.root, self.record, self.pdir)
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.say(f"warning: run summary failed: {exc}")
        summary = {"run": self.run_id, "project": self.project, "status": status, "exit_code": code, "completed": f"{done}/{total}", "escalation": self.record["escalation"]}
        print(json.dumps(summary), flush=True)
        return code

    def escalate(self, stop: Stop) -> int:
        done, total = self.counts()
        data = {
            "run": self.run_id,
            "project": self.project,
            "kind": stop.kind,
            "task": stop.task,
            "reason": stop.reason,
            "progress": f"{done}/{total}",
            "evidence": stop.evidence,
            "detail": stop.extra,
            "hint": HINTS.get(stop.kind, ""),
            "at": now_iso(),
        }
        path = self.rundir / "escalation.json"
        write_json(path, data)
        write_json(self.pdir / "state" / "escalation.json", data)
        self.record["escalation"] = rel(self.root, path)
        self.say(f"STOP {stop.kind}: {stop.reason} (see {self.record['escalation']})")
        return self.finish(stop.kind, KIND_EXIT.get(stop.kind, EXIT_FAILED))


def rel(root: Path, p: Path) -> str:
    return B.rel(root, p)


# ---------------------------------------------------------------------- command
def acquire_lock(pdir: Path):
    (pdir / "state").mkdir(parents=True, exist_ok=True)
    fh = open(pdir / "state" / "run.lock", "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        die("another run is active for this project (state/run.lock is held)", 3)
    return fh


def _raise_interrupted(signum, frame):  # noqa: ARG001
    raise Interrupted()


def cmd_run(args) -> int:
    root = find_root(args.root)
    if args.adhoc is not None:
        request = sys.stdin.read() if args.adhoc == "-" else args.adhoc
        project, tid = W.create_adhoc(root, request, args.project)
        print(f"adhoc project {project} ({tid})", file=sys.stderr, flush=True)
        adhoc = True
    else:
        if not args.project:
            die("usage: ateam run <project> | ateam run --adhoc '<request>'")
        project, adhoc = args.project, False
        W.require_project(root, project)
        adhoc = any(t.meta.get("adhoc") for t in W.load_tasks(W.project_dir(root, project)))
    pdir = W.project_dir(root, project)
    lock = acquire_lock(pdir)
    mark_stale_runs(root, project)
    signal.signal(signal.SIGTERM, _raise_interrupted)
    signal.signal(signal.SIGINT, _raise_interrupted)
    try:
        return Driver(root, project, resume=args.resume, adhoc=adhoc).execute()
    finally:
        lock.close()


def latest_run(root: Path, project: str | None) -> dict | None:
    best = None
    for p in sorted(runs_dir(root).glob("RUN-*.json")):
        rec = read_json(p, None) if p.stat().st_size else None
        if rec and (project is None or rec.get("project") == project):
            best = rec
    if best and best.get("status") == "running" and not pid_alive(best.get("pid", 0)):
        best["status"] = "interrupted (driver not running)"
    return best


def cmd_status(args) -> int:
    root = find_root(args.root)
    project = args.project
    if project is None:
        names = sorted(p.name for p in W.work_root(root).iterdir() if p.is_dir()) if W.work_root(root).is_dir() else []
        emit({"projects": names}, args.json, "\n".join(names) or "(no projects)")
        return 0
    pdir = W.require_project(root, project)
    tasks = [t for t in W.load_tasks(pdir) if not t.errors]
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.status] = counts.get(t.status, 0) + 1
    run = latest_run(root, project)
    esc = read_json(pdir / "state" / "escalation.json", None)
    data = {
        "project": project,
        "counts": counts,
        "next": L.next_action(root, project),
        "run": {k: run.get(k) for k in ("id", "status", "started", "finished", "completed", "total", "exit_code")} if run else None,
        "escalation": esc,
    }
    if run and run.get("status") == "running":
        data["run"]["now"] = describe_current(run.get("current"))
    text = f"{project}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    if run:
        text += f" | last run {run['id']}: {run['status']}"
        if data["run"].get("now"):
            text += f" | now: {data['run']['now']}"
    if esc:
        text += f" | ESCALATION {esc['kind']}: {esc['reason']}"
    emit(data, args.json, text)
    return 0


def wait_state(root: Path, project: str | None) -> dict:
    run = latest_run(root, project)
    if run is None:
        return {"state": "none"}
    info = {k: run.get(k) for k in ("id", "project", "status", "completed", "total", "exit_code", "escalation")}
    st = str(run.get("status"))
    info["state"] = "running" if st == "running" else "finished"
    if info["state"] == "running":
        info["now"] = describe_current(run.get("current"))
        info["recent"] = run.get("recent") or []
    return info


def describe_current(cur: dict | None) -> str:
    """One line for the chef's poll output: which agent works on what, for how long, and its last action."""
    if not cur:
        return "driver between roles (gate or bookkeeping)"
    what = " ".join(x for x in (cur.get("agent"), "on", cur.get("task") or "the project", f"({cur['label']})" if cur.get("label") else "") if x)
    secs = int(time.time() - (cur.get("t0") or time.time()))
    line = f"{what}, {secs // 60}m{secs % 60:02d}s, {cur.get('steps', 0)} steps"
    if cur.get("last"):
        line += f", last: {cur['last']}"
    return line


def cmd_wait(args) -> int:
    """Block until the latest run (of a project) finishes or the timeout passes. One JSON line.

    Headless `opencode run` ends the session, and kills its background jobs, as soon as the agent stops talking, so the
    chef must keep calling this (each call blocks for at most --timeout seconds) until `state` is `finished`.
    """
    root = find_root(args.root)
    deadline = time.time() + max(1, args.timeout)
    info = wait_state(root, args.project)
    while info["state"] != "finished" and time.time() < deadline:
        time.sleep(2)
        info = wait_state(root, args.project)
    print(json.dumps(info), flush=True)
    return 0


def register(sub) -> None:
    w = sub.add_parser("wait", help="block until the latest run finishes (or --timeout seconds); prints one JSON line")
    w.add_argument("project", nargs="?")
    w.add_argument("--timeout", type=int, default=90)
    w.set_defaults(func=cmd_wait)
    p = sub.add_parser("run", help="run a work project with the deterministic driver")
    p.add_argument("project", nargs="?")
    p.add_argument("--adhoc", metavar="REQUEST", help="fast path: one-task project from a request ('-' reads stdin), no architect")
    p.add_argument("--resume", action="store_true", help="continue an interrupted run (in-progress tasks are resumed; valid gates are reused)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_run)
    s = sub.add_parser("status", help="compact project and run status")
    s.add_argument("project", nargs="?")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)
