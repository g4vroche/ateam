---
name: knowledge-lookup
description: Find stored project knowledge before deciding or writing a new entry. Use when you need prior decisions, conventions or fixes for this project.
---
# Knowledge lookup

1. `python3 .opencode/scripts/ateam.py knowledge search <keywords> [--category <c>]` returns at most 5 entries (id, title, path).
2. Read only the entries that match; ignore the rest. `hypothesis` entries are leads to check, `verified` entries are trustworthy.
3. Contradicted or deprecated entries are hidden by default. Never act on knowledge you have not read.
4. If the store has nothing, continue; do not write knowledge yourself. Record a durable fact with `knowledge note "<fact>" --source <where you learned it>`.
