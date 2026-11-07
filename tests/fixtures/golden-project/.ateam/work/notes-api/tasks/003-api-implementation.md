---
id: TASK-003
title: API implementation
status: pending
dependencies: [TASK-002]
priority: medium
review: required
max_attempts: 3
files: [src/api/, tests/api/]
reads: []
knowledge: []
skills: []
quality:
  - format
  - lint
  - typecheck
  - test
---

# Objective
Expose create, read and delete endpoints for notes using the domain model.

# Files to Inspect
- src/domain/
- src/api/

# Acceptance Criteria
- AC1: Endpoints return the documented status codes. [check: api]
- AC2: The error response format is consistent across endpoints. [manual]

# Verification Commands

```verify
api: npm test -- tests/api
```
