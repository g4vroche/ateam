---
name: manual-orchestration
description: In-session fallback for running a work project through subagents. Use only when `ateam.py run` cannot start (for example it cannot launch opencode), never otherwise.
---
# Manual orchestration (fallback)

The driver is the default engine. Use this loop only when it cannot start. The gate and completion rules are the same:
the CLI refuses to complete a task unless the gate passed on the current tree.

Loop, one task at a time, no parallelism:
1. `python3 .opencode/scripts/ateam.py work next <project> --json`. `done` ends the loop; `stop` means escalate to the user.
2. `work start <project> <TASK>`.
3. Call subagent `coder` with: task file path, dependency report paths, scope, skills as `@skill-id`, attempt number.
   On a retry add the path `.ateam/work/<project>/state/<TASK>.failure.md`.
4. `work gate <project> <TASK>`. Exit 0 passed.
   - Passed: if the task has `review: required`, call `tech-lead` (findings file under `reviews/`, verdict on the last line). `CHANGES_REQUESTED`: go back to step 3. Then `work complete <project> <TASK>`.
   - Not passed: `work fail <project> <TASK> --reason gate_failed`. `retry` means go to step 3. `needs_architect` or `blocked` means stop and tell the user.
5. After the last task: `tech-lead` on the final diff (at most 3 rounds), then `tester` once.

Limits (enforced by you here, by the driver normally): at most 3 attempts per task, at most 3 review rounds, never a third identical failure.
Do not edit files yourself.
