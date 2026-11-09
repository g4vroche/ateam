---
name: quality-bootstrap
description: Establish only the missing quality tooling (formatter, linter, type checker, test runner) and map it in .ateam/quality.json. Use when `quality detect` reports missing categories, or when you work on a task titled "Establish Quality Tooling".
---
# Quality bootstrap

Goal: after the task, `quality detect --require format,lint,typecheck,test` exits 0 using the project's native tools.

1. Run `python3 .opencode/scripts/ateam.py quality detect`. Only categories reported `missing` need work.
2. Use the ecosystem's standard tool, preferring what the project already depends on:
   - Python: `ruff` (format and lint), `mypy`, `pytest` or stdlib `unittest`.
   - Node/TypeScript: `prettier`, `eslint`, `tsc --noEmit`, the existing test runner.
   - Rust and Go: the built-ins (`cargo fmt|clippy|check|test`, `gofmt`, `go vet`, `go test`).
3. Install tools only through the project's own dependency manifest. Never touch global configuration.
4. If a category truly does not apply (for example no type checker for plain JavaScript), record it with a reason:
   `python3 .opencode/scripts/ateam.py quality set <category> --na "<reason>"`.
5. When the native tool is not auto-detected, map it:
   `python3 .opencode/scripts/ateam.py quality set <category> --cmd "<command>" [--changed-cmd "<cmd with {files}>" --exts py]`.
6. Run each category once (`quality format`, `quality lint`, ...) and fix real findings on the existing code only
   when they are trivial. Otherwise configure the tool so that the current tree passes and list the debt in the report.

Rules: configure what is missing and nothing else; add at least one real passing test if none exists; never weaken
an existing check to make it pass.
