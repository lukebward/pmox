---
description: Run an arbitrary pmox CLI command and interpret the output.
argument-hint: "<pmox args>   e.g. vm status 100"
allowed-tools: Bash(pmox:*), Bash(python:*)
---

Run the `pmox` CLI with these arguments and explain the result:

```
pmox $ARGUMENTS
```

Guidelines:
- Default to read-only. Output is JSON automatically when captured (your shell
  captures it), so you can parse it directly; add `--no-json` only to show the
  human table.
- Include `--dangerous` (and `--yes` for destructive actions like
  `delete` / `stop` / `reset` / `migrate` / `rollback`) **only** if I explicitly
  asked to change or destroy something in the request above.
- If `pmox` is not on PATH, use `python -m pmox $ARGUMENTS`.
- If the command is blocked by the read-only gate (exit 4) or needs confirmation
  (exit 3), explain which flag is required and confirm with me before re-running.
