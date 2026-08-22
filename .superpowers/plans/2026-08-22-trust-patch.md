# pmox 0.7.2 Trust Patch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore pmox's documented error contract everywhere, make misconfiguration teach its fix, make `pmox health` catch real failures, and ship the small repairs — per `.superpowers/specs/2026-08-22-trust-patch-design.md`.

**Architecture:** All changes are surgical edits to the existing modules (`cli.py`, `config.py`, `errors.py`, `views.py`, `output.py`) plus doc/plugin sync. No new command surface, no new dependencies, no safety-model changes except failing faster and clearer.

**Tech Stack:** Python 3.11+, Typer (>=0.12, tested against 0.26 vendored-click), Rich, pytest with a **100% coverage gate**.

## Global Constraints

- Test suite is fully mocked (no live cluster) and enforces `--cov-fail-under=100` via `pyproject.toml` addopts. **Every new line must be covered.** Use `# pragma: no cover` only where the existing style already does (defensive except paths that genuinely can't be exercised).
- Running a *subset* of tests trips the coverage gate. For single-test iteration use `--no-cov`: `.venv\Scripts\python -m pytest tests/test_x.py --no-cov -q`. Before every commit run the FULL suite: `.venv\Scripts\python -m pytest`.
- This is Windows (PowerShell). The venv python is `.venv\Scripts\python.exe`. Run commands from the repo root `C:\Users\Luke\Workspace\pmox`.
- JSON-stdout purity: in JSON mode nothing but the JSON payload may reach stdout. Progress/notes go to stderr or human mode only.
- All static help text must be ASCII-only (enforced by a test added in Task 8). Runtime human output may use glyphs only via the `glyph()` helper (Task 8).
- Safety gates (`--dangerous`, `--yes`) are never weakened. `--dry-run` continues to need no gates.
- Three-way doc sync: any change to error codes or safety behavior updates `pmox/guide.py`, `plugin/skills/proxmox/SKILL.md`, and the `docs/` site pages in this release.
- Version strings live in TWO places: `pyproject.toml` and `pmox/__init__.py`. Task 10 bumps both to `0.7.2` plus `plugin/.claude-plugin/plugin.json`.
- Commit after each task with a conventional message (`fix:`, `feat:`, `docs:`, `test:`).

---

### Task 1: Parse-error boundary — catch the click typer actually raises

**Files:**
- Modify: `pmox/cli.py:34-37` (import), `pmox/cli.py:605-610` (app construction)
- Create: `tests/test_parse_errors.py`
- Create: `.github/workflows/test.yml`

**Interfaces:**
- Consumes: nothing.
- Produces: the module-level name `click` in `pmox/cli.py` now always refers to the click module typer parses with. All later tasks rely on parse errors emitting `{"ok": false, "error": "usage"}` with exit 2.

**Background (read first):** `pmox/cli.py:34-37` currently prefers the *real* `click` package and falls back to `typer._click`. Typer >= 0.26 parses with its vendored `typer._click` and raises *its* exception classes, so when real click is installed (e.g. via mkdocs), `except click.exceptions.ClickException` at `cli.py:2174` never matches and every parse error escapes as a Rich traceback with exit 1. The fix is to invert the preference — `click` is used ONLY for exception handling in cli.py (verified: lines 2088, 2102, 2171, 2174), so this is safe.

- [ ] **Step 1: Write the failing subprocess tests**

Create `tests/test_parse_errors.py`:

```python
"""Subprocess-level checks that parse errors emit the documented usage envelope.

These run ``python -m pmox`` as a real child process so the actual
import-time click resolution is exercised — the regression this guards
against (typer's vendored click vs the real package) is invisible to
in-process tests that import cli.py under a specific environment.
"""

import json
import os
import subprocess
import sys

import pytest


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pmox", *args],
        capture_output=True,
        text=True,
        env={**os.environ, "PMOX_JSON": "1"},
    )


@pytest.mark.parametrize(
    "argv",
    [
        ("nonexistent-cmd",),
        ("vm", "list", "--bogus"),
        ("--dangerous", "--yes", "vm", "delete", "100"),
        ("vm", "status"),
    ],
    ids=["unknown-command", "unknown-option", "misplaced-yes", "missing-argument"],
)
def test_parse_error_emits_usage_envelope(argv):
    proc = _run(*argv)
    assert proc.returncode == 2, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert payload["error"] == "usage"
    assert "hint" in payload
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_parse_errors.py --no-cov -q`
Expected: all 4 FAIL (returncode 1, stdout not JSON) — the venv has real click 8.4.2 via mkdocs, the trigger condition.

- [ ] **Step 3: Invert the click import and disable pretty exceptions**

In `pmox/cli.py` replace lines 34-37:

```python
try:  # typer >= 0.26 vendors click as typer._click — the module whose exceptions typer raises
    from typer import _click as click
except ImportError:  # older typer parses with the real click package
    import click
```

In the `typer.Typer(...)` construction (`cli.py:605-610`) add `pretty_exceptions_enable=False,` so anything that still escapes prints a plain traceback instead of a Rich wall.

- [ ] **Step 4: Run the new tests and the existing envelope test**

Run: `.venv\Scripts\python -m pytest tests/test_parse_errors.py tests/test_main.py tests/test_cli.py --no-cov -q`
Expected: PASS, including the previously-broken `test_main_unknown_command_json_envelope`.

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest`
Expected: PASS at 100% coverage.

- [ ] **Step 6: Add the CI workflow with a real-click leg**

Create `.github/workflows/test.yml`:

```yaml
name: Tests

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.11", "3.12"]
        click: [vendored-only, real-click]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: python -m pip install -e ".[dev]"
      - name: Install real click (the condition that broke the parse-error boundary)
        if: matrix.click == 'real-click'
        run: python -m pip install click
      - run: python -m pytest
```

- [ ] **Step 7: Commit**

```bash
git add pmox/cli.py tests/test_parse_errors.py .github/workflows/test.yml
git commit -m "fix(cli): catch the click typer actually raises so parse errors emit the usage envelope"
```

---

### Task 2: Config trust — typed coercion errors, explicit paths must exist, .env from cwd

**Files:**
- Modify: `pmox/config.py` (`_coerce` at 125-131, `load_settings` around 197-216, the missing-config message around 91-97)
- Modify: `pmox/cli.py:653-659` (dotenv loading)
- Test: `tests/test_config.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `ConfigError` (existing).
- Produces: `load_settings` raises `ConfigError` for bad numeric values and for explicitly-named missing config files. Message text used by Task 9's docs: `"Config file not found: {path}"`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_config.py` add (adapt fixture/helper names to the file's existing style — it already tests `load_settings` with env/tmp-path fixtures):

