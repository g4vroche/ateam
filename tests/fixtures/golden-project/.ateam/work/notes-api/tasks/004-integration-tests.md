---
id: TASK-004
title: Integration tests
status: pending
dependencies: [TASK-002, TASK-003]
priority: medium
review: none
max_attempts: 3
files: [tests/integration/]
reads: []
knowledge: []
skills: []
quality: [test]
---

# Objective
Add integration tests that exercise the API against the real domain layer.

# Files to Inspect
- tests/integration/
- src/api/

# Acceptance Criteria
- AC1: The full create-read-delete flow passes end to end. [check: integration]

# Verification Commands

```verify
integration: npm test -- tests/integration
```
