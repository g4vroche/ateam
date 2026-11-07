#!/usr/bin/env python3
"""Stand-in for `opencode run` that replays scripted behaviour (no model, no network).

Env: FAKE_OC_SCRIPT = JSON file {"rules": [...]}, FAKE_OC_STATE = directory for call logs and counters.
Each call picks the first rule whose `agent` (and optional `match`, a prompt substring) fits and that still has uses
left (`times`, default unlimited). Rule fields:
  write {path: text}  append {path: text}  delete [path]  sleep seconds  exit code  text "reply"
  error {type,message,status,retryAfterMs}  tokens {input,output}
  report "text"   -> fill `# Decisions Made` of the report path named in the prompt ("Your report: <path>")
  verdict "APPROVED|CHANGES_REQUESTED|FAILED" [+ findings "..."] -> write the review file named in the prompt
"""
import json
import os
import re
import sys
import time
from pathlib import Path


def emit(obj):
    print(json.dumps(obj), flush=True)


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] != "run":
        print("fake opencode: only `run` is supported", file=sys.stderr)
        return 2
    agent = argv[argv.index("--agent") + 1]
    session = argv[argv.index("-s") + 1] if "-s" in argv else None
    prompt = argv[-1]
    state = Path(os.environ["FAKE_OC_STATE"])
    state.mkdir(parents=True, exist_ok=True)
    script = json.loads(Path(os.environ["FAKE_OC_SCRIPT"]).read_text())
    counters_path = state / "counters.json"
    counters = json.loads(counters_path.read_text()) if counters_path.exists() else {}
    with open(state / "calls.jsonl", "a") as fh:
        fh.write(json.dumps({"agent": agent, "session": session, "prompt": prompt, "cwd": os.getcwd(), "pwd": os.environ.get("PWD"), "pid": os.getpid(), "allow_quality_set": os.environ.get("ATEAM_ALLOW_QUALITY_SET")}) + "\n")
    rule = None
    for i, r in enumerate(script["rules"]):
        if r.get("agent") not in (None, agent):
            continue
        if r.get("match") and r["match"] not in prompt:
            continue
        used = counters.get(str(i), 0)
        if r.get("times") is not None and used >= r["times"]:
            continue
        counters[str(i)] = used + 1
        counters_path.write_text(json.dumps(counters))
        rule = r
        break
    rule = rule or {}
    sid = session or f"ses_fake{os.getpid()}"
    emit({"type": "step_start", "sessionID": sid, "part": {"type": "step-start"}})
    if rule.get("announce"):
        Path(rule["announce"]).write_text("started")
    if rule.get("sleep"):
        time.sleep(rule["sleep"])
    for rel, text in (rule.get("write") or {}).items():
        p = Path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    for rel, text in (rule.get("append") or {}).items():
        with open(rel, "a") as fh:
            fh.write(text)
    for rel in rule.get("delete") or []:
        Path(rel).unlink(missing_ok=True)
    if rule.get("report"):
        m = re.search(r"Your report: (\S+)", prompt)
        if m and Path(m.group(1)).exists():
            p = Path(m.group(1))
            t = p.read_text()
            t = re.sub(r"# Decisions Made\n.*?\n# Verification Results", f"# Decisions Made\n{rule['report']}\n\n# Verification Results", t, flags=re.S)
            p.write_text(t)
    if rule.get("verdict"):
        m = re.search(r"Write (?:your )?findings to (\S+?)(?: and |\.\s|\s)", prompt) or re.search(r"findings to (\S+)", prompt)
        if m:
            p = Path(m.group(1).rstrip("."))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# Findings\n{rule.get('findings', '')}\n\nVERDICT: {rule['verdict']}\n")
    tk = rule.get("tokens") or {"input": 100, "output": 20}
    if rule.get("error"):
        e = rule["error"]
        emit({"type": "error", "sessionID": sid, "error": {"type": e.get("type", "provider.error"), "message": e.get("message", "boom"), "status": e.get("status"), "retryAfterMs": e.get("retryAfterMs")}})
    emit({"type": "text", "sessionID": sid, "part": {"type": "text", "text": rule.get("text", "done")}})
    emit({"type": "step_finish", "sessionID": sid, "part": {"type": "step-finish", "tokens": {"input": tk["input"], "output": tk["output"], "reasoning": 0, "cache": {"read": 0, "write": 0}}}})
    return rule.get("exit", 0)


if __name__ == "__main__":
    sys.exit(main())