```python
def test_non_numeric_port_raises_config_error(monkeypatch):
    monkeypatch.setenv("PROXMOX_PORT", "abc")
    with pytest.raises(ConfigError, match="PROXMOX_PORT must be a number"):
        load_settings()


def test_non_numeric_timeout_in_toml_raises_config_error(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[proxmox]\ntimeout = "soon"\n')
    with pytest.raises(ConfigError, match="timeout must be a number"):
        load_settings(config_path=cfg)


def test_explicit_missing_config_path_errors(tmp_path):
    missing = tmp_path / "nope.toml"
    with pytest.raises(ConfigError, match="Config file not found"):
        load_settings(config_path=missing)


def test_explicit_missing_pmox_config_env_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("PMOX_CONFIG", str(tmp_path / "nope.toml"))
    with pytest.raises(ConfigError, match="Config file not found"):
        load_settings()


def test_implicit_default_config_path_may_be_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("PMOX_CONFIG", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # Must not raise about the file; missing required keys is a separate error.
    with pytest.raises(ConfigError, match="Missing required configuration"):
        load_settings()
```

Note: TOML values like `timeout = "soon"` arrive as `str`; TOML `port = 12.5` arrives as `float` — `int(12.5)` does NOT raise, so only string inputs exercise the ValueError path. That is fine; the env-var path is the live-verified failure.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_config.py --no-cov -q`
Expected: the new tests FAIL (ValueError traceback / no error raised).

- [ ] **Step 3: Implement the config.py changes**

Replace `_coerce` (config.py:125-131):

```python
def _coerce(source: dict, *, keys: dict) -> dict:
    """Pull recognised keys out of ``source`` applying the given coercion callables."""
    out: dict = {}
    for dest, (src_key, coerce) in keys.items():
        if src_key in source and source[src_key] not in (None, ""):
            try:
                out[dest] = coerce(source[src_key])
            except (TypeError, ValueError) as exc:
                kind = "a number" if coerce is int else "a valid value"
                raise ConfigError(
                    f"{src_key} must be {kind} (got {source[src_key]!r})."
                ) from exc
    return out
```

In `load_settings`, before the config file is read, enforce existence for *explicit* paths:

```python
explicit = config_path is not None or bool(os.environ.get("PMOX_CONFIG"))
path = config_path or default_config_path()
if explicit and not path.exists():
    raise ConfigError(f"Config file not found: {path}")
```

(Adapt to the function's actual local names; `_load_config_file`'s missing-file `return {}` stays — it now only ever applies to the implicit default path.)

In the missing-required-configuration message (config.py around 91-97), replace the sentence `See .env.example.` with `See https://lukebward.github.io/pmox/configuration/`.

- [ ] **Step 4: Switch .env discovery to the cwd**

In `pmox/cli.py` `main_callback` (lines 653-659), replace the dotenv block:

```python
    # Load a local .env if python-dotenv is available (never fatal). Discovery
    # walks up from the *cwd* — never from the installed package location — so
    # an editable install can't leak the repo's credentials into other dirs.
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
    except Exception:
        pass
```

