# Phase 1 spike findings (OpenCode v2.2.5)

All spikes were run on 2026-11-07 against the configured `Anubis` provider with `chrx-fast` and tiny prompts.

## S1 - Do `instructions` reach agents?

Yes. A project `.opencode/opencode.jsonc` with `"instructions": ["./.opencode/policy/core.md"]` made a
subagent-mode agent (run with `opencode run --agent`) answer from the policy text. Paths resolve from the
project root. `policy/core.md` is therefore loaded through `instructions`. Cost: it is part of every
agent's system context, so it stays short (token budget enforced by a test).

## S2 - `opencode run --format json`

Each line is one JSON event: `step_start`, `text`, `tool_use`, `step_finish`, with `sessionID` on every event.
`step_finish.part.tokens` has `input`, `output`, `reasoning`, `cache.read|write`. Summing `step_finish`
events gives per-run token usage with no extra tooling. Exit code is 0 on normal completion.
`--standalone` starts a private server for the run, so killing the process kills the session (the shared
background service would keep running the session after a client kill). The driver therefore defaults to
`--standalone` and enforces its own wall-clock timeout by killing the process group.
Minimal overhead observed: about 2k input tokens for a trivial prompt with the built-in `explore` agent.

## S3 - `opencode session export <id>`

JSON with `info` (id, agent, `tokens` totals incl. cache, `cost`, `outcome`, `time.busy|idle`, location) and a
`messages` array. Per-session token totals and busy time are available after a run, which gives
per-task numbers when each task runs in its own session (the driver records the session id from the stream).

## S4 - Historical `subagent` failures

5 of 15 historical `subagent` calls failed (lifetime stats). The retained log only keeps recent
sessions, almost all from OpenCode 1.17.x; no v2 failure cause could be recovered. Conclusion: do not rely
on the `subagent` tool for the main loop. The driver launches roles with `opencode run`, which has an explicit
exit code, JSON stream and a killable process.

## S5 - Permission semantics

Verified with a probe agent that allowed only `echo ok*` in the shell:
- `echo ok; echo X > file` was denied (the command is split and every part must match).
- `echo ok $(touch file)` was denied.
- Permission errors surface as `Permission denied: shell` in the tool result.
Shell allowlists therefore resist simple chaining and substitution. They remain guardrails, not a sandbox.
The V2 resolved rule list is ordered and the last match wins (see `opencode debug agents`).
Known v0 defect confirmed from the golden snapshot: `architecte` cannot write `equipe/etapes/*.md`.

## S6 - Skills

`@skill-id` in the prompt makes the agent load the skill through the `skill` tool (verified: the agent returned
the value defined only in the skill body). Per-agent `skill` rules (`"*": deny`, `"demo": allow`) were
accepted in agent frontmatter. Skills are advertised by description, so per-agent allowlists limit advertised
tokens.

## S7 - Reload churn for files under `.opencode/`

Not measured. The docs state that changes under watched config directories reload automatically; runtime data
is placed in `.ateam/` to avoid depending on the answer.

## S8 - Nested `opencode run`

A headless `opencode run --standalone` started from a shell works (verified from a plain shell). Starting it from
inside an agent's background shell is exercised by the optional `-m llm` end-to-end tests.

## Baseline benchmark

Harness: `bench/run_bench.py` + `bench/compare.py`. Results are written to `bench/results/` (git-ignored).

## S10 - Roles as child sessions of the chef (2026-11-08)

`opencode api session.create` accepts `parentID`, `agent`, `title` and `location`. `opencode run -s <that id>`
without `--standalone` reuses the pre-created session on the background service, keeps `parentID`, and still
streams `--format json` events; the child does not appear in `session list` (root sessions only), like a native
subagent. No environment variable gives a shell the id of its session: the driver finds the parent through
`session.active` + `session.get` (agent `chef`, no parent, same directory). On the shared service killing the client
does not stop the session, so the driver calls `session.interrupt` on timeout or interruption.

## S9 — `opencode run` takes its project directory from `$PWD`, not from the process cwd
`subprocess.Popen(..., cwd=X)` alone is not enough: with the parent's `PWD` inherited, `opencode session export`
reports `location.directory` = the parent's directory (verified: cwd only → repo root; cwd + `PWD=X` → X).
Consequence: the first baseline benchmark run wrote `equipe/PLAN.md` into this repository.
Rule for every spawn of `opencode` (bench harness, Phase 4 driver): pass `env=dict(os.environ, PWD=str(cwd))`
and verify `location.directory` after the run. Quality commands also get `PWD=<root>`.
