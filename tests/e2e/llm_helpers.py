"""Helpers for opt-in end-to-end tests that call the real model (pytest -m llm)."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def make_workspace(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    shutil.copytree(REPO / ".opencode", ws / ".opencode", ignore=shutil.ignore_patterns("__pycache__"))
    for rel, text in (files or {}).items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=ws, check=True, capture_output=True)
    return ws


def run_agent(ws: Path, agent: str, prompt: str, timeout: int = 900) -> dict:
    """Run one agent headlessly (own process group, killed on timeout). Returns {code, events, text}."""
    env = dict(os.environ, PWD=str(ws))
    proc = subprocess.Popen(
        ["opencode", "run", "--standalone", "--agent", agent, "--format", "json", "--auto", prompt],
        cwd=ws, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
    events = []
    for line in (out or "").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    text = "".join(e["part"].get("text", "") for e in events if e.get("type") == "text")
    return {"code": proc.returncode, "events": events, "text": text}
