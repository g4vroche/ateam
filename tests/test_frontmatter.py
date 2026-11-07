import pytest

from ateam import frontmatter as fm


def test_roundtrip_task_like():
    meta = {
        "id": "TASK-001",
        "title": "Do: something",
        "status": "pending",
        "dependencies": [],
        "files": ["a/b.py", "c.py"],
        "n": 3,
        "flag": True,
        "quality": ["format", "lint", "typecheck", "test"],
    }
    text = fm.dump(meta, "# Body\n")
    parsed, body = fm.parse(text)
    assert parsed == meta
    assert body.strip() == "# Body"


def test_inline_and_block_lists_and_maps():
    text = """---
a: [x, "y, z", 3]
b:
  - one
  - two
c:
  k1: v1
  k2: [1, 2]
# comment
d: ~
---
body"""
    meta, body = fm.parse(text)
    assert meta == {"a": ["x", "y, z", 3], "b": ["one", "two"], "c": {"k1": "v1", "k2": [1, 2]}, "d": None}
    assert body == "body"


def test_slash_keys_in_metadata_map():
    meta, _ = fm.parse("---\nmetadata:\n  ateam/lifecycle: active\n---\n")
    assert meta["metadata"]["ateam/lifecycle"] == "active"


@pytest.mark.parametrize(
    "bad",
    [
        "no frontmatter",
        "---\na: 1\n",  # unclosed
        "---\na: 1\na: 2\n---\n",  # duplicate
        "---\na:\n  b:\n    c: 1\n---\n",  # too deep
        "---\na:\n  - k: v\n---\n",  # list of maps
        "---\na: {x: 1}\n---\n",  # flow map
        "---\n\ta: 1\n---\n",  # tab
        "---\njust text\n---\n",
    ],
)
def test_rejects_unsupported(bad):
    with pytest.raises(fm.FrontmatterError):
        fm.parse(bad)


def test_strings_that_look_like_other_types_are_quoted():
    meta = {"a": "123", "b": "true", "c": "null", "d": "x: y", "e": ""}
    parsed, _ = fm.parse(fm.dump(meta))
    assert parsed == meta
