"""Restricted YAML frontmatter parser (stdlib only).

Supported subset (everything else raises FrontmatterError):
  * file starts with a `---` line and a closing `---` line
  * `key: scalar`          (string, quoted string, int, true/false, null)
  * `key: [a, b, "c d"]`    (inline list of scalars; `[]` is empty)
  * `key:` + `  - item`     (block list of scalars)
  * `key:` + `  sub: value` (one-level map; values are scalars or inline lists)
  * full-line `#` comments and blank lines
No anchors, multi-line scalars, nested maps deeper than one level, or lists of maps.
"""
from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["FrontmatterError", "split", "parse", "dump", "parse_file"]


class FrontmatterError(ValueError):
    pass


_INT_RE = re.compile(r"^-?\d+$")
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_./-]*):(?:\s+(.*))?$")


def split(text: str) -> tuple[str | None, str]:
    """Return (frontmatter_text or None, body)."""
    if not text.startswith("---"):
        return None, text
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :]).lstrip("\n")
    raise FrontmatterError("frontmatter is not closed with '---'")


def _split_inline(s: str) -> list[str]:
    items, cur, quote = [], "", None
    for ch in s:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur += ch
        elif ch == ",":
            items.append(cur)
            cur = ""
        else:
            cur += ch
    if quote:
        raise FrontmatterError(f"unterminated quote in list: [{s}]")
    if cur.strip() or items:
        items.append(cur)
    return [i for i in items if i.strip() != ""]


def parse_scalar(raw: str) -> Any:
    s = raw.strip()
    if s in ("", "~", "null"):
        return None
    if s.startswith("[") and s.endswith("]"):
        return [parse_scalar(x) for x in _split_inline(s[1:-1])]
    if s.startswith('"'):
        if not s.endswith('"') or len(s) < 2:
            raise FrontmatterError(f"bad double-quoted string: {s}")
        try:
            return json.loads(s)
        except json.JSONDecodeError as exc:
            raise FrontmatterError(f"bad double-quoted string: {s}") from exc
    if s.startswith("'"):
        if not s.endswith("'") or len(s) < 2:
            raise FrontmatterError(f"bad single-quoted string: {s}")
        return s[1:-1].replace("''", "'")
    if s == "true":
        return True
    if s == "false":
        return False
    if _INT_RE.match(s):
        return int(s)
    if s.startswith(("{", "&", "*", "|", ">", "!")):
        raise FrontmatterError(f"unsupported YAML construct: {s}")
    return s


def parse(text: str) -> tuple[dict, str]:
    fm, body = split(text)
    if fm is None:
        raise FrontmatterError("missing frontmatter")
    return _parse_block(fm), body


def parse_file(path) -> tuple[dict, str]:
    with open(path, encoding="utf-8") as fh:
        return parse(fh.read())


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_block(fm: str) -> dict:
    lines = fm.split("\n")
    out: dict = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if "\t" in line[: _indent(line) + 1]:
            raise FrontmatterError(f"line {i + 1}: tabs are not allowed")
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if _indent(line) != 0:
            raise FrontmatterError(f"line {i + 1}: unexpected indentation")
        m = _KEY_RE.match(line)
        if not m:
            raise FrontmatterError(f"line {i + 1}: expected 'key: value', got: {line!r}")
        key, rest = m.group(1), m.group(2)
        if key in out:
            raise FrontmatterError(f"line {i + 1}: duplicate key '{key}'")
        i += 1
        if rest is not None and rest.strip() != "":
            out[key] = parse_scalar(rest)
            continue
        # collect indented children
        children: list[tuple[int, str]] = []
        while i < len(lines):
            nxt = lines[i]
            if not nxt.strip() or nxt.lstrip().startswith("#"):
                i += 1
                continue
            if _indent(nxt) == 0:
                break
            children.append((i + 1, nxt))
            i += 1
        if not children:
            out[key] = None
            continue
        first = children[0][1].lstrip()
        if first.startswith("- ") or first == "-":
            items = []
            for ln, c in children:
                s = c.lstrip()
                if not (s.startswith("- ") or s == "-"):
                    raise FrontmatterError(f"line {ln}: mixed list and map under '{key}'")
                item = s[1:].strip()
                if _KEY_RE.match(item) and not item.startswith(('"', "'")):
                    raise FrontmatterError(f"line {ln}: lists of maps are not supported")
                items.append(parse_scalar(item))
            out[key] = items
        else:
            sub: dict = {}
            for ln, c in children:
                mm = _KEY_RE.match(c.strip())
                if not mm:
                    raise FrontmatterError(f"line {ln}: expected 'key: value' under '{key}'")
                k2, r2 = mm.group(1), mm.group(2)
                if r2 is None or r2.strip() == "":
                    raise FrontmatterError(f"line {ln}: nesting deeper than one level is not supported")
                if k2 in sub:
                    raise FrontmatterError(f"line {ln}: duplicate key '{k2}' under '{key}'")
                sub[k2] = parse_scalar(r2)
            out[key] = sub
    return out


_NEEDS_QUOTE = re.compile(r"^(?:[\[\]{}&*!|>'\"%@`#,-]|\s)|(?:\s$)|(?:: )|(?: #)")


def _fmt_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    s = str(v)
    if (
        s == ""
        or s in ("~", "null", "true", "false")
        or _INT_RE.match(s)
        or _NEEDS_QUOTE.search(s)
        or "\n" in s
    ):
        return json.dumps(s, ensure_ascii=False)
    return s


def dump(meta: dict, body: str = "") -> str:
    out = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            if not v:
                out.append(f"{k}: []")
            elif all(isinstance(x, (int, bool)) or (isinstance(x, str) and len(x) < 30) for x in v) and len(v) <= 3:
                out.append(f"{k}: [" + ", ".join(_fmt_scalar(x) for x in v) + "]")
            else:
                out.append(f"{k}:")
                out.extend(f"  - {_fmt_scalar(x)}" for x in v)
        elif isinstance(v, dict):
            out.append(f"{k}:")
            for k2, v2 in v.items():
                if isinstance(v2, list):
                    out.append(f"  {k2}: [" + ", ".join(_fmt_scalar(x) for x in v2) + "]")
                else:
                    out.append(f"  {k2}: {_fmt_scalar(v2)}")
        else:
            out.append(f"{k}: {_fmt_scalar(v)}")
    out.append("---")
    text = "\n".join(out) + "\n"
    if body:
        text += "\n" + body.lstrip("\n")
    return text
