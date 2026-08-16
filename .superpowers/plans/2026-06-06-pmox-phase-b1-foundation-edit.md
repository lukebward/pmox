# pmox Phase B1: Foundation + Edit Commands — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the cross-cutting agent ergonomics (`--wait`, `--dry-run`, position-independent global flags, machine-readable JSON errors) and the missing guest-edit commands (`set`, `resize`, `rename`, `tag`) to the `pmox` CLI.

**Architecture:** New code stays inside the existing clean module boundaries. `client.py` gains three thin single-endpoint methods. `cli.py` gains a `hoist_global_flags` argv shim, three global flags, a single `_execute` mutation-lifecycle helper (dry-run → dangerous gate → confirm gate → call → optional wait → ok envelope), a `_maybe_wait` poller, and JSON-aware error rendering; every existing mutation is retrofitted onto `_execute`; the new edit commands are added to the shared `build_guest_app` factory so they exist for both `vm` and `ct`. `safety.py` gains one helper. Tests follow the existing two-tier mock pattern; 100% coverage is held throughout.

**Tech Stack:** Python 3.11+, Typer (Click under the hood), proxmoxer (mocked in tests), Rich, pytest + pytest-cov.

**Source of truth:** `.superpowers/specs/2026-06-06-pmox-agent-native-design.md` (§3.1, §4, §5.1).

---

## Conventions for every task

- **Focused test runs** (red/green) disable coverage so a single test can run:
  `python -m pytest tests/test_x.py::test_name -v --no-cov`
- **Before every commit**, run the full suite (this enforces 100% coverage):
  `python -m pytest`
  Expected: all green, ending with `Required test coverage of 100% reached`.
- Activate the venv first (`.venv\Scripts\Activate.ps1` on Windows / `source .venv/bin/activate` elsewhere), or prefix commands with the venv's Python.
- Follow the existing code style in `cli.py`/`client.py` (typer options, closures over `kind`, terse docstrings).

---

## Task 1: `hoist_global_flags` argv shim + wire into `main()`

Makes global flags work in any position (e.g. `pmox vm set 100 -o cores=4 --dangerous`). Pure function = easy to cover.

