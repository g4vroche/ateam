---
description: Run the team on a request. Flags --fast (skip the architect) or --plan (force the architect).
agent: chef
---
Request: $ARGUMENTS

Apply the triage rules. If the request starts with `--fast`, use the fast path; if it starts with `--plan`, use the standard path. Strip the flag before passing the request on. Answer in the language of the request.