Add an in-process test in `tests/test_cli.py` that monkeypatches a fake `dotenv` module recording the `find_dotenv` kwargs and asserts `usecwd=True` was passed (follow the file's existing monkeypatch style for `main_callback` tests). If `main_callback` is exercised via the Typer test runner in that file, patch `sys.modules["dotenv"]` with a stub before invoking.

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest`
Expected: PASS at 100% coverage. If the coercion `TypeError` branch is unreachable in tests, drop `TypeError` from the except tuple rather than adding a pragma.

- [ ] **Step 6: Commit**

```bash
git add pmox/config.py pmox/cli.py tests/test_config.py tests/test_cli.py
git commit -m "fix(config): typed coercion errors, explicit config paths must exist, .env from cwd"
```

---

### Task 3: Error taxonomy — `not_found` and `auth` codes

**Files:**
- Modify: `pmox/errors.py`, `pmox/views.py:19-24`, `pmox/cli.py` (`error_boundary` 412-446, `_emit_error` label dict 371-378)
- Modify: `pmox/guide.py` (error-envelope section, lines 41-49)
- Test: `tests/test_errors.py`, `tests/test_views.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `PmoxError` (existing).
- Produces: `pmox.errors.NotFoundError(PmoxError, LookupError)`; envelope codes `not_found` and `auth`, both exit 1. Task 9 documents them in SKILL.md/docs.

- [ ] **Step 1: Write the failing tests**

`tests/test_errors.py`:

```python
def test_not_found_error_is_pmox_and_lookup_error():
    err = NotFoundError("nope", extra={"hint": "re-list"})
    assert isinstance(err, PmoxError)
    assert isinstance(err, LookupError)
    assert err.extra == {"hint": "re-list"}
```

`tests/test_views.py`:

```python
def test_guest_not_found_returns_not_found_error():
    err = views.guest_not_found(999)
    assert isinstance(err, NotFoundError)
    assert "999" in str(err)
```

`tests/test_cli.py` (follow the file's existing error_boundary test style):

```python
def test_error_boundary_not_found_envelope(capsys):
    with pytest.raises(typer.Exit) as excinfo:
        with cli.error_boundary(json_output=True):
            raise NotFoundError("Guest 999 not found")
    assert excinfo.value.exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "not_found"


class _FakeAuthError(Exception):
    status_code = 401


def test_error_boundary_auth_envelope(capsys):
    with pytest.raises(typer.Exit) as excinfo:
        with cli.error_boundary(json_output=True):
            raise _FakeAuthError("401 Unauthorized: invalid token")
    assert excinfo.value.exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "auth"
    assert "hint" in payload


def test_error_boundary_auth_by_message_prefix(capsys):
    with pytest.raises(typer.Exit):
        with cli.error_boundary(json_output=True):
            raise RuntimeError("403 Forbidden")
    assert json.loads(capsys.readouterr().out)["error"] == "auth"
```

Also add human-mode label coverage (one test per new code calling `_emit_error` with `json_output=False`, matching how existing label cases are covered).

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_errors.py tests/test_views.py tests/test_cli.py --no-cov -q`
Expected: new tests FAIL (NotFoundError undefined; auth exits as "error").

- [ ] **Step 3: Implement**

`pmox/errors.py` — append:

```python
class NotFoundError(PmoxError, LookupError):
    """A guest/node/storage/task lookup found nothing. Not worth retrying —
    re-list (``pmox vm list`` / ``ct list`` / ``task list``) instead."""
```

`pmox/views.py:19-24` — `guest_not_found` returns `NotFoundError(...)` with the same message (import `NotFoundError` from `.errors`; keep the return-an-exception style).

`pmox/cli.py` — import `NotFoundError`; add a module-level helper near `_concise_network_reason`:

```python
_AUTH_HINT = (
    "Token rejected. token-id looks like user@realm!name and the secret is the "
    "token secret, not the account password. Note: an under-privileged token "
    "often shows as EMPTY lists, not errors."
)


def _is_auth_error(exc: BaseException) -> bool:
    """True for a Proxmox API authentication/authorization failure (401/403)."""
    if getattr(exc, "status_code", None) in (401, 403):
        return True
    return str(exc).lstrip().startswith(("401", "403"))
```

In `error_boundary`, add before `except PmoxError` (NotFoundError subclasses it, so order matters):

```python
    except NotFoundError as exc:
        _emit_error(json_output, "not_found", str(exc), 1, extra=exc.extra)
```

And change the final catch-all to classify auth first:

```python
    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        if _is_auth_error(exc):
            _emit_error(json_output, "auth", str(exc), 1, extra={"hint": _AUTH_HINT})
        _emit_error(json_output, "error", str(exc), 1)
```

In `_emit_error`'s label dict add: `"not_found": ("red", "Not found"),` and `"auth": ("red", "Auth error"),`.

`pmox/guide.py` — in the error-envelope section, change the codes line to:

```
  error codes: read_only | confirm_required | config | usage | auth | not_found | network | error
```

and add two bullets after the existing `network` bullet:

```
  - auth = the API token was rejected (401/403). Fix credentials; don't retry.
  - not_found = the vmid/upid/storage doesn't exist. Re-list instead of retrying.
```

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python -m pytest`
Expected: PASS at 100%. Watch for existing tests asserting `error == "error"` for not-found paths — update them to `not_found` (that is the point of the change).

- [ ] **Step 5: Commit**

```bash
git add pmox/errors.py pmox/views.py pmox/cli.py pmox/guide.py tests/
git commit -m "feat(errors): not_found and auth envelope codes"
```

---

### Task 4: Fail-fast confirm — no prompt into a captured stream

**Files:**
- Modify: `pmox/cli.py:294-296` (the single `confirm(...)` call site inside `_execute`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `safety.confirm(action, assume_yes, interactive, prompt_func)` and `safety.stdin_is_tty()` (both existing; check cli.py's `from .safety import ...` line and add `stdin_is_tty` to it).
- Produces: nothing new — behavior only.

- [ ] **Step 1: Write the failing test**

In `tests/test_cli.py` (using the file's existing pattern for driving a destructive command with a fake client — e.g. however `vm delete` without `--yes` is currently tested):

```python
def test_destructive_json_mode_fails_fast_even_with_tty_stdin(monkeypatch, ...):
    """JSON mode (captured stdout) + interactive stdin must NOT prompt: it
    must raise ConfirmationRequired immediately (exit 3, need: --yes)."""
    monkeypatch.setattr(cli, "stdin_is_tty", lambda: True)
    # invoke `--dangerous vm delete <vmid>` with JSON output on and no --yes;
    # assert exit code 3 and envelope error == "confirm_required".
```

Fill in with the concrete invocation helper the file already uses (runner/monkeypatched state). The key assertions: exit 3, `error == "confirm_required"`, and no prompt text on stdout.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_cli.py --no-cov -q -k fails_fast`
Expected: FAIL — with a TTY stdin, `confirm()` tries to prompt.

- [ ] **Step 3: Implement**

In `_execute` (cli.py:294-296) replace the confirm call:

```python
    if destructive:
        # Prompting is only safe when a human sees BOTH streams: a captured
        # stdout (JSON mode) with a TTY stdin would block forever on an
        # invisible prompt. Fail fast with exit 3 / need: --yes instead.
        interactive = (
            stdin_is_tty() and _stream_isatty(sys.stdout) and not state.json
        )
        confirm(confirm_msg or message, assume_yes=yes, interactive=interactive)
```

Add `stdin_is_tty` to cli.py's existing `from .safety import ...` line.

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python -m pytest`
Expected: PASS at 100%.

- [ ] **Step 5: Commit**

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "fix(safety): fail fast on destructive ops when output is captured instead of prompting invisibly"
```

---

### Task 5: Health triage — structured `issues` and four detectors

**Files:**
- Modify: `pmox/views.py:276-321` (`summarize_health`), `pmox/cli.py` (the `health` command's human rendering — find it near the other root commands)
- Test: `tests/test_views.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `client.cluster_status()`, `client.list_nodes()`, `client.cluster_resources(type=...)`, `client.list_tasks(node, limit=N)` (all existing).
- Produces: `summarize_health` result gains `"issues": [{"code", "severity", "node", "message"}]` and `"guests"` gains `"templates"`. `warnings` keeps all current strings and additionally gets one string per issue.

- [ ] **Step 1: Write the failing tests**

In `tests/test_views.py`, extend the existing fake-client fixtures for `summarize_health` (match the file's fixture style):

```python
def test_health_flags_lost_quorum_and_offline_node(...):
    # cluster_status -> [{"type": "cluster", "quorate": 0}, {"type": "node", "online": 0, ...}]
    # list_nodes -> [{"node": "pve1", "status": "offline", "cpu": 0, "mem": 0, "maxmem": 0}]
    result = views.summarize_health(client)
    codes = {i["code"] for i in result["issues"]}
    assert "quorum_lost" in codes
    assert "node_offline" in codes
    assert all(i["severity"] == "critical" for i in result["issues"] if i["code"] in ("quorum_lost", "node_offline"))
    # every issue is mirrored into warnings
    for issue in result["issues"]:
        assert issue["message"] in result["warnings"]


def test_health_flags_unavailable_storage(...):
    # cluster_resources(type="storage") row with "status": "unknown"
    assert any(i["code"] == "storage_unavailable" for i in result["issues"])


def test_health_flags_repeated_task_failures(...):
    # list_tasks returns 4 aptupdate rows, newest 3 with exitstatus != "OK",
    # older one "OK", plus an unrelated OK task type.
    issue = next(i for i in result["issues"] if i["code"] == "task_failures")
    assert "aptupdate" in issue["message"]
    assert "pmox task log UPID:" in issue["message"]


def test_health_excludes_templates_from_guest_counts(...):
    # cluster_resources(type="vm") with one template=1 row and two guests
    assert result["guests"] == {"running": 1, "stopped": 1, "templates": 1}


def test_health_no_issues_on_healthy_cluster(...):
    assert result["issues"] == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_views.py --no-cov -q`
Expected: FAIL (`issues` key missing).

- [ ] **Step 3: Implement in views.py**

Add a helper above `summarize_health`:

```python
def _task_failure_issues(client, node_name: str) -> list:
    """Issues for task types whose most recent 3+ finished runs on ``node_name``
    all failed (e.g. a nightly job that has been broken for days)."""
    by_type: dict = {}
    for t in client.list_tasks(node_name, limit=_RECENT_TASK_LIMIT):
        if t.get("status") == "running" or not t.get("type"):
            continue
        by_type.setdefault(t["type"], []).append(t)
    issues = []
    for task_type, rows in sorted(by_type.items()):
        rows.sort(key=lambda r: r.get("starttime") or 0, reverse=True)
        streak = 0
        for row in rows:
            if str(row.get("exitstatus", "")) == "OK":
                break
            streak += 1
        if streak >= 3:
            issues.append({
                "code": "task_failures",
                "severity": "warning",
                "node": node_name,
                "message": (
                    f"{node_name}: last {streak} {task_type} runs failed; "
                    f"pmox task log {rows[0].get('upid')}"
                ),
            })
    return issues
```

Rework `summarize_health`:

- Start `issues: list = []` alongside `warnings`.
- After reading `cluster`: `if not cluster.get("quorate", 0): issues.append({"code": "quorum_lost", "severity": "critical", "node": None, "message": "cluster has lost quorum"})`.
- Inside the node loop: `if n.get("status") != "online": issues.append({"code": "node_offline", "severity": "critical", "node": n.get("node"), "message": f"node {n.get('node')} is {n.get('status') or 'unknown'}"})`, and extend with `issues.extend(_task_failure_issues(client, n["node"]))` (only for online nodes — an offline node's task API is unreachable; guard with the same status check).
- Inside the storage loop: `status = s.get("status")` / `if status and status not in ("active", "available"): issues.append({"code": "storage_unavailable", "severity": "warning", "node": s.get("node"), "message": f"storage {s.get('storage')} on {s.get('node')} is {status}"})`.
- Guest counts:

```python
    vms = client.cluster_resources(type="vm")
    template_count = sum(1 for v in vms if v.get("template"))
    guests = [v for v in vms if not v.get("template")]
    running = sum(1 for v in guests if v.get("status") == "running")
```

  and return `"guests": {"running": running, "stopped": len(guests) - running, "templates": template_count}`.
- Before returning: `warnings.extend(i["message"] for i in issues)` and add `"issues": issues` to the returned dict.

- [ ] **Step 4: Render issues in the health command's human mode**

Find the `health` command in cli.py (it renders node/storage tables and warnings). After the existing warnings output add:

```python
        for issue in data["issues"]:
            color = "red" if issue["severity"] == "critical" else "yellow"
            console.print(f"[{color}]{issue['severity']}:[/{color}] {issue['message']}")
```

(Adapt the variable name to the command's local; if warnings are rendered from the same strings, ensure issues aren't double-printed — render `issues` lines and keep plain `warnings` rendering only for the non-issue pressure strings, i.e. render `[w for w in data["warnings"] if w not in {i["message"] for i in data["issues"]}]`.) Add a cli-level test asserting an issue line appears in human output.

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest`
Expected: PASS at 100%.

- [ ] **Step 6: Commit**

```bash
git add pmox/views.py pmox/cli.py tests/
git commit -m "feat(health): structured issues - quorum, offline nodes, unavailable storage, repeated task failures"
```

---

### Task 6: `pmox version` never fails

**Files:**
- Modify: `pmox/cli.py:693-698` (the `version` command)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `_client_factory`, `state.settings`, `_concise_network_reason` (existing).
- Produces: JSON shape `{"client": "<ver>", "server": {...}|null}` (+`"note"` when null); always exit 0.

- [ ] **Step 1: Write the failing tests**

```python
def test_version_reports_client_and_server(...):
    # fake client whose .version() returns {"version": "8.4.1"}
    # invoke `version` with JSON on; assert exit 0 and
    # payload == {"client": cli.__version__ ..., "server": {"version": "8.4.1"}}


def test_version_degrades_when_unconfigured(...):
    # settings missing required keys (validate() raises ConfigError)
    # assert exit 0, payload["server"] is None, "not configured" in payload["note"]


def test_version_degrades_when_unreachable(...):
    # client factory raises requests.exceptions.ConnectionError("boom")
    # assert exit 0, payload["server"] is None, "not connected" in payload["note"]
```

Use the file's existing runner + `_client_factory` monkeypatch pattern.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_cli.py --no-cov -q -k version`
Expected: new tests FAIL (current command exits 1 via error_boundary when unconfigured).

- [ ] **Step 3: Implement**

Replace the command (no `error_boundary` — this command must never fail):

```python
@app.command("version")
def server_version(ctx: typer.Context):
    """Show the pmox client version and, when reachable, the server's Proxmox VE version."""
    state: State = ctx.obj
    payload: dict = {"client": __version__, "server": None}
    try:
        state.settings.validate()
        client = state.client or _client_factory(state.settings)
        payload["server"] = client.version()
    except ConfigError:
        payload["note"] = "not configured - see https://lukebward.github.io/pmox/configuration/"
    except Exception as exc:  # noqa: BLE001 - version must always answer
        payload["note"] = f"not connected - {_concise_network_reason(exc)}"
    if state.json:
        print(json.dumps(payload, default=str, indent=2))
    elif payload["server"]:
        console.print(f"pmox {__version__} - server: Proxmox VE {payload['server'].get('version', '?')}")
    else:
        console.print(f"pmox {__version__} - server: {payload['note']}")
```

(ASCII hyphens on purpose — the help/output ASCII policy from Task 8. The spec's 5s probe is satisfied pragmatically: connection failures already surface quickly via `_concise_network_reason`, and shortening the HTTP timeout would require plumbing a per-call override — YAGNI for a patch. Note this deviation in the changelog wording: "best-effort".)

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python -m pytest`
Expected: PASS at 100%. Existing `version` tests asserting the old raw-server-dict shape must be updated to the new shape.

- [ ] **Step 5: Commit**

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "fix(cli): pmox version always answers - client version offline, server best-effort"
```

---

### Task 7: Surface fixes — full UPID column, drop `ct snapshot --vmstate`

**Files:**
- Modify: `pmox/cli.py:508-515` (TASK_COLUMNS), `pmox/cli.py` snapshot-create registration inside `build_guest_app` (the `--vmstate` option near line 1472)
- Test: `tests/test_cli.py`

**Interfaces:** none new.

- [ ] **Step 1: Write the failing tests**

```python
def test_task_table_shows_full_upid(...):
    # render TASK_COLUMNS (or invoke `task list --no-json` with a fake client)
    # with a 60+ char UPID; assert the full string is present in output.


def test_ct_snapshot_create_has_no_vmstate_flag():
    root = typer.main.get_command(cli.app)
    ct_snap_create = root.commands["ct"].commands["snapshot"].commands["create"]
    assert not any("--vmstate" in (p.opts or []) for p in ct_snap_create.params)


def test_vm_snapshot_create_keeps_vmstate_flag():
    root = typer.main.get_command(cli.app)
    vm_snap_create = root.commands["vm"].commands["snapshot"].commands["create"]
    assert any("--vmstate" in (p.opts or []) for p in vm_snap_create.params)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_cli.py --no-cov -q -k "upid or vmstate"`

- [ ] **Step 3: Implement**

- `TASK_COLUMNS`: replace the UPID entry with `Column("UPID", "upid")` (folds like every other column via `output.py`'s `overflow="fold"`).
- In `build_guest_app`, the snapshot `create` command is registered once for both kinds. Register the qemu variant with the `--vmstate` option and the lxc variant without it — the factory already receives the kind; either branch on it when building the command function, or define two thin wrappers sharing the implementation, with the lxc one never sending `vmstate`. Follow the factory's existing per-kind patterns (e.g. how vm-only commands are added).

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python -m pytest`
Expected: PASS at 100%.

- [ ] **Step 5: Commit**

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "fix(cli): full UPID in task list; drop unsupported --vmstate from ct snapshot create"
```

---

### Task 8: Windows encoding — UTF-8 streams, ASCII help, glyph fallback

**Files:**
- Modify: `pmox/cli.py` (`main()` at 2157, static help strings, glyph call sites at ~354, ~877, ~1392, ~1795), `pmox/output.py` (new `glyph()` helper)
- Test: `tests/test_output.py`, `tests/test_main.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `pmox.output.glyph(char: str) -> str`; `pmox.cli._force_utf8(stream) -> None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py` — the standing ASCII-help guard:

```python
def _iter_click_commands(cmd, path="pmox"):
    yield path, cmd
    for name, sub in (getattr(cmd, "commands", None) or {}).items():
        yield from _iter_click_commands(sub, f"{path} {name}")


def test_all_static_help_text_is_ascii():
    root = typer.main.get_command(cli.app)
    offenders = []
    for path, cmd in _iter_click_commands(root):
        texts = [("help", cmd.help or "")]
        texts += [(f"option {p.opts}", p.help or "") for p in getattr(cmd, "params", [])]
        for where, text in texts:
            if any(ord(ch) > 127 for ch in text):
                offenders.append(f"{path}: {where}")
    assert not offenders, f"non-ASCII help text in: {offenders}"
```

`tests/test_output.py`:

```python
def test_glyph_passes_through_when_encodable(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
    assert glyph("\u2713") == "\u2713"


def test_glyph_falls_back_on_cp1252(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    assert glyph("\u2713") == "OK"
    assert glyph("\u2192") == "->"
```

`tests/test_main.py`:

```python
def test_main_reconfigures_streams_to_utf8(monkeypatch):
    calls = []

    class _Stream:
        def reconfigure(self, **kwargs):
            calls.append(kwargs)
    monkeypatch.setattr(cli.sys, "stdout", _Stream())
    monkeypatch.setattr(cli.sys, "stderr", _Stream())
    # invoke main() with argv for a trivial exit (e.g. --version) via the
    # file's existing pattern; then:
    assert {"encoding": "utf-8", "errors": "replace"} in calls
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_output.py tests/test_main.py tests/test_cli.py --no-cov -q -k "ascii or glyph or utf8"`
Expected: FAIL — `glyph` undefined, help text contains `→`/`…`, no reconfigure calls.

- [ ] **Step 3: Implement**

`pmox/output.py` — add:

```python
_GLYPH_FALLBACKS = {"\u2713": "OK", "\u2192": "->", "\u00b7": "-", "\u2026": "..."}


def glyph(char: str) -> str:
    """``char`` when the stdout encoding can render it, else an ASCII stand-in.

    Human-mode niceties (checkmarks, arrows) must degrade on legacy Windows
    consoles instead of printing ``?``; JSON output is \\u-escaped and immune.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        char.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return _GLYPH_FALLBACKS.get(char, "?")
    return char
```

`pmox/cli.py`:

- Add near `main()`:

```python
def _force_utf8(stream) -> None:
    """Best-effort UTF-8 for Windows consoles and pipes (cp1252 by default)."""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - encoding setup must never kill the CLI
        pass
```

  and call `_force_utf8(sys.stdout)` / `_force_utf8(sys.stderr)` as the first two lines of `main()`.
- Sweep static help text: grep cli.py for non-ASCII (`[^\x00-\x7f]` regex) and replace every occurrence *inside help strings* with ASCII (`→` → `->`, `…` → `...`, `·` → `-`). Known sites: the `--json` option help ("auto → JSON") and the `vm set` help ("tags, …").
- Route runtime glyphs through the helper: `_ok`'s checkmark (line ~354) becomes `f"[green]{glyph('\u2713')}[/green] {message}"`; the `vm ip` separator (~877), and the two "Cloning X → Y" arrows (~1392, ~1795) likewise (import `glyph` from `.output`).

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python -m pytest`
Expected: PASS at 100%.

- [ ] **Step 5: Commit**

```bash
git add pmox/cli.py pmox/output.py tests/
git commit -m "fix(output): UTF-8 streams on Windows, ASCII-only help text, glyph fallback for legacy consoles"
```

---

### Task 9: Fill every blank help description

**Files:**
- Modify: `pmox/cli.py` (command registrations inside `build_guest_app` and the storage/cluster/task groups)
- Test: `tests/test_cli.py`

**Interfaces:** none new. Reuses `_iter_click_commands` from Task 8's test.

- [ ] **Step 1: Write the failing test**

```python
def test_every_command_has_help_text():
    root = typer.main.get_command(cli.app)
    blank = [path for path, cmd in _iter_click_commands(root)
             if getattr(cmd, "commands", None) is None and not (cmd.help or "").strip()]
    assert not blank, f"commands with no help text: {blank}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_cli.py --no-cov -q -k every_command_has_help`
Expected: FAIL listing ~19 commands.

- [ ] **Step 3: Add the help strings**

Inside `build_guest_app`, the factory knows the kind — define `label = "VM" if kind == "qemu" else "container"` and use `help=` kwargs (or docstrings, matching each registration's existing style). Exact strings:

| Command | Help |
|---|---|
| vm/ct `list` | `List all {label}s cluster-wide, or one node's with --node.` |
| vm/ct `status` | `Show the live status of a {label} (state, uptime, CPU/mem).` |
| vm/ct `config` | `Show the raw Proxmox config of a {label}.` |
| vm/ct `create` | `Create a bare {label} from raw API params (-o key=value). Prefer `new`/`up` for guided creation.` (for ct: `Prefer `new` for guided creation.`) |
| vm/ct `clone` | `Clone a {label} or template to a new VMID.` |
| vm/ct `migrate` | `Move a {label} to another node (destructive: requires --yes).` |
| vm/ct `delete` | `Permanently delete a {label} and its disks (destructive: requires --yes).` |
| snapshot `list` | `List snapshots of a {label}.` |
| snapshot `create` | `Create a snapshot of a {label}.` |
| snapshot `delete` | `Delete a snapshot (destructive: requires --yes).` |
| snapshot `rollback` | `Roll a {label} back to a snapshot (destructive: requires --yes).` |
| snapshot `-d/--description` option | `Free-form snapshot description.` |
| `storage list` | `List storages cluster-wide with capacity and content types.` |
| `storage content` | `List the volumes on one storage.` |
| `cluster status` | `Show cluster membership and quorum.` |
| `cluster resources` | `List cluster resources (guests, nodes, storage) with usage.` |
| `task list` | `List recent tasks on a node.` |
| `task status` | `Show the current status of a task by UPID.` |
| `task log` | `Print the log of a task by UPID.` |

Also fix the shared `ip` help so each kind reads naturally (currently one string describes both kinds parenthetically): vm variant `Show the live IP address(es) of a VM: guest agent, static cloud-init config, or a same-LAN ARP scan by MAC.`; ct variant `Show the live IP address(es) of a container via its network interfaces.` All strings are ASCII (Task 8's guard enforces this).

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python -m pytest`
Expected: PASS at 100%.

- [ ] **Step 5: Commit**

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "docs(cli): help text for every command, kind-aware for vm vs ct"
```

---

### Task 10: Doc sync, plugin version, changelog, release prep

**Files:**
- Modify: `plugin/.claude-plugin/plugin.json`, `plugin/skills/proxmox/SKILL.md`, `docs/safety.md`, `docs/agents.md`, `docs/configuration.md`, `docs/commands.md`, `docs/development.md`, `README.md`, `CHANGELOG.md`, `pyproject.toml`, `pmox/__init__.py`
- Test: `tests/test_cli.py` (or a new `tests/test_release_meta.py`)

**Interfaces:** consumes the error codes from Task 3 and the `version` shape from Task 6.

- [ ] **Step 1: Write the failing version-sync test**

Create `tests/test_release_meta.py`:

```python
import json
import pathlib
import tomllib

import pmox

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_versions_are_in_sync():
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    plugin = json.loads((_ROOT / "plugin/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
    assert pmox.__version__ == pyproject["project"]["version"]
    assert plugin["version"] == pyproject["project"]["version"]
```

Run: `.venv\Scripts\python -m pytest tests/test_release_meta.py --no-cov -q` — FAILS (plugin.json is 0.3.0).

- [ ] **Step 2: Bump versions**

- `pyproject.toml`: `version = "0.7.2"`
- `pmox/__init__.py`: `__version__ = "0.7.2"`
- `plugin/.claude-plugin/plugin.json`: `"version": "0.7.2"`

Re-run the test — PASSES.

- [ ] **Step 3: Sync the doc surfaces**

- `plugin/skills/proxmox/SKILL.md`: (a) error-code list gains `auth` and `not_found` with the same one-line meanings as guide.py; (b) the misplaced-`--yes` walkthrough (~lines 35-39) now says it fails with a JSON `usage` envelope, exit 2, whose hint points at `--help`; (c) the cheat-sheet `pmox version` entry notes it reports client + server and never fails; (d) "Check `ok` first" (~line 101) gains the caveat that read commands and dry-runs return bare data with no `ok` key.
- `docs/safety.md`: exit-code/error-code table gains `auth` and `not_found` (both exit 1); add one sentence that destructive commands fail fast with exit 3 instead of prompting when output is captured.
- `docs/agents.md`: update the error-code enumeration if it lists them.
- `docs/configuration.md`: document that `.env` is discovered from the current directory upward, and that an explicitly passed `--config`/`PMOX_CONFIG` path must exist.
- `docs/commands.md`: update the `pmox version` description (client + server, never fails).
- `README.md` + `docs/development.md`: add `guestops.py  SSH-into-guest operations for template build (the only module that reaches inside a guest)` to both architecture/module lists (drive-by accuracy fix — it is absent from both).

- [ ] **Step 4: Write the changelog entry**

Prepend to `CHANGELOG.md` following its keep-a-changelog style:

```markdown
## [0.7.2] - 2026-08-22

### Fixed
- Parse errors (unknown command/flag, missing argument, misplaced `--yes`) again emit the
  documented `{"error": "usage"}` JSON envelope with exit 2 — typer >= 0.26 vendors click,
  and the handler was catching the wrong module's exceptions, leaking Rich tracebacks.
- Non-numeric `PROXMOX_PORT`/`PROXMOX_TIMEOUT` values raise a config envelope (exit 2)
  instead of a raw traceback.
- An explicitly passed `--config`/`PMOX_CONFIG` file that does not exist is now an error
  instead of being silently ignored.
- Destructive commands fail fast (exit 3, `need: ["--yes"]`) instead of writing a
  confirmation prompt into a captured stream and blocking forever.
- Human output and help text no longer mojibake on cp1252 Windows consoles: streams are
  reconfigured to UTF-8, help text is ASCII-only, and runtime glyphs degrade gracefully.
- The human `task list` table shows the full UPID instead of truncating it to 48 chars.
- `ct snapshot create` no longer advertises `--vmstate`, which the LXC API does not support.
- `pmox health` guest counts no longer include templates (reported separately).

### Added
- Error codes `auth` (token rejected — fix credentials, don't retry) and `not_found`
  (re-list instead of retrying), alongside the existing six.
- `pmox health` now returns structured `issues` — lost quorum, offline nodes,
  unavailable storage, and task types whose last 3+ runs all failed — mirrored into
  `warnings` for existing consumers.
- Help text for every command (about half were blank), kind-aware for vm vs ct.
- A tests workflow (GitHub Actions) including a leg with the real `click` package
  installed — the condition that originally broke the parse-error boundary.

### Changed
- `.env` discovery now walks up from the current directory, never from the installed
  package location — an editable install no longer leaks the repo's credentials into
  unrelated directories, and pip installs finally honor a cwd `.env`.
- `pmox version` never fails: it always reports the client version and reports the
  server version best-effort (`{"client": ..., "server": ... | null}`), exit 0.
- The Claude Code plugin version now tracks the CLI version (was stuck at 0.3.0).
```

- [ ] **Step 5: Run the full suite and commit**

Run: `.venv\Scripts\python -m pytest`
Expected: PASS at 100%.

```bash
git add -A
git commit -m "Prep 0.7.2 release: bump versions, changelog, doc sync"
```

---

### Task 11: Release (gated on a fully green suite)

**Files:** none (git/GitHub operations only).

- [ ] **Step 1: Final full verification**

Run: `.venv\Scripts\python -m pytest`
Expected: PASS, coverage 100%. Do not proceed on anything less.

- [ ] **Step 2: Live smoke test (read-only)**

From the repo root against the real cluster (never `--dangerous`):

- `.venv\Scripts\pmox nonexistent-cmd` → exit 2, usage envelope
- `$env:PROXMOX_PORT='abc'; .venv\Scripts\pmox nodes list` → exit 2, config envelope (then remove the env var)
- `.venv\Scripts\pmox health` → `issues` includes the lukeserver `task_failures` entry (aptupdate)
- `.venv\Scripts\pmox version` → exit 0 with client+server
- `.venv\Scripts\pmox vm describe 99999` → `error: "not_found"`

- [ ] **Step 3: Push and publish**

```bash
git push origin main
gh release create v0.7.2 --title "pmox 0.7.2" --notes "Trust patch: restores the usage-envelope contract, config trust fixes, auth/not_found error codes, fail-fast confirm, health issue detectors (quorum, offline nodes, repeated task failures), full UPIDs, Windows UTF-8 output, and help text for every command. See CHANGELOG.md for details."
```

Publishing the GitHub release triggers `.github/workflows/publish.yml` (PyPI Trusted Publishing). Verify the Actions run succeeds and https://pypi.org/project/pmox/ shows 0.7.2.