**Files:**
- Modify: `pmox/cli.py` (add constants + function near the top, after imports; change `main()`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py`:

```python
def test_hoist_moves_bool_flag_before_subcommand():
    assert cli.hoist_global_flags(["vm", "set", "100", "--dangerous"]) == [
        "--dangerous", "vm", "set", "100",
    ]


def test_hoist_moves_value_flag_space_form():
    assert cli.hoist_global_flags(["vm", "list", "--timeout", "5"]) == [
        "--timeout", "5", "vm", "list",
    ]


def test_hoist_moves_value_flag_equals_form():
    assert cli.hoist_global_flags(["vm", "list", "--timeout=5"]) == [
        "--timeout=5", "vm", "list",
    ]


def test_hoist_leaves_command_options_in_place():
    # -o and --node are NOT global; they must stay after the subcommand.
    assert cli.hoist_global_flags(["vm", "set", "100", "-o", "cores=4", "--node", "pve1"]) == [
        "vm", "set", "100", "-o", "cores=4", "--node", "pve1",
    ]


def test_hoist_value_flag_at_end_without_value_is_kept():
    assert cli.hoist_global_flags(["vm", "list", "--timeout"]) == ["--timeout", "vm", "list"]


def test_hoist_stops_at_double_dash():
    assert cli.hoist_global_flags(["vm", "list", "--", "--dangerous"]) == [
        "vm", "list", "--", "--dangerous",
    ]


def test_hoist_noop_when_no_globals():
    assert cli.hoist_global_flags(["vm", "list"]) == ["vm", "list"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -k hoist -v --no-cov`
Expected: FAIL with `AttributeError: module 'pmox.cli' has no attribute 'hoist_global_flags'`

- [ ] **Step 3: Implement**

In `pmox/cli.py`, after the imports (around line 44, before `_client_factory`), add:

```python
# Global flags accepted in any position (hoisted to the front before Typer parses).
_GLOBAL_BOOL_FLAGS = frozenset(
    {
        "--json",
        "--no-json",
        "--dangerous",
        "--verify-ssl",
        "--no-verify-ssl",
        "--wait",
        "--no-wait",
        "--dry-run",
        "--version",
    }
)
_GLOBAL_VALUE_FLAGS = frozenset(
    {"--host", "--port", "--token-id", "--token-secret", "--config", "--timeout"}
)


def hoist_global_flags(argv: List[str]) -> List[str]:
    """Move recognised global flags (and their values) to the front of ``argv``.

    Typer puts global options on the root callback, which Click only accepts
    *before* the subcommand. This shim lets ``pmox vm set 100 --dangerous`` work
    by lifting known global flags ahead of the subcommand. Command-level options
    (``-o``, ``--node``, ``--size`` …) are left untouched. Anything after a bare
    ``--`` is passed through verbatim.
    """
    head: List[str] = []
    rest: List[str] = []
    i = 0
    passthrough = False
    while i < len(argv):
        tok = argv[i]
        if passthrough:
            rest.append(tok)
            i += 1
            continue
        if tok == "--":
            passthrough = True
            rest.append(tok)
            i += 1
            continue
        name = tok.split("=", 1)[0]
        if name in _GLOBAL_BOOL_FLAGS:
            head.append(tok)
            i += 1
        elif name in _GLOBAL_VALUE_FLAGS:
            head.append(tok)
            if "=" not in tok and i + 1 < len(argv):
                head.append(argv[i + 1])
                i += 2
            else:
                i += 1
        else:
            rest.append(tok)
            i += 1
    return head + rest
```

Then change `main()` at the bottom of the file:

```python
def main():
    app(args=hoist_global_flags(sys.argv[1:]))
```

- [ ] **Step 4: Update the existing `test_main_invokes_app` test**

Replace the existing `test_main_invokes_app` in `tests/test_cli.py` with:

```python
def test_main_invokes_app(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "app", lambda **kw: captured.update(kw) or captured.setdefault("ran", True))
    monkeypatch.setattr(cli.sys, "argv", ["pmox", "vm", "list", "--json"])
    cli.main()
    assert captured["ran"] is True
    assert captured["args"] == ["--json", "vm", "list"]
```

- [ ] **Step 5: Run focused tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -k "hoist or main_invokes" -v --no-cov`
Expected: PASS

- [ ] **Step 6: Run full suite + commit**

Run: `python -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Accept global flags in any position via hoist_global_flags shim"
```

---

## Task 2: Add `--wait` / `--timeout` / `--dry-run` global flags + State fields

**Files:**
- Modify: `pmox/cli.py` (`State.__init__`, `main_callback`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

The flags are observed end-to-end in later tasks; here we assert they parse and reach `State`. Add a tiny probe command guarded so it only exists under tests is overkill — instead assert via the dry-run envelope path that they're wired. Add:

```python
def test_global_flags_parse_without_error(fake_client, creds):
    # --wait/--timeout/--dry-run are accepted on the root callback (before subcommand).
    fake_client.list_nodes.return_value = []
    r = inv(["--wait", "--timeout", "5", "--dry-run", "nodes", "list"], creds)
    assert r.exit_code == 0, r.output
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_cli.py::test_global_flags_parse_without_error -v --no-cov`
Expected: FAIL with `No such option: --wait`

- [ ] **Step 3: Implement**

In `pmox/cli.py`, extend `State.__init__`:

```python
class State:
    def __init__(
        self,
        settings: Settings,
        json_output: bool = False,
        dangerous: bool = False,
        wait: bool = False,
        timeout: int = 600,
        dry_run: bool = False,
    ):
        self.settings = settings
        self.json = json_output
        self.dangerous = dangerous
        self.wait = wait
        self.timeout = timeout
        self.dry_run = dry_run
        self.client: Optional[ProxmoxClient] = None
```

In `main_callback`, add three options (after the `dangerous` option, before `host`):

```python
    wait: bool = typer.Option(
        False, "--wait/--no-wait", help="Wait for the resulting task to finish and report its outcome."
    ),
    timeout: int = typer.Option(600, "--timeout", help="Seconds to wait when --wait is set (default 600)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the intended API call as JSON and exit without changing anything."
    ),
```

And at the end of `main_callback`, update the `State` construction:

```python
    ctx.obj = State(
        settings=settings,
        json_output=json_on,
        dangerous=dangerous_on,
        wait=wait,
        timeout=timeout,
        dry_run=dry_run,
    )
```

- [ ] **Step 4: Run focused test to verify it passes**

Run: `python -m pytest tests/test_cli.py::test_global_flags_parse_without_error -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `python -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Add --wait/--timeout/--dry-run global flags and State fields"
```

---

## Task 3: `_maybe_wait` task poller

**Files:**
- Modify: `pmox/cli.py` (add `import time`; add `_POLL_SECONDS` + `_maybe_wait`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_maybe_wait_passes_through_non_upid():
    state = cli.State(settings=None)
    ctx = SimpleNamespace(obj=state)
    assert cli._maybe_wait(ctx, "pve1", {"already": "done"}) == {"already": "done"}


def test_maybe_wait_polls_until_stopped(monkeypatch):
    client = MagicMock()
    client.task_status.side_effect = [
        {"status": "running"},
        {"status": "stopped", "exitstatus": "OK"},
    ]
    state = cli.State(settings=None)
    state.client = client
    ctx = SimpleNamespace(obj=state)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    result = cli._maybe_wait(ctx, "pve1", "UPID:pve1:0001")
    assert result == {"status": "stopped", "exitstatus": "OK"}
    assert client.task_status.call_count == 2


def test_maybe_wait_times_out(monkeypatch):
    client = MagicMock()
    client.task_status.return_value = {"status": "running"}
    state = cli.State(settings=None, timeout=10)
    state.client = client
    ctx = SimpleNamespace(obj=state)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cli.time, "monotonic", iter([0.0, 1.0, 999.0]).__next__)
    with pytest.raises(TimeoutError):
        cli._maybe_wait(ctx, "pve1", "UPID:pve1:0001")
```

Add these imports at the top of `tests/test_cli.py` (alongside the existing imports):

```python
from types import SimpleNamespace
from unittest.mock import MagicMock
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_cli.py -k maybe_wait -v --no-cov`
Expected: FAIL with `AttributeError: module 'pmox.cli' has no attribute '_maybe_wait'`

- [ ] **Step 3: Implement**

Add `import time` to the imports block of `pmox/cli.py`. Then add near the other helpers (after `_resolve_node_or_die`):

```python
_POLL_SECONDS = 2


def _maybe_wait(ctx: typer.Context, node: str, result):
    """If ``result`` is a task UPID and --wait is set, poll until the task finishes.

    Returns the final task-status dict, or the original ``result`` if it is not a
    UPID. Raises ``TimeoutError`` if the task does not finish within --timeout.
    """
    if not (isinstance(result, str) and result.startswith("UPID:")):
        return result
    client = _get_client(ctx)
    deadline = time.monotonic() + ctx.obj.timeout
    while True:
        status = client.task_status(node, result)
        if status.get("status") == "stopped":
            return status
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Task {result} did not finish within {ctx.obj.timeout}s.")
        time.sleep(_POLL_SECONDS)
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -k maybe_wait -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `python -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Add _maybe_wait task poller for --wait"
```

---

## Task 4: Machine-readable errors + dry-run emitter

**Files:**
- Modify: `pmox/cli.py` (`error_boundary`, add `_emit_error`, `_emit_dry_run`; update every `with error_boundary():` call site to pass `ctx.obj.json`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_error_json_envelope_readonly(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--json", "vm", "start", "100"], creds)
    assert r.exit_code == 4, r.output
    payload = json.loads(r.output)
    assert payload == {
        "ok": False,
        "error": "read_only",
        "message": payload["message"],
        "need": ["--dangerous"],
    }


def test_error_json_envelope_confirm(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--json", "--dangerous", "vm", "delete", "100"], creds)
    assert r.exit_code == 3, r.output
    payload = json.loads(r.output)
    assert payload["error"] == "confirm_required"
    assert payload["need"] == ["--yes"]


def test_error_human_readonly_still_rich(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--no-json", "vm", "start", "100"], creds)
    assert r.exit_code == 4, r.output
    assert "Read-only" in plain(r.output)
    with pytest.raises(json.JSONDecodeError):
        json.loads(r.output)


def test_dry_run_emitter_shape(capsys):
    cli._emit_dry_run("vm.start", "pve1", {"a": 1})
    out = json.loads(capsys.readouterr().out)
    assert out == {"dry_run": True, "op": "vm.start", "node": "pve1", "params": {"a": 1}}
```

> Note: `test_missing_credentials_exit2` exercises the JSON `config` branch (the runner captures output, so JSON is the default). `test_error_human_readonly_still_rich` covers the human branch. Between the new tests and existing ones, all four error types are hit in both modes.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_cli.py -k "error_json or error_human or dry_run_emitter" -v --no-cov`
Expected: FAIL (old `error_boundary` prints Rich even under `--json`; `_emit_dry_run` missing).

- [ ] **Step 3: Implement**

Replace the existing `error_boundary` in `pmox/cli.py` with:

```python
def _emit_error(json_output: bool, error: str, message: str, code: int, need=None) -> None:
    if json_output:
        payload = {"ok": False, "error": error, "message": message}
        if need:
            payload["need"] = need
        print(json.dumps(payload, default=str, indent=2))
    else:
        label = {
            "read_only": ("yellow", "Read-only"),
            "confirm_required": ("yellow", "Aborted"),
            "config": ("red", "Config error"),
            "error": ("red", "Error"),
        }[error]
        err_console.print(f"[{label[0]}]{label[1]}:[/{label[0]}] {message}")
    raise typer.Exit(code)


def _emit_dry_run(op: str, node, params) -> None:
    print(json.dumps({"dry_run": True, "op": op, "node": node, "params": params or {}}, default=str, indent=2))


@contextmanager
def error_boundary(json_output: bool = False):
    """Translate exceptions into friendly messages and distinct exit codes."""
    try:
        yield
    except typer.Exit:
        raise
    except DangerousNotEnabled as exc:
        _emit_error(json_output, "read_only", str(exc), 4, need=["--dangerous"])
    except ConfirmationRequired as exc:
        _emit_error(json_output, "confirm_required", str(exc), 3, need=["--yes"])
    except ConfigError as exc:
        _emit_error(json_output, "config", str(exc), 2)
    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        _emit_error(json_output, "error", str(exc), 1)
```

Then update **every** `with error_boundary():` call site in `cli.py` to pass the JSON flag:

`with error_boundary():`  →  `with error_boundary(ctx.obj.json):`

There are call sites in: `server_version`, `nodes_list`, `nodes_status`, the factory's `_list`/`_status`/`_config`, `_make_power_command._cmd`, `_create`, `_clone`, `_migrate`, `_delete`, `_snap_list`, `_snap_create`, `_snap_delete`, `_snap_rollback`, `storage_list`, `storage_content`, `cluster_status`, `cluster_resources`, `task_list`, `task_status`, `task_log`. Update all of them. (The callback's own `ConfigError` handler stays as-is — JSON mode isn't resolved yet at that point.)

- [ ] **Step 4: Run focused + full suite to verify it passes**

Run: `python -m pytest tests/test_cli.py -k "error_json or error_human or dry_run_emitter" -v --no-cov`
Expected: PASS
Run: `python -m pytest`
Expected: all green, 100% coverage. (If a line of `_emit_error`'s human `label` map is uncovered, add a `--no-json` test for that error type — e.g. a `--no-json` confirm test.)

- [ ] **Step 5: Commit**

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Emit machine-readable JSON errors under --json; add dry-run emitter"
```

---

## Task 5: `_execute` mutation-lifecycle helper

**Files:**
- Modify: `pmox/cli.py` (add `_execute` after `_maybe_wait`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
def _exec_ctx(**overrides):
    state = cli.State(settings=None)
    for k, v in overrides.items():
        setattr(state, k, v)
    return SimpleNamespace(obj=state)


def test_execute_dry_run_prints_plan_and_skips_call(capsys):
    ctx = _exec_ctx(json=True, dangerous=False, dry_run=True)
    called = {"ran": False}
    cli._execute(
        ctx, op="vm.set", message="Set VM 100", node="pve1",
        call=lambda: called.__setitem__("ran", True), params={"cores": "4"},
    )
    assert called["ran"] is False
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["op"] == "vm.set" and out["params"] == {"cores": "4"}


def test_execute_runs_call_and_emits_ok(capsys):
    ctx = _exec_ctx(json=True, dangerous=True)
    result = cli._execute(
        ctx, op="vm.set", message="Set VM 100", node="pve1",
        call=lambda: "UPID:done", params={"cores": "4"},
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["result"] == "UPID:done"
    assert result == "UPID:done"


def test_execute_blocks_when_not_dangerous():
    ctx = _exec_ctx(json=True, dangerous=False)
    with pytest.raises(cli.DangerousNotEnabled):
        cli._execute(ctx, op="vm.set", message="m", node="pve1", call=lambda: "x")


def test_execute_destructive_requires_yes():
    ctx = _exec_ctx(json=True, dangerous=True)
    with pytest.raises(cli.ConfirmationRequired):
        cli._execute(
            ctx, op="vm.delete", message="m", node="pve1",
            call=lambda: "x", destructive=True, yes=False, confirm_msg="delete 100",
        )


def test_execute_waits_when_requested(monkeypatch):
    client = MagicMock()
    client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    ctx = _exec_ctx(json=True, dangerous=True, wait=True)
    ctx.obj.client = client
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    result = cli._execute(ctx, op="vm.start", message="m", node="pve1", call=lambda: "UPID:x")
    assert result == {"status": "stopped", "exitstatus": "OK"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_cli.py -k execute -v --no-cov`
Expected: FAIL with `AttributeError: module 'pmox.cli' has no attribute '_execute'`

- [ ] **Step 3: Implement**

Add to `pmox/cli.py` after `_maybe_wait`:

```python
def _execute(
    ctx: typer.Context,
    *,
    op: str,
    message: str,
    node,
    call,
    params=None,
    destructive: bool = False,
    yes: bool = False,
    confirm_msg: Optional[str] = None,
):
    """Run a single state-changing operation through the full safety lifecycle.

    Order: dry-run preview (no gates) → require --dangerous → confirm if
    destructive → run ``call`` → optionally wait on the task → emit the ok
    envelope. ``call`` is a zero-arg callable returning the client result.
    """
    state: State = ctx.obj
    if state.dry_run:
        _emit_dry_run(op, node, params)
        return None
    require_dangerous(state.dangerous)
    if destructive:
        confirm(confirm_msg or message, assume_yes=yes)
    result = call()
    if state.wait:
        result = _maybe_wait(ctx, node, result)
    _ok(ctx, message, result)
    return result
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -k execute -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `python -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Add _execute mutation-lifecycle helper (dry-run, gates, wait, ok)"
```

---

## Task 6: Retrofit power commands onto `_execute`

Routes the seven power actions through `_execute` so they gain `--dry-run`/`--wait` while preserving existing messages and behavior.

**Files:**
- Modify: `pmox/cli.py` (`_make_power_command`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_power_dry_run_skips_call(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dry-run", "vm", "start", "100"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["dry_run"] is True and payload["op"] == "qemu.start"
    fake_client.guest_power.assert_not_called()


def test_power_wait_reports_task(fake_client, creds, monkeypatch):
    fake_client.resolve_node.return_value = "pve1"
    fake_client.guest_power.return_value = "UPID:pve1:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    r = inv(["--json", "--dangerous", "--wait", "vm", "start", "100"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["result"]["exitstatus"] == "OK"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_cli.py -k "power_dry_run or power_wait" -v --no-cov`
Expected: FAIL (`--dry-run`/`--wait` not yet honored by power commands).

- [ ] **Step 3: Implement**

Replace `_make_power_command` in `pmox/cli.py` with:

```python
def _make_power_command(group, kind, label, action, destructive, description):
    @group.command(action, help=f"{description} a {label}.")
    def _cmd(ctx: typer.Context, vmid: int = vmid_arg, node: Optional[str] = node_opt, yes: bool = yes_opt):
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            _execute(
                ctx,
                op=f"{kind}.{action}",
                message=f"{description}: {label.lower()} {vmid} on {resolved}",
                node=resolved,
                call=lambda: client.guest_power(resolved, kind, vmid, action),
                params={"vmid": vmid, "action": action},
                destructive=destructive,
                yes=yes,
                confirm_msg=f"{action} {label.lower()} {vmid} on {resolved}",
            )

    return _cmd
```

- [ ] **Step 4: Run to verify pass (incl. all existing power/safety tests)**

Run: `python -m pytest tests/test_cli.py -k "power or start or destructive or nondestructive or ok_" -v --no-cov`
Expected: PASS (existing `test_ok_human_envelope`, `test_ok_json_envelope`, `test_destructive_power_needs_yes`, etc. still pass — messages unchanged).

- [ ] **Step 5: Run full suite + commit**

Run: `python -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Route power commands through _execute (dry-run + wait support)"
```

---

## Task 7: Retrofit create / clone / migrate / delete / snapshots onto `_execute`

**Files:**
- Modify: `pmox/cli.py` (`_create`, `_clone`, `_migrate`, `_delete`, `_snap_create`, `_snap_delete`, `_snap_rollback`)
- Test: `tests/test_cli.py` (existing tests must keep passing; add one dry-run test)

- [ ] **Step 1: Write the failing test**

```python
def test_delete_dry_run_skips_call(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dry-run", "vm", "delete", "100"], creds)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["op"] == "qemu.delete"
    fake_client.delete_guest.assert_not_called()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_cli.py::test_delete_dry_run_skips_call -v --no-cov`
Expected: FAIL (delete doesn't honor `--dry-run` yet; without `--dangerous` it currently exits 4).

- [ ] **Step 3: Implement**

In `pmox/cli.py`, rewrite the bodies (keep the decorators/signatures as they are). For `_create`:

```python
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            params = {}
            if name:
                params["name" if kind == "qemu" else "hostname"] = name
            for item in option or []:
                if "=" not in item:
                    raise ValueError(f"--option must be key=value (got {item!r}).")
                key, value = item.split("=", 1)
                params[key] = value
            _execute(
                ctx,
                op=f"{kind}.create",
                message=f"Creating {label.lower()} {vmid} on {node}",
                node=node,
                call=lambda: client.create_guest(node, kind, vmid, **params),
                params={"vmid": vmid, **params},
            )
```

> Note: this keeps `_create`'s existing inline `-o` loop unchanged; Task 9 later extracts it into `parse_options`. No forward dependency between tasks.

For `_clone`:

```python
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            params = {}
            if name:
                params["name"] = name
            if full:
                params["full"] = 1
            if target:
                params["target"] = target
            _execute(
                ctx,
                op=f"{kind}.clone",
                message=f"Cloning {label.lower()} {vmid} → {newid}",
                node=resolved,
                call=lambda: client.clone_guest(resolved, kind, vmid, newid, **params),
                params={"newid": newid, **params},
            )
```

For `_migrate` (destructive):

```python
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            params = {}
            if online:
                params["online"] = 1
            _execute(
                ctx,
                op=f"{kind}.migrate",
                message=f"Migrating {label.lower()} {vmid} → {target}",
                node=resolved,
                call=lambda: client.migrate_guest(resolved, kind, vmid, target, **params),
                params={"target": target, **params},
                destructive=True,
                yes=yes,
                confirm_msg=f"migrate {label.lower()} {vmid} from {resolved} to {target}",
            )
```

For `_delete` (destructive):

```python
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            _execute(
                ctx,
                op=f"{kind}.delete",
                message=f"Deleted {label.lower()} {vmid} on {resolved}",
                node=resolved,
                call=lambda: client.delete_guest(resolved, kind, vmid, purge=purge),
                params={"vmid": vmid, "purge": purge},
                destructive=True,
                yes=yes,
                confirm_msg=f"DELETE {label.lower()} {vmid} on {resolved} (irreversible)",
            )
```

For `_snap_create`:

```python
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            params = {}
            if description:
                params["description"] = description
            if vmstate:
                params["vmstate"] = 1
            _execute(
                ctx,
                op=f"{kind}.snapshot.create",
                message=f"Creating snapshot {name!r} of {label.lower()} {vmid}",
                node=resolved,
                call=lambda: client.create_snapshot(resolved, kind, vmid, name, **params),
                params={"snapname": name, **params},
            )
```

For `_snap_delete` (destructive):

```python
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            _execute(
                ctx,
                op=f"{kind}.snapshot.delete",
                message=f"Deleted snapshot {name!r} of {label.lower()} {vmid}",
                node=resolved,
                call=lambda: client.delete_snapshot(resolved, kind, vmid, name),
                params={"snapname": name},
                destructive=True,
                yes=yes,
                confirm_msg=f"delete snapshot {name!r} of {label.lower()} {vmid}",
            )
```

For `_snap_rollback` (destructive):

```python
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            _execute(
                ctx,
                op=f"{kind}.snapshot.rollback",
                message=f"Rolling back {label.lower()} {vmid} → {name!r}",
                node=resolved,
                call=lambda: client.rollback_snapshot(resolved, kind, vmid, name),
                params={"snapname": name},
                destructive=True,
                yes=yes,
                confirm_msg=f"ROLLBACK {label.lower()} {vmid} to snapshot {name!r} (loses current state)",
            )
```

- [ ] **Step 4: Run to verify pass (incl. all existing create/clone/migrate/delete/snapshot tests)**

Run: `python -m pytest tests/test_cli.py -k "create or clone or migrate or delete or snapshot" -v --no-cov`
Expected: PASS (existing assertions on client calls and exit codes unchanged).

- [ ] **Step 5: Run full suite + commit**

Run: `python -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Route create/clone/migrate/delete/snapshots through _execute"
```

---

## Task 8: Client methods — `update_config`, `resize_disk`, `cluster_nextid`

**Files:**
- Modify: `pmox/client.py` (add three methods)
- Test: `tests/test_client.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_update_config(client, api):
    client.update_config("pve1", "qemu", 100, cores=4, memory=4096)
    api.nodes.return_value.qemu.return_value.config.put.assert_called_once_with(cores=4, memory=4096)


def test_resize_disk(client, api):
    client.resize_disk("pve1", "qemu", 100, "scsi0", "+10G")
    api.nodes.return_value.qemu.return_value.resize.put.assert_called_once_with(disk="scsi0", size="+10G")


def test_cluster_nextid(client, api):
    api.cluster.nextid.get.return_value = "101"
    assert client.cluster_nextid() == "101"
    api.cluster.nextid.get.assert_called_once_with()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_client.py -k "update_config or resize_disk or cluster_nextid" -v --no-cov`
Expected: FAIL with `AttributeError: 'ProxmoxClient' object has no attribute 'update_config'`

- [ ] **Step 3: Implement**

In `pmox/client.py`, add to the guest section (after `guest_config`):

```python
    def update_config(self, node: str, kind: str, vmid, **params) -> Any:
        """Set/update guest options (synchronous PUT on the config endpoint)."""
        return self._guest(node, kind, vmid).config.put(**params)

    def resize_disk(self, node: str, kind: str, vmid, disk: str, size: str) -> Any:
        """Grow a disk. ``size`` is e.g. ``+10G`` (grow by) or ``50G`` (grow to)."""
        return self._guest(node, kind, vmid).resize.put(disk=disk, size=size)
```

And in the version/cluster section (after `cluster_resources`):

```python
    def cluster_nextid(self) -> Any:
        """Return the next free VMID from the cluster."""
        return self._api.cluster.nextid.get()
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `python -m pytest tests/test_client.py -k "update_config or resize_disk or cluster_nextid" -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `python -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/client.py tests/test_client.py
git commit -m "Add client methods: update_config, resize_disk, cluster_nextid"
```

---

## Task 9: Extract `parse_options` and reuse in `create`

**Files:**
- Modify: `pmox/cli.py` (add `parse_options`; use it in `_create`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_options_ok():
    assert cli.parse_options(["cores=4", "memory=4096"]) == {"cores": "4", "memory": "4096"}


def test_parse_options_empty():
    assert cli.parse_options(None) == {}


def test_parse_options_rejects_no_equals():
    with pytest.raises(ValueError):
        cli.parse_options(["noequals"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_cli.py -k parse_options -v --no-cov`
Expected: FAIL with `AttributeError: module 'pmox.cli' has no attribute 'parse_options'`

- [ ] **Step 3: Implement**

Add to `pmox/cli.py` near the other helpers:

```python
def parse_options(items: Optional[List[str]]) -> dict:
    """Parse repeatable ``-o key=value`` options into a dict."""
    params: dict = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--option must be key=value (got {item!r}).")
        key, value = item.split("=", 1)
        params[key] = value
    return params
```

Then in `_create`, replace the inline `-o` loop (the `for item in option or []:` block added in Task 7) with a single line: `params.update(parse_options(option))`.

- [ ] **Step 4: Run focused + full to verify pass**

Run: `python -m pytest tests/test_cli.py -k "parse_options or create" -v --no-cov`
Expected: PASS (incl. existing `test_create_bad_option_exit1`).

- [ ] **Step 5: Run full suite + commit**

Run: `python -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Extract parse_options helper; reuse in create"
```

---

## Task 10: `set` command + delete-guard

**Files:**
- Modify: `pmox/safety.py` (add `set_requires_confirmation`)
- Modify: `pmox/cli.py` (add `set` to the `build_guest_app` factory, near the other guest commands)
- Test: `tests/test_safety.py`, `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_safety.py`:

```python
def test_set_requires_confirmation():
    from pmox.safety import set_requires_confirmation
    assert set_requires_confirmation({"cores": "4"}) is False
    assert set_requires_confirmation({"delete": "net1"}) is True
```

In `tests/test_cli.py`:

```python
def test_set_updates_config(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dangerous", "vm", "set", "100", "-o", "cores=4", "-o", "memory=4096"], creds)
    assert r.exit_code == 0, r.output
    fake_client.update_config.assert_called_once_with("pve1", "qemu", 100, cores="4", memory="4096")


def test_set_needs_dangerous(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["vm", "set", "100", "-o", "cores=4"], creds)
    assert r.exit_code == 4, r.output
    fake_client.update_config.assert_not_called()


def test_set_requires_at_least_one_option(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dangerous", "vm", "set", "100"], creds)
    assert r.exit_code == 1, r.output


def test_set_delete_needs_yes(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dangerous", "vm", "set", "100", "-o", "delete=net1"], creds)
    assert r.exit_code == 3, r.output
    fake_client.update_config.assert_not_called()

    r2 = inv(["--dangerous", "vm", "set", "100", "-o", "delete=net1", "--yes"], creds)
    assert r2.exit_code == 0, r2.output
    fake_client.update_config.assert_called_once_with("pve1", "qemu", 100, delete="net1")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_safety.py::test_set_requires_confirmation tests/test_cli.py -k "set_" -v --no-cov`
Expected: FAIL (`set_requires_confirmation` missing; `No such command 'set'`).

- [ ] **Step 3: Implement**

In `pmox/safety.py`, add:

```python
def set_requires_confirmation(params: dict) -> bool:
    """A config update that removes a device (``delete=``) is destructive."""
    return "delete" in params
```

In `pmox/cli.py`, import it:

```python
from .safety import (
    ConfirmationRequired,
    DangerousNotEnabled,
    confirm,
    require_dangerous,
    set_requires_confirmation,
)
```

In `build_guest_app`, add this command (place it after `_config`):

```python
    @group.command("set")
    def _set(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        option: Optional[List[str]] = typer.Option(
            None, "--option", "-o", help="Config key=value to set (repeatable). Use delete=dev to remove (needs --yes)."
        ),
        node: Optional[str] = node_opt,
        yes: bool = yes_opt,
    ):
        """Update configuration of a {label} (cores, memory, disks, nics, tags, …)."""
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            params = parse_options(option)
            if not params:
                raise ValueError("set needs at least one -o key=value.")
            resolved = node or _resolve_node_or_die(client, vmid)
            _execute(
                ctx,
                op=f"{kind}.set",
                message=f"Set {label.lower()} {vmid} on {resolved}",
                node=resolved,
                call=lambda: client.update_config(resolved, kind, vmid, **params),
                params=params,
                destructive=set_requires_confirmation(params),
                yes=yes,
                confirm_msg=f"set {label.lower()} {vmid}: remove {params.get('delete')}",
            )
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `python -m pytest tests/test_safety.py::test_set_requires_confirmation tests/test_cli.py -k "set_" -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `python -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/safety.py pmox/cli.py tests/test_safety.py tests/test_cli.py
git commit -m "Add vm/ct set command with delete-guard"
```

---

## Task 11: `resize` command

**Files:**
- Modify: `pmox/cli.py` (add `resize` to the factory, after `set`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_resize_grows_disk(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dangerous", "vm", "resize", "100", "--disk", "scsi0", "--size", "+10G"], creds)
    assert r.exit_code == 0, r.output
    fake_client.resize_disk.assert_called_once_with("pve1", "qemu", 100, "scsi0", "+10G")


def test_resize_needs_dangerous(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["vm", "resize", "100", "--disk", "scsi0", "--size", "+10G"], creds)
    assert r.exit_code == 4, r.output
    fake_client.resize_disk.assert_not_called()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_cli.py -k resize -v --no-cov`
Expected: FAIL with `No such command 'resize'`

- [ ] **Step 3: Implement**

In `build_guest_app`, after `_set`:

```python
    @group.command("resize")
    def _resize(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        disk: str = typer.Option(..., "--disk", help="Disk to grow, e.g. scsi0."),
        size: str = typer.Option(..., "--size", help="+10G (grow by) or 50G (grow to)."),
        node: Optional[str] = node_opt,
    ):
        """Grow a disk of a {label} (grow-only)."""
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            _execute(
                ctx,
                op=f"{kind}.resize",
                message=f"Resize {label.lower()} {vmid} disk {disk} to {size}",
                node=resolved,
                call=lambda: client.resize_disk(resolved, kind, vmid, disk, size),
                params={"disk": disk, "size": size},
            )
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -k resize -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `python -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Add vm/ct resize command"
```

---

## Task 12: `rename` command

**Files:**
- Modify: `pmox/cli.py` (add `rename` to the factory, after `resize`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_rename_vm_sets_name(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dangerous", "vm", "rename", "100", "web01"], creds)
    assert r.exit_code == 0, r.output
    fake_client.update_config.assert_called_once_with("pve1", "qemu", 100, name="web01")


def test_rename_ct_sets_hostname(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dangerous", "ct", "rename", "200", "box01"], creds)
    assert r.exit_code == 0, r.output
    fake_client.update_config.assert_called_once_with("pve1", "lxc", 200, hostname="box01")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_cli.py -k rename -v --no-cov`
Expected: FAIL with `No such command 'rename'`

- [ ] **Step 3: Implement**

In `build_guest_app`, after `_resize`:

```python
    @group.command("rename")
    def _rename(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        newname: str = typer.Argument(..., help="New name (VM) / hostname (CT)."),
        node: Optional[str] = node_opt,
    ):
        """Rename a {label}."""
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            key = "name" if kind == "qemu" else "hostname"
            _execute(
                ctx,
                op=f"{kind}.rename",
                message=f"Rename {label.lower()} {vmid} to {newname}",
                node=resolved,
                call=lambda: client.update_config(resolved, kind, vmid, **{key: newname}),
                params={key: newname},
            )
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -k rename -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `python -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Add vm/ct rename command"
```

---

## Task 13: `merge_tags` helper + `tag` command

**Files:**
- Modify: `pmox/cli.py` (add `import re`; add `merge_tags`; add `tag` to the factory after `rename`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_merge_tags_set_replaces():
    assert cli.merge_tags("a;b", set_="x,y") == "x;y"


def test_merge_tags_add_and_remove():
    assert cli.merge_tags("a;b", add="c", remove="a") == "b;c"


def test_merge_tags_add_is_idempotent():
    assert cli.merge_tags("a;b", add="b") == "a;b"


def test_merge_tags_empty():
    assert cli.merge_tags("", add="a") == "a"


def test_tag_reads_then_writes(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    fake_client.guest_config.return_value = {"tags": "prod"}
    r = inv(["--dangerous", "vm", "tag", "100", "--add", "k3s"], creds)
    assert r.exit_code == 0, r.output
    fake_client.update_config.assert_called_once_with("pve1", "qemu", 100, tags="prod;k3s")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_cli.py -k "merge_tags or tag_reads" -v --no-cov`
Expected: FAIL with `AttributeError: module 'pmox.cli' has no attribute 'merge_tags'`

- [ ] **Step 3: Implement**

Add `import re` to `pmox/cli.py`. Add the helper near `parse_options`:

```python
def _split_tags(value: Optional[str]) -> List[str]:
    return [t for t in re.split(r"[;,]", value or "") if t]


def merge_tags(current: str, add: Optional[str] = None, remove: Optional[str] = None, set_: Optional[str] = None) -> str:
    """Compute a new Proxmox ``tags`` string. ``set_`` replaces; otherwise add/remove."""
    if set_ is not None:
        tags = _split_tags(set_)
    else:
        tags = _split_tags(current)
        for t in _split_tags(add):
            if t not in tags:
                tags.append(t)
        for t in _split_tags(remove):
            if t in tags:
                tags.remove(t)
    return ";".join(tags)
```

In `build_guest_app`, after `_rename`:

```python
    @group.command("tag")
    def _tag(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        add: Optional[str] = typer.Option(None, "--add", help="Comma-separated tags to add."),
        remove: Optional[str] = typer.Option(None, "--remove", help="Comma-separated tags to remove."),
        set_: Optional[str] = typer.Option(None, "--set", help="Comma-separated tags to set (replaces all)."),
        node: Optional[str] = node_opt,
    ):
        """Add/remove/set tags on a {label}."""
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            current = client.guest_config(resolved, kind, vmid).get("tags", "")
            new_tags = merge_tags(current, add=add, remove=remove, set_=set_)
            _execute(
                ctx,
                op=f"{kind}.tag",
                message=f"Set tags on {label.lower()} {vmid}: {new_tags!r}",
                node=resolved,
                call=lambda: client.update_config(resolved, kind, vmid, tags=new_tags),
                params={"tags": new_tags},
            )
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -k "merge_tags or tag_reads" -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `python -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Add merge_tags helper and vm/ct tag command"
```

---

## Self-review (completed during planning)

- **Spec coverage (§4, §5.1, §3.1):** position-independent flags (Task 1) ✓; `--wait`/`--timeout`/`--dry-run` (Tasks 2, 3, 5, 6, 7) ✓; `_execute` lifecycle (Task 5) ✓; machine-readable errors (Task 4) ✓; `set` delete-guard (Task 10) ✓; `set`/`resize`/`rename`/`tag` for both kinds (Tasks 10–13, via the factory) ✓; client `update_config`/`resize_disk`/`cluster_nextid` (Task 8) ✓. **Deferred to B2/C/A (not this plan):** `vm new`, `describe`, `health`, provisioning, skill/docs.
- **Name/type consistency:** `hoist_global_flags`, `_maybe_wait`, `_execute(op,message,node,call,params,destructive,yes,confirm_msg)`, `_emit_error`, `_emit_dry_run`, `parse_options`, `merge_tags`, `set_requires_confirmation`, `update_config`, `resize_disk`, `cluster_nextid` are used consistently across tasks.
- **Coverage discipline:** every task adds tests covering its own new lines; focused runs use `--no-cov`, the pre-commit run uses full `pytest` (100% gate).
- **No forward references:** Task 7 keeps `_create`'s inline `-o` loop; Task 9 later refactors it into `parse_options`. Tasks execute in order with no dependency on a not-yet-defined helper.
```
