---
id: TASK-002
title: Domain model
status: pending
dependencies: [TASK-001]
priority: high
review: none
max_attempts: 3
files: [src/domain/, tests/domain/]
reads: [docs/conventions.md]
knowledge: []
skills: []
quality:
  - format
  - lint
  - typecheck
  - test
---

# Objective
Implement the `Note` entity and its validation rules.

# Files to Inspect
- src/domain/
- docs/conventions.md

# Acceptance Criteria
- AC1: Invalid notes are rejected with typed errors. [check: unit]
- AC2: The domain layer compiles under strict typing. [check: typecheck]

# Verification Commands

```verify
unit: npm test -- tests/domain
```
