"""Headless role runs through `opencode run`: timeouts, process-group kill, event parsing, retries.

Rules learned in the spikes (tests/spikes.md): the project directory comes from $PWD, `--standalone` makes killing
the client kill the session, and the JSON stream carries per-step token counts.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

CONTEXT_HINTS = ("context length", "context window", "context_length", "maximum context", "too many tokens", "prompt is too long", "context exceeded")
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
FATAL_WAIT_MS = 5 * 60 * 1000


@dataclass
class RoleResult:
    agent: str
    exit_code: int | None = None
    timed_out: bool = False
    duration_s: float = 0.0
    sessions: list[str] = field(default_factory=list)
    text: str = ""
    tokens: dict = field(default_factory=lambda: {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0})
    steps: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    tools: dict = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    log: str | None = None
    attempts: int = 1  # process launches, including transient-error retries

    @property
    def session(self) -> str | None:
        return self.sessions[-1] if self.sessions else None

    @property
    def context_exhausted(self) -> bool:
        return any(any(h in (e.get("message") or "").lower() for h in CONTEXT_HINTS) for e in self.errors)

    @property
    def fatal_provider_error(self) -> bool:
        """Errors that retrying soon cannot fix (quota, auth, long retry-after)."""
        for e in self.errors:
            if (e.get("retry_after_ms") or 0) > FATAL_WAIT_MS or e.get("status") in (401, 402, 403):
                return True
        return False

    @property
    def transient_error(self) -> bool:
        if self.fatal_provider_error or self.context_exhausted:
            return False
        return any(e.get("status") in TRANSIENT_STATUS or e.get("type", "").startswith(("provider.", "network", "timeout")) for e in self.errors)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.errors

    def summary(self) -> dict:
        return {
            "agent": self.agent,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_s": round(self.duration_s, 1),
            "sessions": self.sessions,
            "tokens": self.tokens,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "tool_failures": self.tool_failures,
            "errors": self.errors[:3],
            "launches": self.attempts,
        }


def build_command(binary: str, agent: str, prompt: str, standalone: bool = True, session: str | None = None) -> list[str]:
    cmd = [*shlex.split(binary), "run"]
    if standalone:
        cmd.append("--standalone")
    cmd += ["--agent", agent, "--format", "json", "--auto"]
    if session:
        cmd += ["-s", session]
    cmd.append(prompt)
    return cmd


def _pdeathsig():
    """Kill the child if the driver dies (Linux), so an interrupted run never keeps editing files."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001
        pass


def _consume(line: str, res: RoleResult) -> None:
    try:
        e = json.loads(line)
    except ValueError:
        return
    if not isinstance(e, dict):
        return
    sid = e.get("sessionID")
    if sid and sid not in res.sessions:
        res.sessions.append(sid)
    t = e.get("type")
    part = e.get("part") or {}
    if t == "text":
        res.text += part.get("text", "")
    elif t == "tool_use":
        name = part.get("tool", "?")
        res.tool_calls += 1
        res.tools[name] = res.tools.get(name, 0) + 1
        state = part.get("state") or {}
        if state.get("status") == "error":
            res.tool_failures += 1
    elif t == "step_finish":
        res.steps += 1
        tk = part.get("tokens") or {}
        res.tokens["input"] += tk.get("input", 0) or 0
        res.tokens["output"] += tk.get("output", 0) or 0
        res.tokens["reasoning"] += tk.get("reasoning", 0) or 0
        cache = tk.get("cache") or {}
        res.tokens["cache_read"] += cache.get("read", 0) or 0
        res.tokens["cache_write"] += cache.get("write", 0) or 0
    elif t == "error":
        err = e.get("error") or {}
        resp = err.get("response") or {}
        res.errors.append(
            {
                "type": err.get("type", "error"),
                "message": str(err.get("message", ""))[:300],
                "status": err.get("status") or resp.get("status"),
                "retry_after_ms": err.get("retryAfterMs"),
            }
        )


def run_once(
    root: Path,
    binary: str,
    agent: str,
    prompt: str,
    timeout: int,
    log_path: Path | None = None,
    standalone: bool = True,
    session: str | None = None,
    on_start=None,
    env_extra: dict | None = None,
) -> RoleResult:
    res = RoleResult(agent=agent)
    cmd = build_command(binary, agent, prompt, standalone, session)
    env = dict(os.environ, PWD=str(root), **(env_extra or {}))
    t0 = time.time()
    logfh = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logfh = open(log_path, "a", encoding="utf-8")
        logfh.write(json.dumps({"driver": "start", "agent": agent, "cmd": cmd[:-1] + ["<prompt>"], "at": time.time()}) + "\n")
        res.log = str(log_path)
    try:
        proc = subprocess.Popen(
            cmd, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", start_new_session=True, preexec_fn=_pdeathsig,
        )
    except FileNotFoundError:
        res.exit_code = 127
        res.errors.append({"type": "launch", "message": f"cannot execute {binary!r}", "status": None, "retry_after_ms": None})
        if logfh:
            logfh.close()
        return res
    if on_start:
        on_start(proc)
    stderr_tail: list[str] = []

    def read_out():
        for line in proc.stdout:
            if logfh:
                logfh.write(line if line.endswith("\n") else line + "\n")
            _consume(line, res)

    def read_err():
        for line in proc.stderr:
            stderr_tail.append(line.rstrip())
            del stderr_tail[:-20]

    threads = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
    for th in threads:
        th.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        res.timed_out = True
        kill_group(proc)
    except BaseException:  # driver interrupted (SIGTERM/SIGINT): never leave the role running
        kill_group(proc)
        raise
    for th in threads:
        th.join(10)
    res.exit_code = proc.returncode
    res.duration_s = time.time() - t0
    if res.exit_code not in (0, None) and not res.errors and not res.timed_out:
        res.errors.append({"type": "exit", "message": f"exit {res.exit_code}: {' | '.join(stderr_tail[-3:])}"[:300], "status": None, "retry_after_ms": None})
    if logfh:
        logfh.write(json.dumps({"driver": "end", "exit_code": res.exit_code, "timed_out": res.timed_out, "duration_s": round(res.duration_s, 1)}) + "\n")
        logfh.close()
    return res


def kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue


def merge(into: RoleResult, other: RoleResult) -> RoleResult:
    other.attempts = into.attempts + 1
    for k in ("input", "output", "reasoning", "cache_read", "cache_write"):
        other.tokens[k] += into.tokens[k]
    other.steps += into.steps
    other.tool_calls += into.tool_calls
    other.tool_failures += into.tool_failures
    other.duration_s += into.duration_s
    other.sessions = into.sessions + [s for s in other.sessions if s not in into.sessions]
    return other


def run_role(
    root: Path,
    binary: str,
    agent: str,
    prompt: str,
    timeout: int,
    log_path: Path | None = None,
    standalone: bool = True,
    session: str | None = None,
    retries: int = 2,
    backoff_s: float = 20.0,
    on_start=None,
    sleep=time.sleep,
    env_extra: dict | None = None,
) -> RoleResult:
    """Run a role; retry only transient provider errors (not timeouts, not task failures)."""
    res = run_once(root, binary, agent, prompt, timeout, log_path, standalone, session, on_start, env_extra)
    tries = 0
    while res.transient_error and not res.timed_out and tries < retries:
        tries += 1
        sleep(backoff_s * tries)
        again = run_once(root, binary, agent, prompt, timeout, log_path, standalone, session, on_start, env_extra)
        res = merge(res, again)
    return res
