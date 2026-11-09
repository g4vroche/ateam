"""Shared helpers: project root discovery, atomic writes, locks, time, output."""
from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
OPENCODE_DIR = SCRIPTS_DIR.parent
TEMPLATES_DIR = OPENCODE_DIR / "templates"


def find_root(start: str | os.PathLike | None = None) -> Path:
    """Project root: $ATEAM_ROOT, else nearest ancestor with .ateam or .opencode, else cwd."""
    env = os.environ.get("ATEAM_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(start or os.getcwd()).resolve()
    for cand in [here, *here.parents]:
        if (cand / ".ateam").is_dir() or (cand / ".opencode").is_dir():
            return cand
    return here


def ateam_dir(root: Path) -> Path:
    return root / ".ateam"


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def today() -> str:
    return _dt.date.today().isoformat()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write_json(path: Path, data) -> None:
    atomic_write(path, json.dumps(data, indent=2, sort_keys=False, ensure_ascii=False) + "\n")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


@contextlib.contextmanager
def file_lock(path: Path):
    """Exclusive advisory lock on `path` (created if needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 40) -> str:
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    return s[:max_len].strip("-") or "item"


def load_template(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def emit(obj, as_json: bool, text: str | None = None) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False))
    else:
        print(text if text is not None else json.dumps(obj, indent=2, ensure_ascii=False))


DEFAULT_CONFIG = {
    "version": 1,
    "max_attempts": 3,
    "signature_repeat_limit": 2,
    "task_timeout_s": 1800,
    "gate_timeout_s": 600,
    "role_timeout_s": 1200,
    "max_review_rounds": 3,
    "standalone": True,
    "attach_to_chef": True,
    "coder_session": "fresh",
    "opencode_bin": "opencode",
    "max_task_chars": 6000,
    "skill_budget": 8,
    "feedback_notes_per_task": 2,
    "g1_min_runs": 5,
    "g1_min_bench": 2,
    "role_retries": 2,
    "retry_backoff_s": 20,
    "final_review": True,
    "tester": True,
    "adhoc_tester": False,
    "keep_failed_logs": True,
    "learning_enabled": None,
    "retention": {
        "state_days": 30,
        "feedback_addressed_days": 90,
        "inbox_days": 14,
        "keep_runs": 20,
    },
}


def load_config(root: Path) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    user = read_json(ateam_dir(root) / "config.json", {}) or {}
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def learning_enabled(root: Path) -> bool:
    """SkillSmith/Dreamer lock (gate G1). An explicit config value wins; otherwise `.ateam/g1.json` decides."""
    explicit = load_config(root).get("learning_enabled")
    if isinstance(explicit, bool):
        return explicit
    g1 = read_json(ateam_dir(root) / "g1.json", None)
    return bool(g1) and g1.get("decision") == "go"
