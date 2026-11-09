"""Skill lint shared by `ateam lint skills` (installed skills) and `ateam improve validate` (draft skills)."""
from __future__ import annotations

import re
from pathlib import Path

from . import frontmatter as fm
from . import schemas

_REF_RE = re.compile(r"`((?:references|scripts)/[A-Za-z0-9_./-]+)`")
MAX_LINES = 140
MAX_CHARS = 9000
OVERLAP_LIMIT = 0.6
# Frontmatter keys that would widen what a skill may do.
FORBIDDEN_META = ("permission", "permissions", "allowed-tools", "allowed_tools", "tools")
# Instructions a skill must never give: widening permissions, editing agent definitions or gates, bypassing the gate.
SCOPE_PATTERNS = [
    (re.compile(r"(?i)\b(edit|modify|change|write|overwrite)\b[^.\n]{0,40}(\.opencode/agents|opencode\.jsonc|policy/core\.md)"), "tells the agent to edit agent definitions or global policy"),
    (re.compile(r"(?i)\b(edit|modify|change|write|overwrite)\b[^.\n]{0,40}\.ateam/quality\.json"), "tells the agent to edit the quality gate configuration"),
    (re.compile(r"(?i)\b(skip|bypass|disable|ignore|weaken)\b[^.\n]{0,30}\b(gate|permission|quality check|test guard|review)\b"), "tells the agent to bypass a gate, review or permission"),
    (re.compile(r"(?i)\bwork\s+(complete|gate)\b[^.\n]{0,40}--reviewed"), "tells the agent to force-complete tasks"),
    (re.compile(r"(?i)\b(allow|grant)\b[^.\n]{0,30}\b(all|any)\b[^.\n]{0,20}\b(shell|command|edit)\b"), "widens permissions"),
]


def description_words(meta: dict) -> set[str]:
    return set(re.findall(r"[a-z]{4,}", str(meta.get("description", "")).lower()))


def lint_skill_dir(d: Path, others: dict[str, set[str]] | None = None) -> list[str]:
    """Errors for one skill directory. `others` maps other skill ids to their description words (overlap check)."""
    errs: list[str] = []
    sk = d / "SKILL.md"
    if not sk.exists():
        return [f"{d.name}: SKILL.md missing"]
    try:
        meta, body = fm.parse_file(sk)
    except fm.FrontmatterError as exc:
        return [f"{d.name}: frontmatter: {exc}"]
    errs.extend(f"{d.name}: {e}" for e in schemas.validate_skill(meta, body, d.name))
    for key in FORBIDDEN_META:
        if key in meta:
            errs.append(f"{d.name}: frontmatter key '{key}' is not allowed in a skill (skills cannot widen permissions)")
    for ref in _REF_RE.findall(body):
        if not (d / ref).exists():
            errs.append(f"{d.name}: referenced path {ref} does not exist")
    if len(body.split("\n")) > MAX_LINES:
        errs.append(f"{d.name}: body over {MAX_LINES} lines; move detail into references/")
    total = sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
    if total > MAX_CHARS * 4:
        errs.append(f"{d.name}: skill directory is {total} bytes; keep detail small")
    if len(body) > MAX_CHARS:
        errs.append(f"{d.name}: SKILL.md body is {len(body)} chars (cap {MAX_CHARS}); move detail into references/")
    text = body + "\n" + "\n".join(str(v) for v in meta.values() if isinstance(v, str))
    for pat, why in SCOPE_PATTERNS:
        if pat.search(text):
            errs.append(f"{d.name}: permission scope: {why}")
    words = description_words(meta)
    for other, ow in (others or {}).items():
        if other == d.name:
            continue
        union = len(words | ow) or 1
        sim = len(words & ow) / union
        if sim > OVERLAP_LIMIT:
            errs.append(f"{d.name}: overlaps skill {other} (description similarity {sim:.2f}); extend that skill instead")
    return errs


def installed_descriptions(root: Path) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    sdir = root / ".opencode" / "skills"
    if sdir.is_dir():
        for d in sorted(p for p in sdir.iterdir() if p.is_dir()):
            try:
                meta, _ = fm.parse_file(d / "SKILL.md")
            except (fm.FrontmatterError, OSError):
                continue
            out[d.name] = description_words(meta)
    return out


def lifecycle(meta: dict) -> str:
    return (meta.get("metadata") or {}).get("ateam/lifecycle", "active")
