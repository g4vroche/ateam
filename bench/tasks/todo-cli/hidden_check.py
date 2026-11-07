"""Hidden acceptance checks for the todo-cli benchmark task (never shown to agents).

Usage: python3 hidden_check.py WORKSPACE_DIR  -> prints JSON {passed, total, details}
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ws = os.path.abspath(sys.argv[1])
todo = os.path.join(ws, "todo.py")
results = []


def check(name, fn):
    try:
        ok = bool(fn())
        results.append({"name": name, "ok": ok})
    except Exception as exc:  # noqa: BLE001
        results.append({"name": name, "ok": False, "error": str(exc)})


def run(args, store):
    return subprocess.run(
        [sys.executable, todo, "--file", store, *args], capture_output=True, text=True, cwd=ws, timeout=30
    )


with tempfile.TemporaryDirectory() as tmp:
    store = os.path.join(tmp, "t.json")
    check("todo.py exists", lambda: os.path.isfile(todo))
    check("list empty", lambda: run(["list"], store).stdout.strip() == "No tasks.")
    check("add prints id", lambda: run(["add", "buy milk"], store).stdout.strip() == "Added #1: buy milk")
    check("second id", lambda: run(["add", "write code"], store).stdout.strip() == "Added #2: write code")
    check(
        "list shows both",
        lambda: run(["list"], store).stdout.strip().splitlines() == ["1. [ ] buy milk", "2. [ ] write code"],
    )
    check("done marks", lambda: run(["done", "1"], store).stdout.strip() == "Done #1")
    check(
        "list shows done",
        lambda: run(["list"], store).stdout.strip().splitlines()[0] == "1. [x] buy milk",
    )
    check("persisted as json", lambda: isinstance(json.load(open(store)), (list, dict)))
    check("empty text exits 2", lambda: run(["add", "   "], store).returncode == 2)
    check("empty text stderr", lambda: bool(run(["add", ""], store).stderr.strip()))
    check("unknown id exits 1", lambda: run(["done", "99"], store).returncode == 1)
    check("non numeric id exits 1", lambda: run(["done", "abc"], store).returncode == 1)
    bad = os.path.join(tmp, "bad.json")
    open(bad, "w").write("{not json")
    check("corrupt json exits 1", lambda: run(["list"], bad).returncode == 1)
    check("corrupt json message", lambda: bool(run(["list"], bad).stderr.strip()))
    check("corrupt file untouched", lambda: (run(["add", "x"], bad), open(bad).read() == "{not json")[1])


def count_tests():
    n = 0
    tdir = os.path.join(ws, "tests")
    if not os.path.isdir(tdir):
        return 0
    for root, _, files in os.walk(tdir):
        for f in files:
            if f.endswith(".py"):
                n += len(re.findall(r"^\s*def test_", open(os.path.join(root, f)).read(), re.M))
    return n


check("at least 4 tests written", lambda: count_tests() >= 4)


def own_tests_pass():
    r = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        capture_output=True,
        text=True,
        cwd=ws,
        timeout=120,
    )
    out = r.stderr + r.stdout
    ran = re.search(r"Ran (\d+) test", out)
    if r.returncode == 0 and ran and int(ran.group(1)) > 0:
        return True
    r = subprocess.run([sys.executable, "-m", "pytest", "-q"], capture_output=True, text=True, cwd=ws, timeout=120)
    return r.returncode == 0


check("own tests pass", own_tests_pass)
passed = sum(1 for r in results if r["ok"])
print(json.dumps({"passed": passed, "total": len(results), "details": results}))
