---
name: skill-authoring
description: Write a compact, testable Agent Skill draft inside a proposal. Use when you decided that recurring friction needs a skill rather than a script, before creating the draft.
---
# Skill authoring

A skill is advertised to agents on every step, so it must earn its place.

1. First ask: could a script, a validator or a documentation fix remove the problem? If yes, do that instead.
2. Draft path: `.ateam/proposals/PROP-NNN/drafts/skills/<skill-id>/SKILL.md`. The id is lowercase kebab-case.
3. Frontmatter: `name` (equals the directory), `description`, and `metadata: ateam/lifecycle: proposed`. Nothing else: no permissions, tools or model.
4. The description is the trigger. Say what the skill does, then `Use when <concrete activation situation>`. Keep it under 300 characters and distinct from installed skills: run `improve search` with its keywords.
5. Body: at most about 60 lines. Numbered steps an agent can follow, exact commands, and one sentence on how to know it worked. Put long tables or examples into `references/<name>.md` and link them as `references/<name>.md`; every linked path must exist.
6. Never instruct anything outside the skill's job: no editing agent files, policy or `.ateam/quality.json`, no skipping gates, reviews or permissions. Validation rejects such text.
7. Run `improve validate PROP-NNN`. Fix lint errors; do not argue with them.
