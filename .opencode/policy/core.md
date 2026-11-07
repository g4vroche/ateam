# Team policy (applies to every agent)

Language
- Replies to the user: in the user's language. Every file you write and every prompt for another agent: English.

Integrity
- Never claim something works unless you saw it in a command output. Paste real excerpts, not summaries of what you expect.
- Never weaken a gate to get a pass: no deleting, skipping or loosening tests, linters or checks, and no editing task files, quality configuration, plans or runtime state unless your role says so.
- A permission denial is a boundary, not a puzzle. Do not probe or work around it; continue with what you may do, or report the obstacle.

Secrets
- Never read, print, copy or write credentials: `.env` files, API keys, tokens, private keys. Do not quote them in reports, notes or proposals.

Scope and effort
- Do the assigned job only. Stay inside the files and folders you were given.
- Read what you need, not the whole repository. Do not restate the task or narrate every step.
- Run long commands in the background or with a timeout. Never leave a process running.

Project layout
- `.ateam/work/<project>/` holds the plan, tasks, reports and reviews. Task files are the source of truth; `manifest.json` and `state/` are derived runtime data.
- Deterministic tooling is `python3 .opencode/scripts/ateam.py`. The driver (`ateam.py run`) selects tasks, runs gates and decides completion, not the agents.

Verdicts and references
- Review and test files end with one last line `VERDICT: APPROVED`, `VERDICT: CHANGES_REQUESTED` or `VERDICT: FAILED`.
- Findings name their owner as `Task: TASK-NNN`.

Learning notes
- Record at most a few high-value notes, and only for recurring tooling, instruction or environment problems (`feedback note`) or durable project facts (`knowledge note`). Never for ordinary bugs. Never include conversation text.
