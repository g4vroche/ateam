# Golden task (valid, compact)

```markdown
---
id: TASK-003
title: Notes API endpoints
status: pending
dependencies: [TASK-002]
priority: medium
review: required
max_attempts: 3
files: [src/api/, tests/api/]
reads: [docs/conventions.md]
knowledge: []
skills: []
quality: [format, lint, typecheck, test]
---

# Objective
Expose create, read and delete endpoints for notes using the domain model from TASK-002.

# Files to Inspect
- src/domain/note.ts
- src/api/

# Acceptance Criteria
- AC1: POST /notes returns 201 with the created note, and 422 for an empty title. [check: api]
- AC2: GET and DELETE of an unknown id return 404. [check: api]
- AC3: Error responses share one JSON shape across endpoints. [manual]

# Verification Commands

```verify
api: npm test -- tests/api
```
```

Why it is good: one behaviour, explicit files, every criterion mapped to a check or `[manual]`, a runnable and
narrow verify command, optional sections left out.

# Golden TASK-001 (missing quality tooling)

Frontmatter: `touches_gate: true`, `review: required`, `quality: []`, `skills: [quality-bootstrap]`.
Verify: `detect: python3 .opencode/scripts/ateam.py quality detect --require format,lint,typecheck,test`.
