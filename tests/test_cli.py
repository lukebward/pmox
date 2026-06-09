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

_IMPORT_STORAGES = [
    {"storage": "local", "content": "import,iso,vztmpl,backup", "plugintype": "dir"},
    {"storage": "local-lvm", "content": "images,rootdir", "plugintype": "lvmthin"},
]


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
    assert payload["op"] == "qemu.start"
    assert payload["vmid"] == 100
    assert payload["node"] == "pve1"
    assert payload["upid"] == "UPID:task"
    assert "result" not in payload  # typed fields replace the untyped result


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

    def fake_app(**kw):
        captured.update(kw)
        captured["ran"] = True
        return None

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(cli.sys, "argv", ["pmox", "vm", "list", "--json"])
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert ei.value.code == 0
    assert captured["ran"] is True
    assert captured["args"] == ["--json", "vm", "list"]
    assert captured["standalone_mode"] is False


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
    with pytest.raises(TimeoutError) as ei:
        cli._maybe_wait(ctx, "pve1", "UPID:pve1:0001")
    assert isinstance(ei.value, cli.TaskTimeout)
    assert ei.value.extra["upid"] == "UPID:pve1:0001"
    assert ei.value.extra["node"] == "pve1"
    assert "task wait" in ei.value.extra["hint"]


def test_maybe_wait_raises_on_failed_task(monkeypatch):
    client = MagicMock()
    client.task_status.return_value = {"status": "stopped", "exitstatus": "some error"}
    state = cli.State(settings=None)
    state.client = client
    ctx = SimpleNamespace(obj=state)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    with pytest.raises(RuntimeError) as ei:
        cli._maybe_wait(ctx, "pve1", "UPID:x")
    assert isinstance(ei.value, cli.TaskFailed)
    assert ei.value.extra["upid"] == "UPID:x"
    assert ei.value.extra["node"] == "pve1"
    assert "task log" in ei.value.extra["hint"]


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
        call=lambda: "UPID:done", params={"cores": "4"}, vmid=100,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["op"] == "vm.set" and payload["node"] == "pve1" and payload["vmid"] == 100
    assert payload["upid"] == "UPID:done"
    assert result == "UPID:done"


def test_ok_human_prints_hint_and_detail(capsys):
    state = cli.State(settings=None)
    ctx = SimpleNamespace(obj=state)
    cli._ok(ctx, "did it", upid="UPID:x", hint="check with `pmox task wait UPID:x`")
    out = plain(capsys.readouterr().out)
    assert "did it" in out and "UPID:x" in out and "task wait" in out


