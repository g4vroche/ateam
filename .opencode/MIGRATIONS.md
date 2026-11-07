# Migration notes

## 2.0.0 (from the original boilerplate)

- Orchestration moved from LLM-only Chef loops to the deterministic driver: `python3 .opencode/scripts/ateam.py run <project>`.
- Agents are now `architect`, `coder`, `tester`, `tech-lead`, `chef` (English). The French names `architecte`, `dev`, `testeur` remain as hidden aliases for one release.
- The `equipe/` folder is replaced by `.ateam/work/<project>/`. Run `python3 .opencode/scripts/ateam.py work migrate-legacy` to archive an existing `equipe/` (add `--move` to rename it).
- `.env` files are now denied to every agent (`opencode run --auto` approves `ask`).
- `policy/core.md` is loaded through `.opencode/opencode.jsonc` `instructions`. If your project already has an `opencode.jsonc`, add `"./.opencode/policy/core.md"` to its `instructions` array.
- SkillSmith (`/improve`) and the Dreamer (`/dream`) are installed but locked until `python3 .opencode/scripts/ateam.py g1` reports `go` (or `learning_enabled` is set in `.ateam/config.json`).
- Rotate any API key that was ever stored in plain text in a global OpenCode config; this project never reads it.
