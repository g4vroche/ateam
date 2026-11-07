Build a small command-line todo manager in Python 3 (standard library only), in a file named `todo.py` at the project root.

Interface:
- Global option `--file PATH` selects the JSON storage file (default `todo.json` in the current directory). It must be accepted before the sub-command.
- `todo.py add TEXT`: appends a task, prints `Added #<id>: <TEXT>` and exits 0. Ids start at 1 and increase by 1. Empty or whitespace-only TEXT exits with code 2 and prints an error to stderr.
- `todo.py list`: prints one line per task as `<id>. [ ] <text>` (or `[x]` when done). With no tasks prints `No tasks.`.
- `todo.py done ID`: marks the task done, prints `Done #<id>` and exits 0. An unknown or non-numeric ID exits with code 1 and prints an error to stderr.
- If the storage file contains invalid JSON, every command exits with code 1 and a clear message on stderr (the file must not be overwritten).

Also add automated tests under `tests/` (at least 4 test functions or methods) that run with `python3 -m unittest` or pytest and cover the cases above. Verify everything by running it.
