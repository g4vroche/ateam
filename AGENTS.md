# ateam: working on this repository

This repo is a boilerplate for a multi-agent team on OpenCode: `.opencode/` is the product, `tests/` and `bench/` verify it.

- Deterministic tooling: `python3 .opencode/scripts/ateam.py <area> ...` (stdlib-only Python 3.12). Entry point `.opencode/scripts/ateam.py`, modules in `.opencode/scripts/ateam/`.
- Tests: `bash tests/run.sh -q` (fake `opencode` shim, no network). Real-model checks are opt-in: `bash tests/run.sh -m llm tests/e2e/<file>`; run them one at a time, the inference server is single-lane.
- Before finishing a change: `python3 .opencode/scripts/ateam.py lint all` and `python3 .opencode/scripts/ateam.py agents sync-aliases --check`.
- Agent files in `.opencode/agents/` use native V2 frontmatter with an ordered `permissions` list (last match wins). `architecte.md`, `dev.md`, `testeur.md` are generated aliases: edit `architect.md`, `coder.md`, `tester.md` then run `agents sync-aliases`.
- Any permission change must be added, with a reason, to `tests/golden/intended-changes.json` (the permission-diff test fails otherwise). `opencode run --auto` approves everything not denied, so new agents must deny `.env` reads and be explicit.
- Universal agent rules live in `.opencode/policy/core.md` only. Do not repeat them in agent prompts; prompt token budgets are tested.
- Runtime data lives in `.ateam/` (git-ignored where it is runtime). Never commit secrets. The global OpenCode config may contain a plaintext API key: do not read, print or copy it.