def test_execute_sync_result_kept_in_result_field(capsys):
    ctx = _exec_ctx(json=True, dangerous=True)
    cli._execute(
        ctx, op="vm.set", message="m", node="pve1",
        call=lambda: ["something", "else"], vmid=100,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == ["something", "else"]
    assert "upid" not in payload and "task" not in payload


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
    assert payload["task"]["exitstatus"] == "OK"
    assert "result" not in payload


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


def _ip_row(name="web-01"):
    row = {"vmid": 150, "node": "lukeserver"}
    if name is not None:
        row["name"] = name
    return [row]


def test_vm_ip_filtered(fake_client, creds):
    fake_client.cluster_resources.return_value = _ip_row()
    fake_client.agent_network_interfaces.return_value = {"result": [
        {"name": "lo", "hardware-address": "0", "ip-addresses": [
            {"ip-address-type": "ipv4", "ip-address": "127.0.0.1", "prefix": 8}]},
        {"name": "eth0", "hardware-address": "bc:24:11:aa:bb:cc", "ip-addresses": [
            {"ip-address-type": "ipv4", "ip-address": "192.168.1.50", "prefix": 24},
            {"ip-address-type": "ipv6", "ip-address": "fe80::1", "prefix": 64}]},
    ]}
    r = inv(["--no-json", "vm", "ip", "150"], creds)
    assert r.exit_code == 0, r.output
    out = plain(r.output)
    assert "primary 192.168.1.50" in out
    assert "eth0" in out
    assert "127.0.0.1" not in out  # loopback hidden by default
    assert "fe80::1" not in out    # link-local hidden by default


def test_vm_ip_all_shows_loopback_and_mac(fake_client, creds):
    fake_client.cluster_resources.return_value = _ip_row(name=None)  # exercises name-absent header
    fake_client.agent_network_interfaces.return_value = {"result": [
        {"name": "lo", "ip-addresses": [  # no hardware-address -> MAC '-'
            {"ip-address-type": "ipv4", "ip-address": "127.0.0.1", "prefix": 8}]},
        {"name": "eth0", "hardware-address": "bc:24:11:aa:bb:cc", "ip-addresses": [
            {"ip-address-type": "ipv4", "ip-address": "192.168.1.50", "prefix": 24}]},
    ]}
    r = inv(["--no-json", "vm", "ip", "150", "--all"], creds)
    assert r.exit_code == 0, r.output
    out = plain(r.output)
    assert "127.0.0.1" in out      # loopback shown with --all
    assert "bc:24:11" in out       # MAC shown (fold-safe prefix)


def test_vm_ip_json_full_data(fake_client, creds):
    fake_client.cluster_resources.return_value = _ip_row()
    fake_client.agent_network_interfaces.return_value = {"result": [
        {"name": "eth0", "hardware-address": "bc:24:11:aa:bb:cc", "ip-addresses": [
            {"ip-address-type": "ipv4", "ip-address": "192.168.1.50", "prefix": 24}]},
    ]}
    r = inv(["--json", "vm", "ip", "150"], creds)
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["primary"] == "192.168.1.50"
    assert data["interfaces"][0]["name"] == "eth0"
    fake_client.agent_network_interfaces.assert_called_once_with("lukeserver", 150)


def test_ct_ip_uses_interfaces_endpoint(fake_client, creds):
    fake_client.cluster_resources.return_value = [{"vmid": 200, "node": "pve1", "name": "ct"}]
    fake_client.lxc_interfaces.return_value = [{"name": "eth0", "hwaddr": "aa:bb", "inet": "10.0.0.5/24"}]
    r = inv(["--json", "ct", "ip", "200"], creds)
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["primary"] == "10.0.0.5" and data["source"] == "lxc-interfaces"
    fake_client.lxc_interfaces.assert_called_once_with("pve1", 200)


def test_vm_ip_agent_down_error_envelope(fake_client, creds):
    fake_client.cluster_resources.return_value = _ip_row()
    fake_client.agent_network_interfaces.side_effect = RuntimeError("guest agent is not running")
    r = inv(["--json", "vm", "ip", "150"], creds)
    assert r.exit_code == 1, r.output
    payload = json.loads(r.output)
    assert payload["ok"] is False and payload["error"] == "error"
    assert "agent: 1" in payload["message"]


def test_describe_includes_network(fake_client, creds):
    fake_client.resolve_node.return_value = "lukeserver"
    fake_client.guest_status.return_value = {"status": "running"}
    fake_client.guest_config.return_value = {"cores": 2}
    fake_client.list_snapshots.return_value = []
    fake_client.list_tasks.return_value = []
    fake_client.cluster_resources.return_value = _ip_row()
    fake_client.agent_network_interfaces.return_value = {"result": [
        {"name": "eth0", "hardware-address": "x", "ip-addresses": [
            {"ip-address-type": "ipv4", "ip-address": "192.168.1.50", "prefix": 24}]},
    ]}
    r = inv(["--json", "vm", "describe", "150"], creds)
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["network"]["available"] is True and data["network"]["primary"] == "192.168.1.50"


def test_describe_human_network_unavailable(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    fake_client.guest_status.return_value = {"status": "running"}
    fake_client.guest_config.return_value = {}
    fake_client.list_snapshots.return_value = []
    fake_client.list_tasks.return_value = []
    fake_client.cluster_resources.return_value = [{"vmid": 100, "node": "pve1", "name": "x"}]
    fake_client.agent_network_interfaces.side_effect = RuntimeError("agent down")
    r = inv(["--no-json", "vm", "describe", "100"], creds)
    assert r.exit_code == 0, r.output
    assert "network: unavailable" in plain(r.output)


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


def test_ct_new_creates_container(fake_client, creds, tmp_path, monkeypatch):
    import pmox.cli as cli
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAA user@host")
    fake_client.list_appliances.return_value = [{"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    fake_client.storage_content.return_value = []
    fake_client.download_appliance.return_value = "UPID:apl"
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "ct", "new", "box", "--template", "ubuntu-24.04", "--node", "pve1",
             "--vmid", "300", "--disk", "8", "--ssh-key", str(key), "--ip", "dhcp"], creds)
    assert r.exit_code == 0, r.output
    create_kwargs = fake_client.create_guest.call_args.kwargs
    assert create_kwargs["ostemplate"] == "local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst"
    assert create_kwargs["rootfs"] == "local-lvm:8"
    assert create_kwargs["ssh-public-keys"] == "ssh-ed25519 AAAA user@host"
    assert create_kwargs["hostname"] == "box"


def test_ct_new_dry_run(fake_client, creds):
    fake_client.list_appliances.return_value = [{"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    fake_client.storage_content.return_value = []
    r = inv(["--dry-run", "ct", "new", "box", "--template", "ubuntu-24.04", "--node", "pve1", "--vmid", "300"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["op"] == "lxc.new"
    assert payload["plan"][-1]["op"] in {"create_guest", "guest_power"}
    fake_client.create_guest.assert_not_called()


def test_ct_new_needs_dangerous(fake_client, creds):
    fake_client.list_appliances.return_value = [{"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    fake_client.storage_content.return_value = []
    r = inv(["ct", "new", "box", "--template", "ubuntu-24.04", "--node", "pve1", "--vmid", "300"], creds)
    assert r.exit_code == 4, r.output
    fake_client.create_guest.assert_not_called()


def test_vm_has_no_ct_template_option(fake_client, creds):
    # `vm new` must NOT accept --template (that's ct-only)
    r = inv(["--dangerous", "vm", "new", "web", "--template", "ubuntu-24.04", "--node", "pve1", "--vmid", "300"], creds)
    assert r.exit_code != 0


# ------------------------------------------------- Task 5 (C1): vm new --image cloud-init mode --


def test_vm_new_image_creates_cloudinit_vm(fake_client, creds, tmp_path, monkeypatch):
    import pmox.cli as cli
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)  # don't actually sleep while polling
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAA user@host")
    fake_client.storage_content.return_value = []
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.download_url.return_value = "UPID:dl"
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(
        ["--dangerous", "vm", "new", "web", "--image", "ubuntu-24.04", "--node", "pve1",
         "--vmid", "150", "--disk", "50", "--ssh-key", str(key), "--ip", "dhcp", "--ciuser", "ubuntu"],
        creds,
    )
    assert r.exit_code == 0, r.output
    fake_client.download_url.assert_called_once()
    create_kwargs = fake_client.create_guest.call_args.kwargs
    assert create_kwargs["ide2"] == "local-lvm:cloudinit"
    assert create_kwargs["ciuser"] == "ubuntu"
    assert "import-from=local:import/noble-server-cloudimg-amd64.qcow2" in create_kwargs["scsi0"]
    fake_client.resize_disk.assert_called_once_with(node="pve1", kind="qemu", vmid=150, disk="scsi0", size="50G")


def test_vm_new_image_dry_run_prints_plan(fake_client, creds):
    fake_client.storage_content.return_value = []
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    r = inv(["--dry-run", "vm", "new", "web", "--image", "ubuntu-24.04", "--node", "pve1", "--vmid", "150"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["op"] == "qemu.new.image"
    assert [s["op"] for s in payload["plan"]] == ["download_url", "create_guest", "guest_power"]
    fake_client.create_guest.assert_not_called()


def test_vm_new_image_needs_dangerous(fake_client, creds):
    fake_client.storage_content.return_value = []
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    r = inv(["vm", "new", "web", "--image", "ubuntu-24.04", "--node", "pve1", "--vmid", "150"], creds)
    assert r.exit_code == 4, r.output
    fake_client.create_guest.assert_not_called()


def test_vm_new_blank_still_works(fake_client, creds):
    # no --image → the B2 blank-shell path is unchanged
    r = inv(["--dangerous", "vm", "new", "--node", "pve1", "--vmid", "150"], creds)
    assert r.exit_code == 0, r.output
    fake_client.create_guest.assert_called_once()
    assert "ide2" not in fake_client.create_guest.call_args.kwargs


def test_vm_new_image_passes_o_options(fake_client, creds, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.storage_content.return_value = []
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.download_url.return_value = "UPID:dl"
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(
        ["--dangerous", "vm", "new", "web", "--image", "ubuntu-24.04",
         "--node", "pve1", "--vmid", "150", "-o", "agent=0"],
        creds,
    )
    assert r.exit_code == 0, r.output
    assert fake_client.create_guest.call_args.kwargs["agent"] == "0"


def test_vm_new_image_auto_routes_off_lvmthin(fake_client, creds, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.storage_content.return_value = []
    fake_client.download_url.return_value = "UPID:dl"
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "vm", "new", "web", "--image", "ubuntu-24.04",
             "--node", "pve1", "--vmid", "150", "--storage", "local-lvm"], creds)
    assert r.exit_code == 0, r.output
    assert fake_client.download_url.call_args.kwargs["storage"] == "local"


def test_vm_new_image_explicit_import_storage(fake_client, creds, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.storage_content.return_value = []
    fake_client.download_url.return_value = "UPID:dl"
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "vm", "new", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--vmid", "150", "--import-storage", "local"], creds)
    assert r.exit_code == 0, r.output
    assert fake_client.download_url.call_args.kwargs["storage"] == "local"


def test_vm_new_image_no_import_storage_errors(fake_client, creds):
    fake_client.list_storage.return_value = [
        {"storage": "local-lvm", "content": "images,rootdir", "plugintype": "lvmthin"},
    ]
    r = inv(["--dangerous", "vm", "new", "web", "--image", "ubuntu-24.04", "--node", "pve1", "--vmid", "150"], creds)
    assert r.exit_code == 1, r.output
    fake_client.create_guest.assert_not_called()


# ------------------------------------------------- Task 6 (C1): image list + image pull --


def test_image_list_json(fake_client, creds):
    r = inv(["--json", "image", "list"], creds)
    assert r.exit_code == 0, r.output
    names = [row["name"] for row in json.loads(r.output)]
    assert "ubuntu-24.04" in names


def test_image_pull_downloads(fake_client, creds, monkeypatch):
    import pmox.cli as cli
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.storage_content.return_value = []
    fake_client.download_url.return_value = "UPID:dl"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "image", "pull", "ubuntu-24.04", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    fake_client.download_url.assert_called_once()
    assert fake_client.download_url.call_args.kwargs["content"] == "import"


def test_image_pull_needs_dangerous(fake_client, creds):
    r = inv(["image", "pull", "ubuntu-24.04", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 4, r.output
    fake_client.download_url.assert_not_called()


def test_image_pull_cached_skips(fake_client, creds):
    fake_client.storage_content.return_value = [{"volid": "local:import/noble-server-cloudimg-amd64.qcow2"}]
    r = inv(["--dangerous", "image", "pull", "ubuntu-24.04", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    fake_client.download_url.assert_not_called()


def test_image_pull_dry_run(fake_client, creds):
    r = inv(["--dry-run", "image", "pull", "ubuntu-24.04", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["op"] == "image.pull"
    fake_client.download_url.assert_not_called()


def test_image_pull_rejects_volid(fake_client, creds):
    r = inv(["--dangerous", "image", "pull", "local:import/x.qcow2", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 1, r.output
    fake_client.download_url.assert_not_called()


# ------------------------------------------------- Task 4 (C2): vm new --from-template --


def test_vm_new_from_template_clones_and_sets_ci(fake_client, creds, tmp_path, monkeypatch):
    import pmox.cli as cli
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAA user@host")
    fake_client.resolve_node.return_value = "pve1"
    fake_client.clone_guest.return_value = "UPID:clone"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "vm", "new", "web", "--from-template", "9000", "--vmid", "120",
             "--ssh-key", str(key), "--ip", "dhcp", "--ciuser", "ubuntu"], creds)
    assert r.exit_code == 0, r.output
    fake_client.clone_guest.assert_called_once_with(node="pve1", kind="qemu", vmid=9000, newid=120, name="web", full=1)
    ci = fake_client.update_config.call_args.kwargs
    assert ci["ciuser"] == "ubuntu" and ci["ipconfig0"] == "ip=dhcp"
    assert ci["vmid"] == 120 and ci["node"] == "pve1"


def test_vm_new_from_template_dry_run(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dry-run", "vm", "new", "web", "--from-template", "9000", "--vmid", "120"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["op"] == "qemu.new.from_template"
    assert payload["plan"][0]["op"] == "clone_guest"
    fake_client.clone_guest.assert_not_called()


def test_vm_new_image_and_template_mutually_exclusive(fake_client, creds):
    r = inv(["--dangerous", "vm", "new", "web", "--image", "ubuntu-24.04", "--from-template", "9000", "--node", "pve1"], creds)
    assert r.exit_code == 1, r.output


def test_vm_new_from_template_needs_dangerous(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["vm", "new", "web", "--from-template", "9000", "--vmid", "120"], creds)
    assert r.exit_code == 4, r.output
    fake_client.clone_guest.assert_not_called()


def test_vm_new_from_template_resolve_fails(fake_client, creds):
    fake_client.resolve_node.return_value = None
    r = inv(["--dangerous", "vm", "new", "web", "--from-template", "9000", "--vmid", "120"], creds)
    assert r.exit_code == 1, r.output
    fake_client.clone_guest.assert_not_called()


# ------------------------------------------------- Task 5 (C2): image pull --as-template --


def test_image_pull_as_template_builds_template(fake_client, creds, monkeypatch):
    import pmox.cli as cli
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.storage_content.return_value = []
    fake_client.download_url.return_value = "UPID:dl"
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "image", "pull", "ubuntu-24.04", "--storage", "local", "--node", "pve1",
             "--as-template", "--vmid", "9000"], creds)
    assert r.exit_code == 0, r.output
    fake_client.create_guest.assert_called_once()
    fake_client.convert_to_template.assert_called_once_with(node="pve1", kind="qemu", vmid=9000)


def test_image_pull_as_template_dry_run(fake_client, creds):
    fake_client.storage_content.return_value = []
    r = inv(["--dry-run", "image", "pull", "ubuntu-24.04", "--storage", "local", "--node", "pve1",
             "--as-template", "--vmid", "9000"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["op"] == "image.pull.template"
    assert payload["plan"][-1]["op"] == "convert_to_template"
    fake_client.create_guest.assert_not_called()


def test_image_pull_as_template_needs_dangerous(fake_client, creds):
    fake_client.storage_content.return_value = []
    r = inv(["image", "pull", "ubuntu-24.04", "--storage", "local", "--node", "pve1", "--as-template", "--vmid", "9000"], creds)
    assert r.exit_code == 4, r.output
    fake_client.create_guest.assert_not_called()


# ------------------------------------------------- Task 4 (C3): image list --ct and image pull --ct --


def test_image_list_ct(fake_client, creds):
    fake_client.list_appliances.return_value = [
        {"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst", "type": "lxc"},
    ]
    r = inv(["--json", "image", "list", "--ct", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    assert any("ubuntu-24.04" in row["template"] for row in json.loads(r.output))
    fake_client.list_appliances.assert_called_once_with("pve1")


def test_image_list_ct_requires_node(fake_client, creds):
    r = inv(["--json", "image", "list", "--ct"], creds)
    assert r.exit_code == 1, r.output
    fake_client.list_appliances.assert_not_called()


def test_image_pull_ct_downloads(fake_client, creds, monkeypatch):
    import pmox.cli as cli
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.list_appliances.return_value = [{"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    fake_client.storage_content.return_value = []
    fake_client.download_appliance.return_value = "UPID:apl"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "image", "pull", "ubuntu-24.04", "--ct", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    fake_client.download_appliance.assert_called_once_with("pve1", "local", "ubuntu-24.04-standard_24.04-2_amd64.tar.zst")


def test_image_pull_ct_needs_dangerous(fake_client, creds):
    fake_client.list_appliances.return_value = [{"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    fake_client.storage_content.return_value = []
    r = inv(["image", "pull", "ubuntu-24.04", "--ct", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 4, r.output
    fake_client.download_appliance.assert_not_called()


def test_image_pull_ct_cached_skips(fake_client, creds):
    fake_client.list_appliances.return_value = [{"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    fake_client.storage_content.return_value = [{"volid": "local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    r = inv(["--dangerous", "image", "pull", "ubuntu-24.04", "--ct", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    fake_client.download_appliance.assert_not_called()


def test_image_pull_ct_unknown_template(fake_client, creds):
    fake_client.list_appliances.return_value = [{"template": "debian-12-standard_12.7-1_amd64.tar.zst"}]
    r = inv(["--dangerous", "image", "pull", "ubuntu-24.04", "--ct", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 1, r.output
    fake_client.download_appliance.assert_not_called()


def test_image_pull_ct_dry_run(fake_client, creds):
    fake_client.list_appliances.return_value = [{"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    r = inv(["--dry-run", "image", "pull", "ubuntu-24.04", "--ct", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["op"] == "image.pull.ct"
    assert payload["dry_run"] is True
    fake_client.download_appliance.assert_not_called()


def test_image_pull_ct_and_as_template_exclusive(fake_client, creds):
    r = inv(["--dangerous", "image", "pull", "ubuntu-24.04", "--ct", "--as-template", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 1, r.output


# --- vm up command ---


def _net_creds(creds):
    return {**creds, "PROXMOX_NET_CIDR": "192.168.0.0/24",
            "PROXMOX_NET_GATEWAY": "192.168.0.1",
            "PROXMOX_NET_POOL": "192.168.0.200-192.168.0.250"}


def test_vm_up_allocates_ip_and_creates(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.cluster_resources.return_value = []
    fake_client.storage_content.return_value = []
    fake_client.download_url.return_value = "UPID:dl"
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", str(key), "--ciuser", "ubuntu"], _net_creds(creds))
    assert r.exit_code == 0, r.output
    create = fake_client.create_guest.call_args.kwargs
    assert create["ipconfig0"] == "ip=192.168.0.200/24,gw=192.168.0.1"
    assert "import-from=local:import/noble-server-cloudimg-amd64.qcow2" in create["scsi0"]
    assert create["ide2"] == "local-lvm:cloudinit"
    assert "192.168.0.200" in r.output and "ssh ubuntu@192.168.0.200" in r.output


def test_vm_up_json_output(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.cluster_resources.return_value = []
    fake_client.storage_content.return_value = []
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--json", "--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", str(key), "--ciuser", "ubuntu"], _net_creds(creds))
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["ip"] == "192.168.0.200"
    assert out["ssh"] == "ssh ubuntu@192.168.0.200"


def test_vm_up_explicit_ip_skips_allocation(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.storage_content.return_value = []
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--ip", "192.168.0.77/24,gw=192.168.0.1", "--ssh-key", str(key)], creds)
    assert r.exit_code == 0, r.output
    fake_client.cluster_resources.assert_not_called()
    assert fake_client.create_guest.call_args.kwargs["ipconfig0"] == "ip=192.168.0.77/24,gw=192.168.0.1"
    out = json.loads(r.output)
    assert out["ip"] == "192.168.0.77"
    assert out["ssh"] is None  # no ciuser -> JSON ssh is null; human output prints a placeholder instead


def test_vm_up_no_config_defaults_to_dhcp(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.storage_content.return_value = []
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    # creds has no [network] pool and no --ip -> DHCP, zero config required
    r = inv(["--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", str(key)], creds)
    assert r.exit_code == 0, r.output
    fake_client.cluster_resources.assert_not_called()  # no static allocation attempted
    assert fake_client.create_guest.call_args.kwargs["ipconfig0"] == "ip=dhcp"
    out = json.loads(r.output)
    assert out["ip"] is None and out["ssh"] is None


def test_vm_up_dhcp_human_output(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.storage_content.return_value = []
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--no-json", "--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04",
             "--node", "pve1", "--ssh-key", str(key)], creds)
    assert r.exit_code == 0, r.output
    assert "DHCP" in r.output


def test_vm_up_from_template_clones(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.resolve_node.return_value = "pve1"
    fake_client.cluster_nextid.return_value = "150"
    fake_client.cluster_resources.return_value = []  # pool free -> .200
    fake_client.clone_guest.return_value = "UPID:clone"
    fake_client.update_config.return_value = "UPID:cfg"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "vm", "up", "web", "--from-template", "9000",
             "--ssh-key", str(key), "--ciuser", "ubuntu"], _net_creds(creds))
    assert r.exit_code == 0, r.output
    fake_client.clone_guest.assert_called_once()
    fake_client.resolve_node.assert_called_with(9000)  # node from the template
    cfg = fake_client.update_config.call_args.kwargs
    assert cfg["ipconfig0"] == "ip=192.168.0.200/24,gw=192.168.0.1"
    assert "sshkeys" in cfg
    assert "192.168.0.200" in r.output


def test_vm_up_from_template_dhcp(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.resolve_node.return_value = "pve1"
    fake_client.cluster_nextid.return_value = "150"
    fake_client.clone_guest.return_value = "UPID:clone"
    fake_client.update_config.return_value = "UPID:cfg"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    # plain creds (no [network] pool) and no --ip -> DHCP
    r = inv(["--dangerous", "vm", "up", "web", "--from-template", "9000", "--ssh-key", str(key)], creds)
    assert r.exit_code == 0, r.output
    fake_client.cluster_resources.assert_not_called()  # no static allocation
    assert fake_client.update_config.call_args.kwargs["ipconfig0"] == "ip=dhcp"


def test_vm_up_from_template_unresolvable_errors(fake_client, creds, tmp_path):
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.cluster_nextid.return_value = "150"
    fake_client.resolve_node.return_value = None  # template not found
    r = inv(["--dangerous", "vm", "up", "web", "--from-template", "9000", "--ssh-key", str(key)], creds)
    assert r.exit_code == 1, r.output
    fake_client.clone_guest.assert_not_called()


def test_vm_up_requires_image_or_template(fake_client, creds, tmp_path):
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    r = inv(["--dangerous", "vm", "up", "web", "--ssh-key", str(key)], creds)  # neither source
    assert r.exit_code == 1, r.output
    fake_client.create_guest.assert_not_called()
    fake_client.clone_guest.assert_not_called()


def test_vm_up_image_and_template_mutually_exclusive(fake_client, creds, tmp_path):
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    r = inv(["--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04",
             "--from-template", "9000", "--ssh-key", str(key)], creds)
    assert r.exit_code == 1, r.output
    fake_client.create_guest.assert_not_called()
    fake_client.clone_guest.assert_not_called()


def test_vm_up_dry_run(fake_client, creds, tmp_path):
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.cluster_resources.return_value = []
    fake_client.storage_content.return_value = []
    r = inv(["--dry-run", "vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", str(key)], _net_creds(creds))
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["op"] == "qemu.up"
    fake_client.create_guest.assert_not_called()


def test_vm_up_needs_dangerous(fake_client, creds, tmp_path):
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.cluster_resources.return_value = []
    fake_client.storage_content.return_value = []
    r = inv(["vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", str(key)], _net_creds(creds))
    assert r.exit_code == 4, r.output
    fake_client.create_guest.assert_not_called()


def test_vm_up_no_ssh_key(fake_client, creds, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.cluster_resources.return_value = []
    fake_client.storage_content.return_value = []
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--no-ssh-key", "--ciuser", "ubuntu"], _net_creds(creds))
    assert r.exit_code == 0, r.output
    assert "sshkeys" not in fake_client.create_guest.call_args.kwargs


def test_vm_up_human_output(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.cluster_resources.return_value = []
    fake_client.storage_content.return_value = []
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--no-json", "--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", str(key), "--ciuser", "ubuntu"], _net_creds(creds))
    assert r.exit_code == 0, r.output
    out = plain(r.output)
    assert "192.168.0.200" in out
    assert "ssh ubuntu@192.168.0.200" in out


def test_vm_up_dry_run_does_not_generate_key(fake_client, creds, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli.provision.subprocess, "run", lambda *a, **k: calls.append(a))
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.cluster_resources.return_value = []
    fake_client.storage_content.return_value = []
    missing = tmp_path / "newkey.pub"
    r = inv(["--dry-run", "vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", str(missing)], _net_creds(creds))
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["op"] == "qemu.up"
    assert calls == []            # ssh-keygen never invoked
    assert not missing.exists()   # no key generated during dry-run


def test_vm_up_readonly_does_not_generate_key(fake_client, creds, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli.provision.subprocess, "run", lambda *a, **k: calls.append(a))
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.cluster_resources.return_value = []
    fake_client.storage_content.return_value = []
    missing = tmp_path / "newkey.pub"
    r = inv(["vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", str(missing)], _net_creds(creds))  # no --dangerous -> exit 4
    assert r.exit_code == 4, r.output
    assert calls == []            # gate blocked BEFORE any key generation
    assert not missing.exists()


# ----------------------------------- structured success envelopes (vmid & co) --


def test_vm_new_image_envelope_has_vmid(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.storage_content.return_value = []
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.download_url.return_value = "UPID:dl"
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--json", "--dangerous", "vm", "new", "web", "--image", "ubuntu-24.04",
             "--node", "pve1", "--vmid", "150"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["ok"] is True
    assert payload["vmid"] == 150 and payload["node"] == "pve1"
    assert payload["op"] == "qemu.new.image"


def test_vm_new_from_template_envelope_has_vmid(fake_client, creds, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.resolve_node.return_value = "pve1"
    fake_client.clone_guest.return_value = "UPID:clone"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--json", "--dangerous", "vm", "new", "web", "--from-template", "9000", "--vmid", "120"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["vmid"] == 120 and payload["node"] == "pve1"
    assert payload["op"] == "qemu.new.from_template"
    assert payload["template"] == 9000


def test_ct_new_envelope_has_vmid(fake_client, creds, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.list_appliances.return_value = [{"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    fake_client.storage_content.return_value = []
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.download_appliance.return_value = "UPID:apl"
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--json", "--dangerous", "ct", "new", "box", "--template", "ubuntu-24.04",
             "--node", "pve1", "--vmid", "300"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["vmid"] == 300 and payload["node"] == "pve1"
    assert payload["op"] == "lxc.new"


def test_image_pull_envelope_has_volid_and_upid(fake_client, creds, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.storage_content.return_value = []
    fake_client.download_url.return_value = "UPID:dl"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--json", "--dangerous", "image", "pull", "ubuntu-24.04", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["volid"] == "local:import/noble-server-cloudimg-amd64.qcow2"
    assert payload["upid"] == "UPID:dl"
    assert payload["op"] == "image.pull"


def test_image_pull_cached_envelope_has_volid(fake_client, creds):
    fake_client.storage_content.return_value = [{"volid": "local:import/noble-server-cloudimg-amd64.qcow2"}]
    r = inv(["--json", "--dangerous", "image", "pull", "ubuntu-24.04", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["volid"] == "local:import/noble-server-cloudimg-amd64.qcow2"
    assert "upid" not in payload  # nothing downloaded


def test_image_pull_as_template_envelope_has_vmid(fake_client, creds, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.storage_content.return_value = []
    fake_client.download_url.return_value = "UPID:dl"
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--json", "--dangerous", "image", "pull", "ubuntu-24.04", "--storage", "local", "--node", "pve1",
             "--as-template", "--vmid", "9000"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["vmid"] == 9000 and payload["node"] == "pve1"
    assert payload["op"] == "image.pull.template"


def test_image_pull_ct_envelope_has_volid(fake_client, creds, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.list_appliances.return_value = [{"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    fake_client.storage_content.return_value = []
    fake_client.download_appliance.return_value = "UPID:apl"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--json", "--dangerous", "image", "pull", "ubuntu-24.04", "--ct", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["volid"] == "local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst"
    assert payload["op"] == "image.pull.ct"


def test_vm_up_json_envelope_dhcp_hint(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.storage_content.return_value = []
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--json", "--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", str(key)], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["ok"] is True and payload["op"] == "qemu.up"
    assert payload["vmid"] == 150 and payload["ip"] is None
    assert "hint" in payload  # machine-actionable next step for the DHCP case


def test_clone_envelope_reports_new_vmid(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    fake_client.clone_guest.return_value = "UPID:clone"
    r = inv(["--json", "--dangerous", "vm", "clone", "100", "--newid", "105"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["vmid"] == 105  # the clone's id, not the source


# ------------------------------------- structured failure envelopes (errors.py) --


def test_wait_failed_task_envelope_has_upid_node_hint(fake_client, creds, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.resolve_node.return_value = "pve1"
    fake_client.guest_power.return_value = "UPID:pve1:dead"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "got signal 11"}
    r = inv(["--json", "--dangerous", "--wait", "vm", "start", "100"], creds)
    assert r.exit_code == 1, r.output
    payload = json.loads(r.output)
    assert payload["ok"] is False and payload["error"] == "error"
    assert payload["upid"] == "UPID:pve1:dead"
    assert payload["node"] == "pve1"
    assert "task log" in payload["hint"]


def test_error_human_mode_prints_hint(fake_client, creds, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.resolve_node.return_value = "pve1"
    fake_client.guest_power.return_value = "UPID:pve1:dead"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "boom"}
    r = inv(["--no-json", "--dangerous", "--wait", "vm", "start", "100"], creds)
    assert r.exit_code == 1, r.output
    assert "task log" in plain(r.output)


# -------------------------------------------------- friendly network errors --


def test_network_error_dns_concise(fake_client, creds):
    import requests

    wall = (
        "HTTPSConnectionPool(host='pve.local', port=8006): Max retries exceeded with url: "
        "/api2/json/version (Caused by NameResolutionError(\"<urllib3.connection.HTTPSConnection object>: "
        "Failed to resolve 'pve.local' ([Errno 11001] getaddrinfo failed)\"))"
    )
    fake_client.version.side_effect = requests.exceptions.ConnectionError(wall)
    r = inv(["--json", "version"], creds)
    assert r.exit_code == 1, r.output
    payload = json.loads(r.output)
    assert payload["error"] == "network"
    assert "Failed to resolve 'pve.local'" in payload["message"]
    assert "Max retries" not in payload["message"]
    assert "PROXMOX_HOST" in payload["message"]


def test_network_error_ssl_mentions_verify_flag(fake_client, creds):
    import requests

    fake_client.version.side_effect = requests.exceptions.SSLError(
        "certificate verify failed: self-signed certificate"
    )
    r = inv(["--json", "version"], creds)
    assert r.exit_code == 1, r.output
    payload = json.loads(r.output)
    assert payload["error"] == "network"
    assert "--no-verify-ssl" in payload["message"] or "PROXMOX_VERIFY_SSL" in payload["message"]


def test_network_error_timeout(fake_client, creds):
    import requests

    fake_client.version.side_effect = requests.exceptions.ReadTimeout("read timed out")
    r = inv(["--json", "version"], creds)
    assert r.exit_code == 1, r.output
    payload = json.loads(r.output)
    assert payload["error"] == "network"
    assert "imed out" in payload["message"]


def test_network_error_human_label(fake_client, creds):
    import requests

    fake_client.version.side_effect = requests.exceptions.ConnectionError("Connection refused")
    r = inv(["--no-json", "version"], creds)
    assert r.exit_code == 1, r.output
    assert "Network error" in plain(r.output)


def test_concise_network_reason_resolve():
    msg = "blah (Caused by NameResolutionError(\"Failed to resolve 'pve.local' (no DNS)\"))"
    assert cli._concise_network_reason(Exception(msg)) == "Failed to resolve 'pve.local'"


def test_concise_network_reason_errno():
    out = cli._concise_network_reason(Exception("x (Caused by ... [Errno 111] Connection refused))"))
    assert out.startswith("[Errno 111] Connection refused")


def test_concise_network_reason_keyword():
    assert "refused" in cli._concise_network_reason(Exception("NewConnectionError: connection refused by peer"))


def test_concise_network_reason_fallback_truncates():
    assert len(cli._concise_network_reason(Exception("x" * 500))) <= 160


# ------------------------------------------------- usage errors via main() --


def _run_main(monkeypatch, capsys, argv, env=None):
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli.sys, "argv", ["pmox", *argv])
    with pytest.raises(SystemExit) as ei:
        cli.main()
    out, err = capsys.readouterr()
    code = ei.value.code
    return (0 if code is None else code), out, err


def test_main_unknown_command_json_envelope(monkeypatch, capsys):
    code, out, err = _run_main(monkeypatch, capsys, ["vm", "badcmd"], env={"PMOX_JSON": "1"})
    assert code == 2
    payload = json.loads(out)
    assert payload["ok"] is False and payload["error"] == "usage"
    assert "badcmd" in payload["message"]
    assert "--help" in payload["hint"]


def test_main_unknown_option_json_envelope(monkeypatch, capsys):
    code, out, err = _run_main(monkeypatch, capsys, ["vm", "list", "--bogus"], env={"PMOX_JSON": "1"})
    assert code == 2
    payload = json.loads(out)
    assert payload["error"] == "usage"
    assert "--bogus" in payload["message"]


def test_main_usage_error_human_keeps_click_text(monkeypatch, capsys):
    code, out, err = _run_main(monkeypatch, capsys, ["vm", "badcmd"], env={"PMOX_JSON": "0"})
    assert code == 2
    assert "badcmd" in err
    assert out == ""


def test_main_no_args_json_short_message(monkeypatch, capsys):
    code, out, err = _run_main(monkeypatch, capsys, [], env={"PMOX_JSON": "1"})
    assert code == 2
    payload = json.loads(out)
    assert payload["error"] == "usage"
    assert len(payload["message"]) < 200  # not the full help wall
    assert "--help" in payload["hint"]


def test_main_no_args_human_shows_help(monkeypatch, capsys):
    code, out, err = _run_main(monkeypatch, capsys, [], env={"PMOX_JSON": "0"})
    assert code == 2
    assert "Usage" in (out + err)


def test_main_version_flag_exits_zero(monkeypatch, capsys):
    code, out, err = _run_main(monkeypatch, capsys, ["--version"])
    assert code == 0
    assert "pmox" in out


def test_main_translates_abort_to_exit1(monkeypatch, capsys):
    def fake_app(**kw):
        raise cli.click.exceptions.Abort()

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(cli.sys, "argv", ["pmox", "vm", "list"])
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert ei.value.code == 1


def test_main_other_click_exception_json(monkeypatch, capsys):
    def fake_app(**kw):
        raise cli.click.exceptions.ClickException("kaboom")

    monkeypatch.setattr(cli, "app", fake_app)
    code, out, err = _run_main(monkeypatch, capsys, ["vm", "list"], env={"PMOX_JSON": "1"})
    assert code == 1
    payload = json.loads(out)
    assert payload["error"] == "error" and "kaboom" in payload["message"]


def test_main_other_click_exception_human(monkeypatch, capsys):
    def fake_app(**kw):
        raise cli.click.exceptions.ClickException("kaboom")

    monkeypatch.setattr(cli, "app", fake_app)
    code, out, err = _run_main(monkeypatch, capsys, ["vm", "list"], env={"PMOX_JSON": "0"})
    assert code == 1
    assert "kaboom" in err


def test_main_dangling_subgroup_json_envelope(monkeypatch, capsys):
    code, out, err = _run_main(monkeypatch, capsys, ["vm"], env={"PMOX_JSON": "1"})
    assert code == 2
    payload = json.loads(out)
    assert payload["error"] == "usage"
    assert payload["message"] == "Missing command for `pmox vm`."


def test_dangling_group_path_cases():
    assert cli._dangling_group_path([]) == "pmox"
    assert cli._dangling_group_path(["vm"]) == "pmox vm"
    assert cli._dangling_group_path(["--json", "vm", "snapshot"]) == "pmox vm snapshot"
    assert cli._dangling_group_path(["--timeout", "5"]) == "pmox"   # globals only
    assert cli._dangling_group_path(["vm", "list"]) is None         # complete command
    assert cli._dangling_group_path(["vm", "nope"]) is None         # unknown token
    assert cli._dangling_group_path(["--version"]) is None          # eager option short-circuits
    assert cli._dangling_group_path(["--timeout"]) is None          # value flag missing its value
    assert cli._dangling_group_path(["vm", "--help"]) is None       # option token


def test_handle_parse_error_no_args_json(monkeypatch, capsys):
    monkeypatch.setenv("PMOX_JSON", "1")
    cmd = cli.typer.main.get_command(cli.app)
    ctx = cli.click.core.Context(cmd, info_name="pmox")
    exc = cli.click.exceptions.NoArgsIsHelpError(ctx)
    capsys.readouterr()  # swallow the help printed as a ctor side effect
    code = cli._handle_parse_error(["vm"], exc)
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "usage"
    assert payload["message"].startswith("Missing command")


def test_handle_parse_error_no_args_human_does_not_reprint(monkeypatch, capsys):
    monkeypatch.setenv("PMOX_JSON", "0")
    cmd = cli.typer.main.get_command(cli.app)
    ctx = cli.click.core.Context(cmd, info_name="pmox")
    exc = cli.click.exceptions.NoArgsIsHelpError(ctx)
    capsys.readouterr()
    code = cli._handle_parse_error(["vm"], exc)
    out, err = capsys.readouterr()
    assert code == 2
    assert out == "" and err == ""  # the ctor side effect already printed the help


def test_json_mode_from_argv_flag_beats_env(monkeypatch):
    monkeypatch.setenv("PMOX_JSON", "0")
    assert cli._json_mode_from_argv(["--json", "vm", "list"]) is True
    monkeypatch.setenv("PMOX_JSON", "1")
    assert cli._json_mode_from_argv(["--no-json", "vm", "list"]) is False


def test_json_mode_from_argv_ignores_after_double_dash(monkeypatch):
    monkeypatch.setenv("PMOX_JSON", "0")
    assert cli._json_mode_from_argv(["vm", "list", "--", "--json"]) is False
