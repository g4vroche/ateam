---
id: TASK-001
title: Establish quality tooling
status: pending
dependencies: []
priority: high
review: none
max_attempts: 3
files: [package.json, tsconfig.json]
reads: []
knowledge: []
skills: [quality-bootstrap]
quality: [format, lint]
---

# Objective
Inspect the repository and add only the missing quality tooling: formatter, linter, type checker and test runner, mapped in `.ateam/quality.json`.

# Files to Inspect
- package.json
- tsconfig.json

# Acceptance Criteria
- AC1: `quality detect` reports no missing category. [check: detect]
- AC2: Formatting and lint pass on the current tree. [check: format,lint]

# Verification Commands

```verify
detect: python3 .opencode/scripts/ateam.py quality detect --require format,lint,typecheck,test
```
