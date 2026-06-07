"""CLI-level tests using Typer's runner with an injected fake client.

These exercise every command plus the two-tier safety model end to end:
    * read-only by default            -> mutations exit 4
    * --dangerous enables mutations   -> non-destructive ops succeed
    * destructive ops still need --yes -> exit 3 without it
"""

import json
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

import pmox.cli as cli

runner = CliRunner()


def inv(args, creds, **kwargs):
    return runner.invoke(cli.app, args, env=creds, **kwargs)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def plain(text):
    """Strip ANSI SGR color codes so output assertions are stable regardless of
    the ambient color environment. CI commonly sets FORCE_COLOR, which makes Rich
    emit color even when stdout is captured (not a TTY)."""
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------- read-only --


def test_server_version(fake_client, creds):
    fake_client.version.return_value = {"version": "8.1.4"}
    r = inv(["version"], creds)
    assert r.exit_code == 0, r.output
    fake_client.version.assert_called_once_with()


def test_server_version_json(fake_client, creds):
    fake_client.version.return_value = {"version": "8.1.4"}
    r = inv(["--json", "version"], creds)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == {"version": "8.1.4"}


def test_nodes_list(fake_client, creds):
    fake_client.list_nodes.return_value = [{"node": "pve1", "status": "online"}]
    r = inv(["nodes", "list"], creds)
    assert r.exit_code == 0, r.output
    assert "pve1" in r.output


def test_nodes_status(fake_client, creds):
    fake_client.node_status.return_value = {"uptime": 100}
    r = inv(["nodes", "status", "pve1"], creds)
    assert r.exit_code == 0, r.output
    fake_client.node_status.assert_called_once_with("pve1")


def test_vm_list_json(fake_client, creds):
    data = [{"vmid": 100, "name": "web", "type": "qemu", "status": "running", "node": "pve1"}]
    fake_client.list_guests.return_value = data
    r = inv(["--json", "vm", "list"], creds)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == data
    fake_client.list_guests.assert_called_once_with("qemu", node=None)


def test_ct_list(fake_client, creds):
    fake_client.list_guests.return_value = []
    r = inv(["ct", "list"], creds)
    assert r.exit_code == 0, r.output
    fake_client.list_guests.assert_called_once_with("lxc", node=None)


