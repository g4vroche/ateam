---
name: feedback-recording
description: Record a high-value experience note about a recurring tooling, instruction or environment problem. Use when the same obstacle cost you real time and a script or skill could have prevented it, never for ordinary bugs.
---
# Feedback recording

Notes feed later improvement of the team. They are expensive to read, so each one must be specific and evidenced.

Record a note only when ALL hold:
- the obstacle is about the tooling, instructions, environment or process, not the code you were asked to write;
- it cost you noticeable effort (several failed commands, a missing tool, a contradictory instruction);
- you can point at evidence (a file, a command and its output, a log path).

Limits: at most 2 notes per task. A note without a problem statement or evidence is rejected. Never include secrets,
tokens or credentials: the script rejects them.

Command (run from the project root):

```
python3 .opencode/scripts/ateam.py feedback note --category <cat> --severity <low|medium|high> \
  --signature <kebab-slug> --problem "<what blocked you>" --evidence "<path or command>" \
  [--workaround "<what finally worked>"] [--improvement "<what would prevent it>"]
```

- `--signature` names the problem class so repeated notes group together (for example `missing-test-runner`).
- Categories: tooling-gap, unclear-instructions, repeated-mistake, context-overload, testing-difficulty,
  documentation-gap, skill-deficiency, orchestration, dependency, environment, unnecessary-llm-call, other.
- A durable project fact (convention, decision) is not feedback: use
  `python3 .opencode/scripts/ateam.py knowledge note "<fact>" --source "<where you saw it>"`.
