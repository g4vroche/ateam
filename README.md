# ateam

A small team of agents on [OpenCode](https://opencode.ai) that turns a request into tested, reviewed code. An LLM never decides what runs next: a deterministic driver does, so quality gates cannot be argued away.

```
/team Build a todo CLI with tests          # fast path or full plan, chosen by the Chef
python3 .opencode/scripts/ateam.py status   # where is it
```

## Install

```
python3 .opencode/scripts/ateam.py init --target /path/to/your/project    # copies .opencode/, creates .ateam/
python3 .opencode/scripts/ateam.py upgrade --target /path/to/your/project # later: never overwrites your edits
```

`init` writes a git-ignore block, `.ateam/config.json` and `.ateam/VERSION`. `upgrade` tracks installed file hashes: files you did not touch are updated, files you changed are kept (a changed file that also changed upstream gets the new version next to it as `<file>.new`; exit code 20). `--dry-run` shows the plan. Migration notes for your version range are printed ([`.opencode/MIGRATIONS.md`](.opencode/MIGRATIONS.md)).

Requirements: OpenCode 2.x with models `chrx-architect`, `chrx-coder`, `chrx-fast` on the `Anubis` provider (edit the `model:` lines in `.opencode/agents/*.md` for another setup), Python 3.12.

## How it works

| Agent | Model | Job | Cannot |
| --- | --- | --- | --- |
| `chef` | chrx-coder | triage, start the driver, handle escalations, report to you | write code, edit files |
| `architect` | chrx-architect | write `plan.md` and compact task files, validated by script | implement, run gates |
| `coder` | chrx-coder | implement exactly one task | complete tasks, edit task/gate/plan/`.opencode/` |
| `tech-lead` | chrx-architect | review the diff, verdict | edit source |
| `tester` | chrx-coder | adversarial test once per run, verdict | edit source |
| `skillsmith` | chrx-architect | turn recurring feedback into proposals (locked until G1) | install anything |
| `dreamer` | chrx-fast | consolidate durable knowledge into drafts (locked until G1) | write outside drafts |

**Standard path**: Chef → `architect` (writes `.ateam/work/<project>/`) → `python3 .opencode/scripts/ateam.py run <project>` in the background.
**Fast path** (single deliverable, no new component): `ateam.py run --adhoc "<request>"` creates a one-task project and skips the architect. `/team --fast` and `/team --plan` force a path.

### The driver (`ateam.py run`)

For every ready task: lock the task definition → fresh `coder` session → hardened gate → complete or record the failure. Then an integration gate, a final `tech-lead` review (max 3 rounds), and one `tester` pass.

- Failure handling: the same failure signature twice → `needs_architect`; `max_attempts` bounds retries → `blocked`. Only transient provider errors are retried inside a role.
- Resumable (`--resume`), single-run lock, roles die with the driver, wall-clock timeouts per task, role and gate.
- Exit codes: `0` completed, `10` blocked, `11` needs_architect (see `escalation.json`), `12` failed, `130` interrupted.

### Gate integrity

The gate runs `.ateam/quality.json` categories (format, lint, typecheck, test by default) plus the task's `verify:` checks. Guards that make "pass" mean something:

- worktree fingerprint and a task-hash lock (editing the task after `start` invalidates the gate);
- a quality.json hash lock (only `touches_gate` tasks may change the gate configuration);
- test guard (a diff that removes or skips tests fails), trivial-command rejection (`true`, `echo`), zero tests = failure;
- every acceptance criterion maps to `[check: id]` or `[manual]`; manual criteria go to the reviewer;
- the coder cannot edit its own task, the gate configuration or runtime state: this is enforced by permissions, not by prompt.

### Layout

```
.opencode/                 the product (agents, commands, skills, policy, scripts, templates, scheduling)
.ateam/work/<project>/     plan.md, tasks/, reports/, reviews/, manifest.json (derived), state/ (runtime)
.ateam/quality.json        gate configuration
.ateam/runs/               run records, logs, summaries (runtime)
.ateam/feedback/           captured events (runtime)   .ateam/knowledge/   store, INDEX.md, inbox (runtime)
.ateam/proposals/          SkillSmith proposals        .ateam/g1.json      gate G1 decision
```

Task files are the source of truth; `manifest.json` is derived (`work sync`). `ateam.py work migrate-legacy` archives the old `equipe/` folder (nothing is invented; `--move` renames it).

## Learning loop (locked until gate G1)

1. **Capture** (always on): the driver records signals (repeated gate failure, retries, blocked tasks) and agents may add at most 2 evidenced notes per task (`feedback note`; secrets, missing evidence and ordinary bugs are rejected). Each run writes `.ateam/runs/<RUN>/summary.md`.
2. **Gate G1**: `python3 .opencode/scripts/ateam.py g1` writes `.ateam/g1.json`. Go only if the benchmark shows a gain without lower correctness, at least 5 **real** runs produced events, and some signature recurs across runs. Until then `improve new|approve|promote|deprecate` and `knowledge dream` refuse to run (override with `"learning_enabled": true` in `.ateam/config.json`).
3. **SkillSmith** (`/improve`): `feedback query --open --group` → search installed skills and scripts → write `.ateam/proposals/PROP-nnn/`. `improve validate` lints drafts and runs their tests. Approval is enforced by `improve promote`, not by the prompt:

   | Change | Policy |
   | --- | --- |
   | docs, new tested scripts or validators | promoted automatically after validation |
   | skills | stop at `validated`; a human runs `improve approve PROP-x --by <name>` |
   | permissions, orchestration, prompts, commands, policy, quality gates | always need explicit approval; risk is computed from the drafts, a draft cannot declare itself low-risk |
   | architecture proposals | reported to the Chef, never promoted |
   | knowledge proposals | go to the knowledge inbox |

4. **Dreamer** (`/dream`, `ateam.py knowledge dream`): `knowledge collect` lists sources changed since the last checkpoint (content-hash ledger); if nothing is new there is **no model call**. Otherwise the Dreamer writes drafts to `.ateam/knowledge/drafts/` and files them with `knowledge upsert`, which enforces provenance, dedupes by key, flags contradictions instead of overwriting them (`knowledge resolve --keep` is human-only), rejects secrets and conversation dumps, and downgrades `verified` unless a source is a completed task's report or a user confirmation. The checkpoint is written only after the store lints clean. Scheduling is never installed for you: see `.opencode/scheduling/` (systemd user timer, crontab) and `.opencode/scripts/dream-cron.sh`.

## Configuration (`.ateam/config.json`)

Defaults live in `common.DEFAULT_CONFIG`; override any key.

| Key | Default | Meaning |
| --- | --- | --- |
| `max_attempts` / `signature_repeat_limit` | 3 / 2 | retries per task / identical failures before `needs_architect` |
| `task_timeout_s`, `role_timeout_s`, `gate_timeout_s` | 1800, 1200, 600 | wall-clock limits |
| `max_review_rounds` | 3 | final review rounds |
| `final_review`, `tester`, `adhoc_tester` | true, true, false | optional stages |
| `role_retries`, `retry_backoff_s` | 2, 20 | transient provider errors only |
| `max_task_chars` | 6000 | hard size limit of a task file |
| `skill_budget` | 8 | max installed skills (each costs description tokens on every step) |
| `feedback_notes_per_task` | 2 | cap on agent notes |
| `g1_min_runs`, `g1_min_bench` | 5, 2 | gate G1 evidence thresholds |
| `learning_enabled` | null | explicit override of the G1 lock |
| `retention` | state 30 d, feedback 90 d, inbox 14 d, 20 runs | used by `gc` |

`python3 .opencode/scripts/ateam.py gc` is a dry run; `--apply` deletes expired logs and baselines of completed projects, old runs, addressed feedback and already-dreamed inbox notes. It never touches task files, reports, reviews, open feedback, proposals or the knowledge store.

## Agent permissions and language

- `.opencode/policy/core.md` (about 300 words) is loaded into every agent through `.opencode/opencode.jsonc` → `instructions`: language rule (replies in your language, artifacts in English), integrity, secrets, scope. Agent prompts do not repeat it.
- `opencode run --auto` approves everything that is not explicitly denied, so every custom agent denies `.env` reads (`.env.example` stays readable) and lists its permissions explicitly. Shell allowlists resist chaining but are guardrails, not a sandbox.
- Agent frontmatter is the native V2 form (ordered `permissions`, last match wins). The legacy `temperature: 0.2` field of the original boilerplate was dropped (not part of the native V2 frontmatter); set sampling options in the provider or model configuration if you need them.
- Every difference from the original permissions is reviewed in `tests/golden/intended-changes.json`.

## Tests

```
bash tests/run.sh -q                       # static suite: fake opencode shim, no network, also run by CI
bash tests/run.sh -m llm tests/e2e/test_agents_llm.py::test_tester_writes_findings_with_verdict   # real model, opt-in, one at a time
python3 .opencode/scripts/ateam.py lint all   # agents (needs opencode), skills, work projects, knowledge store
```

Scenarios A–G (quality bootstrap, gate hardening, resume and recovery, feedback capture, SkillSmith, Dreamer, escalation) are covered in `tests/test_*.py`. CI (`.github/workflows/ci.yml`) runs the static suite and `ateam lint`; resolved-agent checks run only where `opencode` is on PATH.

## Benchmark

`bench/run_bench.py --variant baseline|current` runs the same request (`bench/tasks/todo-cli`: a small CLI with 17 hidden acceptance checks) through the original boilerplate (`bench/baseline-v0/`) and the current one in throwaway git workspaces, with the real models, one run at a time (the inference server is single-lane). `bench/compare.py --label final --runs` prints medians and deltas. Raw results are in `bench/results/` (git-ignored); `.ateam/g1.json` records the evaluation.

Final benchmark, 3 runs per variant (medians):

| | wall | input tokens | output tokens | steps | hidden checks passed | timeouts (40 min cap) |
| --- | --- | --- | --- | --- | --- | --- |
| baseline-v0 | 2400 s (capped) | 232k | 122k | 103 | 17/17 in every run | 3 of 3 |
| current | 344 s (331, 344, 756) | 79k | 17k | 25 | 17/17 in every run | 0 of 3 |
| delta | -86 % | -66 % | -86 % | -76 % | no change | |

Read these numbers carefully:

- The baseline never terminated inside the 40-minute cap in any run (its LLM-driven review and test loop keeps going after the code is already complete and correct). Its wall time, tokens and steps are therefore lower bounds, and the true gain is at least this large. The current system always finished.
- One task, one model setup, N = 3: this shows direction and size, not a statistical result. Run-to-run spread of the current system is large (331 s to 756 s).
- Token figures come from `opencode stats` for the workspace and include the driver's role sessions (coder, reviewer), not only the chef.
- The mid-point benchmark (2 current runs of the phase 4 code) scored 16/17 hidden checks: its quality-gate detection ran `unittest discover -s tests` without `-t .`, so the coder did not make `tests/` a package and the benchmark's own `-t .` check failed. That was fixed (detection keeps `-t .`, the coder prompt names the convention) and the final runs are 17/17. One earlier current run was discarded (kept in `bench/results/invalid/`): the chef ended its turn right after starting the background driver, which killed it; the chef now polls `ateam.py wait` until the run finishes.

## Gate G1 status

`python3 .opencode/scripts/ateam.py g1` was evaluated on 2026-11-07 and the decision is **NO-GO** for now:

- benchmark: met (gain in time and tokens, hidden-check pass rate not lower);
- at least 5 real runs with captured events: not met (this repository has no real runs of its own; the benchmark workspaces are discarded after each run);
- recurring feedback patterns across real runs: not met.

SkillSmith and the Dreamer are installed, unit-tested with a fake model and checked once end to end with the real model (`pytest -m llm`), but they stay locked until you have used the team on real work and `ateam.py g1` reports `go`. To experiment earlier, set `"learning_enabled": true` in `.ateam/config.json`.

## Security

- The global OpenCode config can hold a plaintext API key. This project never reads it. If a key was ever stored in plain text, rotate it.
- Notes, proposals and knowledge entries are scanned for secret patterns and rejected on a match.