def test_vm_status_autoresolves_node(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    fake_client.guest_status.return_value = {"status": "running"}
    r = inv(["vm", "status", "100"], creds)
    assert r.exit_code == 0, r.output
    fake_client.resolve_node.assert_called_once_with(100)
    fake_client.guest_status.assert_called_once_with("pve1", "qemu", 100)


def test_vm_status_with_explicit_node_skips_resolve(fake_client, creds):
    fake_client.guest_status.return_value = {"status": "running"}
    r = inv(["vm", "status", "100", "--node", "pve2"], creds)
    assert r.exit_code == 0, r.output
    fake_client.resolve_node.assert_not_called()
    fake_client.guest_status.assert_called_once_with("pve2", "qemu", 100)


def test_vm_config(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    fake_client.guest_config.return_value = {"cores": 2}
    r = inv(["vm", "config", "100"], creds)
    assert r.exit_code == 0, r.output
    fake_client.guest_config.assert_called_once_with("pve1", "qemu", 100)


def test_resolve_failure_exit1(fake_client, creds):
    fake_client.resolve_node.return_value = None
    r = inv(["vm", "status", "999"], creds)
    assert r.exit_code == 1, r.output


def test_storage_list(fake_client, creds):
    fake_client.cluster_resources.return_value = [{"storage": "local", "node": "pve1"}]
    r = inv(["storage", "list"], creds)
    assert r.exit_code == 0, r.output
    fake_client.cluster_resources.assert_called_once_with(type="storage")


def test_storage_list_node_filter(fake_client, creds):
    fake_client.cluster_resources.return_value = [
        {"storage": "local", "node": "pve1"},
        {"storage": "local", "node": "pve2"},
    ]
    r = inv(["--json", "storage", "list", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    assert all(row["node"] == "pve1" for row in json.loads(r.output))


def test_storage_content(fake_client, creds):
    fake_client.storage_content.return_value = [{"volid": "x"}]
    r = inv(["storage", "content", "local", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    fake_client.storage_content.assert_called_once_with("pve1", "local")


def test_cluster_status_table(fake_client, creds):
    fake_client.cluster_status.return_value = [
        {"type": "cluster", "name": "cl"},
        {"type": "node", "name": "pve1", "online": 1},
    ]
    r = inv(["--no-json", "cluster", "status"], creds)
    assert r.exit_code == 0, r.output
    assert "pve1" in r.output


def test_cluster_status_json(fake_client, creds):
    data = [{"type": "node", "name": "pve1", "online": 1}]
    fake_client.cluster_status.return_value = data
    r = inv(["--json", "cluster", "status"], creds)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == data


def test_cluster_resources(fake_client, creds):
    fake_client.cluster_resources.return_value = [{"type": "vm", "vmid": 100}]
    r = inv(["cluster", "resources", "--type", "vm"], creds)
    assert r.exit_code == 0, r.output
    fake_client.cluster_resources.assert_called_once_with(type="vm")


def test_task_list(fake_client, creds):
    fake_client.list_tasks.return_value = [{"upid": "U", "type": "x"}]
    r = inv(["task", "list", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    fake_client.list_tasks.assert_called_once_with("pve1", limit=50)


def test_task_status(fake_client, creds):
    fake_client.task_status.return_value = {"status": "OK"}
    r = inv(["task", "status", "UPID:x", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    fake_client.task_status.assert_called_once_with("pve1", "UPID:x")


def test_task_log_text(fake_client, creds):
    fake_client.task_log.return_value = [{"n": 1, "t": "line one"}, {"n": 2, "t": "line two"}]
    r = inv(["--no-json", "task", "log", "UPID:x", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    assert "line one" in r.output


def test_task_log_json(fake_client, creds):
    data = [{"n": 1, "t": "line one"}]
    fake_client.task_log.return_value = data
    r = inv(["--json", "task", "log", "UPID:x", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == data


# ------------------------------------------------------------- safety gates --


def test_start_blocked_in_readonly(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["vm", "start", "100"], creds)
    assert r.exit_code == 4, r.output
    fake_client.guest_power.assert_not_called()


def test_start_allowed_in_dangerous(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dangerous", "vm", "start", "100"], creds)
    assert r.exit_code == 0, r.output
    fake_client.guest_power.assert_called_once_with("pve1", "qemu", 100, "start")


def test_pmox_dangerous_env_enables_writes(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["vm", "start", "100"], dict(creds, PMOX_DANGEROUS="1"))
    assert r.exit_code == 0, r.output
    fake_client.guest_power.assert_called_once_with("pve1", "qemu", 100, "start")


@pytest.mark.parametrize("action", ["shutdown", "reboot", "suspend", "resume"])
def test_nondestructive_power(fake_client, creds, action):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dangerous", "vm", action, "100"], creds)
    assert r.exit_code == 0, r.output
    fake_client.guest_power.assert_called_once_with("pve1", "qemu", 100, action)


@pytest.mark.parametrize("action", ["stop", "reset"])
def test_destructive_power_needs_yes(fake_client, creds, action):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dangerous", "vm", action, "100"], creds)
    assert r.exit_code == 3, r.output
    fake_client.guest_power.assert_not_called()

    r2 = inv(["--dangerous", "vm", action, "100", "--yes"], creds)
    assert r2.exit_code == 0, r2.output
    fake_client.guest_power.assert_called_once_with("pve1", "qemu", 100, action)


def test_create_vm(fake_client, creds):
    r = inv(
        ["--dangerous", "vm", "create", "105", "--node", "pve1", "--name", "web", "-o", "cores=2"],
        creds,
    )
    assert r.exit_code == 0, r.output
    fake_client.create_guest.assert_called_once_with("pve1", "qemu", 105, name="web", cores="2")


def test_create_ct_uses_hostname(fake_client, creds):
    r = inv(["--dangerous", "ct", "create", "205", "--node", "pve1", "--name", "box"], creds)
    assert r.exit_code == 0, r.output
    fake_client.create_guest.assert_called_once_with("pve1", "lxc", 205, hostname="box")


def test_create_bad_option_exit1(fake_client, creds):
    r = inv(["--dangerous", "vm", "create", "105", "--node", "pve1", "-o", "noequals"], creds)
    assert r.exit_code == 1, r.output
    fake_client.create_guest.assert_not_called()


def test_clone(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(
        ["--dangerous", "vm", "clone", "100", "--newid", "105", "--full", "--name", "copy", "--target", "pve2"],
        creds,
    )
    assert r.exit_code == 0, r.output
    fake_client.clone_guest.assert_called_once_with("pve1", "qemu", 100, 105, name="copy", full=1, target="pve2")


def test_migrate_needs_yes(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dangerous", "vm", "migrate", "100", "--target", "pve2"], creds)
    assert r.exit_code == 3, r.output

    r2 = inv(["--dangerous", "vm", "migrate", "100", "--target", "pve2", "--online", "--yes"], creds)
    assert r2.exit_code == 0, r2.output
    fake_client.migrate_guest.assert_called_once_with("pve1", "qemu", 100, "pve2", online=1)


def test_delete_blocked_in_readonly_before_confirm(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["vm", "delete", "100", "--yes"], creds)
    assert r.exit_code == 4, r.output  # write gate fires before the confirm gate
    fake_client.delete_guest.assert_not_called()


def test_delete_needs_yes(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dangerous", "vm", "delete", "100"], creds)
    assert r.exit_code == 3, r.output
    fake_client.delete_guest.assert_not_called()


def test_delete_ok_with_purge(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    fake_client.delete_guest.return_value = "UPID"
    r = inv(["--dangerous", "vm", "delete", "100", "--yes", "--purge"], creds)
    assert r.exit_code == 0, r.output
    fake_client.delete_guest.assert_called_once_with("pve1", "qemu", 100, purge=True)


def test_ok_json_envelope(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    fake_client.guest_power.return_value = "UPID:task"
    r = inv(["--json", "--dangerous", "vm", "start", "100"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["ok"] is True
    assert payload["result"] == "UPID:task"


# --------------------------------------------------------------- snapshots ---


def test_snapshot_list(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    fake_client.list_snapshots.return_value = [{"name": "pre"}]
    r = inv(["vm", "snapshot", "list", "100"], creds)
    assert r.exit_code == 0, r.output
    fake_client.list_snapshots.assert_called_once_with("pve1", "qemu", 100)


def test_snapshot_create_needs_dangerous(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["vm", "snapshot", "create", "100", "snap1"], creds)
    assert r.exit_code == 4, r.output
    fake_client.create_snapshot.assert_not_called()


def test_snapshot_create(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dangerous", "vm", "snapshot", "create", "100", "snap1", "-d", "desc", "--vmstate"], creds)
    assert r.exit_code == 0, r.output
    fake_client.create_snapshot.assert_called_once_with("pve1", "qemu", 100, "snap1", description="desc", vmstate=1)


def test_snapshot_delete_needs_yes(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dangerous", "vm", "snapshot", "delete", "100", "snap1"], creds)
    assert r.exit_code == 3, r.output

    r2 = inv(["--dangerous", "vm", "snapshot", "delete", "100", "snap1", "--yes"], creds)
    assert r2.exit_code == 0, r2.output
    fake_client.delete_snapshot.assert_called_once_with("pve1", "qemu", 100, "snap1")


def test_snapshot_rollback(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dangerous", "vm", "snapshot", "rollback", "100", "snap1", "--yes"], creds)
    assert r.exit_code == 0, r.output
    fake_client.rollback_snapshot.assert_called_once_with("pve1", "qemu", 100, "snap1")


# ----------------------------------------------------------- config / meta ---


def test_missing_credentials_exit2():
    r = runner.invoke(
        cli.app,
        ["nodes", "list"],
        env={
            "PROXMOX_HOST": "",
            "PROXMOX_TOKEN_ID": "",
            "PROXMOX_TOKEN_SECRET": "",
            "PMOX_CONFIG": "/nonexistent-pmox-config.toml",
        },
    )
    assert r.exit_code == 2, r.output


def test_version_flag():
    r = runner.invoke(cli.app, ["--version"])
    assert r.exit_code == 0
    assert "pmox" in r.output


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


def test_hoist_multiple_interleaved_flags():
    assert cli.hoist_global_flags(["vm", "list", "--json", "--timeout", "30"]) == [
        "--json", "--timeout", "30", "vm", "list",
    ]


def test_main_invokes_app(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "app", lambda **kw: captured.update(kw) or captured.setdefault("ran", True))
    monkeypatch.setattr(cli.sys, "argv", ["pmox", "vm", "list", "--json"])
    cli.main()
    assert captured["ran"] is True
    assert captured["args"] == ["--json", "vm", "list"]


def test_callback_config_error_exit2(monkeypatch):
    from pmox.config import ConfigError

    def boom(*args, **kwargs):
        raise ConfigError("bad config file")

    monkeypatch.setattr(cli, "load_settings", boom)
    r = runner.invoke(cli.app, ["nodes", "list"], env={})
    assert r.exit_code == 2, r.output


def test_callback_dotenv_failure_is_ignored(fake_client, creds, monkeypatch):
    import dotenv

    def boom(*args, **kwargs):
        raise RuntimeError("dotenv blew up")

    monkeypatch.setattr(dotenv, "load_dotenv", boom)
    fake_client.list_nodes.return_value = []
    r = runner.invoke(cli.app, ["nodes", "list"], env=creds)
    assert r.exit_code == 0, r.output


# --------------------------------------------------- output-mode resolution ---
# JSON vs human tables. Precedence (highest first): explicit --json/--no-json
# flag > PMOX_JSON env > auto-detect (not a TTY -> JSON, so agents/pipes get
# machine-readable output for free while humans at a terminal get tables).


@pytest.mark.parametrize(
    "flag, env, isatty, expected",
    [
        # explicit flag always wins, regardless of env or TTY
        (True, None, True, True),
        (True, "0", True, True),
        (False, None, False, False),
        (False, "1", False, False),
        # PMOX_JSON, when set, decides (no flag given)
        (None, "1", True, True),
        (None, "true", True, True),
        (None, "yes", True, True),
        (None, "0", False, False),
        (None, "false", False, False),
        # PMOX_JSON=auto defers to the TTY check
        (None, "auto", True, False),
        (None, "auto", False, True),
        (None, "  AUTO  ", False, True),
        # empty / unset env falls through to auto-detect
        (None, "", False, True),
        (None, None, True, False),   # human terminal -> tables
        (None, None, False, True),   # captured / piped -> JSON
    ],
)
def test_resolve_json_output(flag, env, isatty, expected):
    assert cli.resolve_json_output(flag, env, isatty) is expected


def test_stream_isatty_reports_true():
    class TTY:
        def isatty(self):
            return True

    assert cli._stream_isatty(TTY()) is True


def test_stream_isatty_false_when_isatty_raises():
    class Bad:
        def isatty(self):
            raise OSError("no tty here")

    assert cli._stream_isatty(Bad()) is False


def test_json_default_when_output_captured(fake_client, creds):
    """Under the runner stdout is not a TTY, so JSON is the default with no flag."""
    data = [{"vmid": 100, "name": "web"}]
    fake_client.list_guests.return_value = data
    r = inv(["vm", "list"], creds)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == data


def test_no_json_overrides_capture_default(fake_client, creds):
    """--no-json forces the human table even when output is captured."""
    data = [{"vmid": 100, "name": "web", "type": "qemu", "status": "running", "node": "pve1"}]
    fake_client.list_guests.return_value = data
    r = inv(["--no-json", "vm", "list"], creds)
    assert r.exit_code == 0, r.output
    assert "web" in r.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(r.output)


def test_ok_human_envelope(fake_client, creds):
    """--no-json yields the human success line, not the JSON envelope."""
    fake_client.resolve_node.return_value = "pve1"
    fake_client.guest_power.return_value = "UPID:task"
    r = inv(["--no-json", "--dangerous", "vm", "start", "100"], creds)
    assert r.exit_code == 0, r.output
    assert "Start: vm 100 on pve1" in plain(r.output)
    with pytest.raises(json.JSONDecodeError):
        json.loads(r.output)


def test_global_flags_parse_without_error(fake_client, creds):
    # --wait/--timeout/--dry-run are accepted on the root callback (before subcommand).
    fake_client.list_nodes.return_value = []
    r = inv(["--wait", "--timeout", "5", "--dry-run", "nodes", "list"], creds)
    assert r.exit_code == 0, r.output


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


# ---------------------------------------------------------- _execute helper ---


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


# ------------------------------------------------- Task 6: power via _execute --


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


# ------------------------------------------------- Task 7: create/clone/migrate/delete/snap via _execute --


def test_delete_dry_run_skips_call(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dry-run", "vm", "delete", "100"], creds)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["op"] == "qemu.delete"
    assert json.loads(r.output)["dry_run"] is True
    fake_client.delete_guest.assert_not_called()


# ------------------------------------------------- Task 9: parse_options helper --


def test_parse_options_ok():
    assert cli.parse_options(["cores=4", "memory=4096"]) == {"cores": "4", "memory": "4096"}


def test_parse_options_empty():
    assert cli.parse_options(None) == {}


def test_parse_options_rejects_no_equals():
    with pytest.raises(ValueError):
        cli.parse_options(["noequals"])


# ------------------------------------------------- Task 10: set command + delete-guard --


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


# ------------------------------------------------- Task 11: resize command --


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


# ------------------------------------------------- Task 12: rename command --


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


# ------------------------------------------------- Task 13: merge_tags + tag command --


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


# ------------------------------------------------- Task 3: vm/ct describe command --


def test_describe_json(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    fake_client.guest_status.return_value = {"status": "running"}
    fake_client.guest_config.return_value = {"cores": 2}
    fake_client.list_snapshots.return_value = []
    fake_client.list_tasks.return_value = []
    r = inv(["--json", "vm", "describe", "100"], creds)
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["vmid"] == 100 and data["node"] == "pve1" and data["kind"] == "qemu"


def test_describe_human(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    fake_client.guest_status.return_value = {"status": "running"}
    fake_client.guest_config.return_value = {"cores": 2}
    fake_client.list_snapshots.return_value = [{"name": "pre"}]
    fake_client.list_tasks.return_value = [{"id": "100", "type": "qmstart"}]
    r = inv(["--no-json", "vm", "describe", "100"], creds)
    assert r.exit_code == 0, r.output
    assert "running" in plain(r.output)


def test_describe_ct(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    fake_client.guest_status.return_value = {"status": "running"}
    fake_client.guest_config.return_value = {}
    fake_client.list_snapshots.return_value = []
    fake_client.list_tasks.return_value = []
    r = inv(["--json", "ct", "describe", "200"], creds)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["kind"] == "lxc"


# ------------------------------------------------- Task 5: health top-level command --


def test_health_json(fake_client, creds):
    fake_client.cluster_status.return_value = [{"type": "cluster", "quorate": 1}, {"type": "node", "name": "p1", "online": 1}]
    fake_client.list_nodes.return_value = [{"node": "p1", "status": "online", "cpu": 0.1, "mem": 1, "maxmem": 10}]
    fake_client.cluster_resources.side_effect = lambda type=None: (
        [{"storage": "local", "node": "p1", "disk": 1, "maxdisk": 10}] if type == "storage"
        else [{"vmid": 100, "status": "running"}]
    )
    r = inv(["--json", "health"], creds)
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["quorate"] is True and data["guests"]["running"] == 1


def test_health_human(fake_client, creds):
    fake_client.cluster_status.return_value = [{"type": "cluster", "quorate": 1}, {"type": "node", "name": "p1", "online": 1}]
    fake_client.list_nodes.return_value = [{"node": "p1", "status": "online", "cpu": 0.9, "mem": 9, "maxmem": 10}]
    fake_client.cluster_resources.side_effect = lambda type=None: (
        [{"storage": "s", "node": "p1", "disk": 9, "maxdisk": 10}] if type == "storage"
        else [{"vmid": 100, "status": "running"}]
    )
    r = inv(["--no-json", "health"], creds)
    assert r.exit_code == 0, r.output
    assert "p1" in plain(r.output)


# ------------------------------------------------- Task 6: vm new command --


def test_vm_new_with_profile_explicit_node_and_vmid(fake_client, creds):
    r = inv(["--dangerous", "vm", "new", "web", "--size", "medium", "--node", "pve1", "--vmid", "105"], creds)
    assert r.exit_code == 0, r.output
    fake_client.create_guest.assert_called_once_with(
        "pve1", "qemu", 105,
        cores=2, memory=4096, scsihw="virtio-scsi-single", net0="virtio,bridge=vmbr0",
        ostype="l26", name="web",
    )
    fake_client.cluster_nextid.assert_not_called()


def test_vm_new_auto_vmid(fake_client, creds):
    fake_client.cluster_nextid.return_value = "150"
    r = inv(["--dangerous", "vm", "new", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    fake_client.cluster_nextid.assert_called_once_with()
    args, kwargs = fake_client.create_guest.call_args
    assert args[2] == 150  # vmid coerced to int


def test_vm_new_auto_node_single(fake_client, creds):
    fake_client.list_nodes.return_value = [{"node": "only"}]
    fake_client.cluster_nextid.return_value = "150"
    r = inv(["--dangerous", "vm", "new", "--vmid", "150"], creds)
    assert r.exit_code == 0, r.output
    assert fake_client.create_guest.call_args.args[0] == "only"


def test_vm_new_auto_node_multiple_errors(fake_client, creds):
    fake_client.list_nodes.return_value = [{"node": "a"}, {"node": "b"}]
    r = inv(["--dangerous", "vm", "new", "--vmid", "150"], creds)
    assert r.exit_code == 1, r.output
    fake_client.create_guest.assert_not_called()


def test_vm_new_with_disk(fake_client, creds):
    r = inv(["--dangerous", "vm", "new", "--node", "pve1", "--vmid", "150", "--disk", "50"], creds)
    assert r.exit_code == 0, r.output
    kwargs = fake_client.create_guest.call_args.kwargs
    assert kwargs["scsi0"] == "local-lvm:50,iothread=1"
    assert kwargs["boot"] == "order=scsi0"


def test_vm_new_option_override(fake_client, creds):
    r = inv(["--dangerous", "vm", "new", "--node", "pve1", "--vmid", "150", "-o", "cores=8"], creds)
    assert r.exit_code == 0, r.output
    assert fake_client.create_guest.call_args.kwargs["cores"] == "8"


def test_vm_new_bad_size(fake_client, creds):
    r = inv(["--dangerous", "vm", "new", "--node", "pve1", "--vmid", "150", "--size", "huge"], creds)
    assert r.exit_code == 1, r.output
    fake_client.create_guest.assert_not_called()


def test_vm_new_needs_dangerous(fake_client, creds):
    r = inv(["vm", "new", "--node", "pve1", "--vmid", "150"], creds)
    assert r.exit_code == 4, r.output
    fake_client.create_guest.assert_not_called()


def test_vm_new_dry_run(fake_client, creds):
    r = inv(["--dry-run", "vm", "new", "--node", "pve1", "--vmid", "150"], creds)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["op"] == "qemu.new"
    fake_client.create_guest.assert_not_called()


def test_ct_has_no_new(fake_client, creds):
    r = inv(["ct", "new", "box"], creds)
    assert r.exit_code != 0  # no such command for containers
