---
name: task-authoring
description: Write compact, atomic, verifiable task files for an ateam work project. Use when you create or fix files under .ateam/work/<project>/tasks, or when `work validate` reports task errors.
---
# Task authoring

Tasks are executed one at a time by a coder who sees ONLY its task file, the reports of its dependencies and the
files it lists. Write each task so that nothing else is needed. Every extra word costs output tokens.

## Workflow
1. `python3 .opencode/scripts/ateam.py quality detect` first. Missing categories in the profile mean TASK-001
   "Establish Quality Tooling" comes first (`touches_gate: true`, `review: required`, `skills: [quality-bootstrap]`).
2. `work init <project>`, then fill `plan.md` (every section non-empty).
3. `work new-task <project> "<title>" [--deps TASK-001,TASK-002]` for each task, then edit the file.
4. `work sync <project>` and `work validate <project>`. Fix every ERROR. Read each WARN. At most 3 fix rounds.

## Sizing
- One task = one coherent change a coder finishes in a single session: about 1 to 5 files, one behaviour.
- Split when a task has two independent acceptance outcomes or touches unrelated components.
- Keep the body under about 40 lines. Put long specs in a doc listed under `reads:` and refer to it.
- Add dependencies only when the later task needs files, interfaces or decisions of the earlier one.

## Required sections (all other sections only if they carry content)
`# Objective`, `# Files to Inspect`, `# Acceptance Criteria`, `# Verification Commands`.
Optional: `# Context`, `# Required Inputs`, `# Expected Changes`, `# Constraints`, `# Expected Deliverables`.

## Acceptance criteria and checks
- Format: `- AC1: <observable outcome> [check: id1,id2]` or `... [manual]`.
- Every criterion maps to a verify check id (or a quality category listed in `quality:`) or is tagged `[manual]`.
  Manual criteria go to the reviewer, so keep them few and put what to look at in the text.
- A criterion states an observable outcome, never an activity ("tests are written").

## Verification block
One fenced block, one `check-id: command` per line. Commands must be deterministic, non-interactive and fast,
run from the project root, and fail (non-zero) when the outcome is not met.
- Never `true`, `echo`, `:` or a command that can pass with zero tests. Prefer the narrowest test command.
- Use the project's real tools (see `quality detect`). Do not invent tools that are not installed.

## Frontmatter
`id`, `title`, `status: pending`, `dependencies`, `priority`, `review: none|required`, `max_attempts`,
`files` (scope the coder may touch), `reads` (docs to read), `knowledge` (KNOW ids), `skills` (explicit skill
ids), `quality` (categories checked by the gate; default format, lint, typecheck, test).
- `review: required` for security, data migrations, public API changes, concurrency, or gate/config changes.
- Set `touches_gate: true` (needs `review: required`) only for tasks that change `.ateam/quality.json`.
- List a skill under `skills:` only when this task needs it; each one is loaded into the coder's context.

## Examples
See `references/golden-task.md` for a full task, and `references/plan.md` for a plan.
