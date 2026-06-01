"""The pmox command-line interface.

Command groups: ``nodes``, ``vm``, ``ct``, ``storage``, ``cluster``, ``task``
(with a nested ``snapshot`` group under ``vm``/``ct``), plus a top-level
``version``.

Global options live on the root callback and must precede the subcommand, e.g.::

    pmox --json vm list
    pmox --host 10.0.0.2 nodes list

Set ``PMOX_JSON=1`` to default to JSON output (handy when an AI drives the CLI).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

import typer

from . import __version__
from .client import ProxmoxClient
from .config import ConfigError, Settings, _parse_bool, load_settings
from .output import (
    Column,
    console,
    emit,
    err_console,
    fmt_epoch,
    human_bytes,
    human_uptime,
    percent,
    status_fmt,
)
from .safety import ConfirmationRequired, DangerousNotEnabled, confirm, require_dangerous

# Indirection so tests can inject a fake client factory.
_client_factory = ProxmoxClient.from_settings


class State:
    def __init__(self, settings: Settings, json_output: bool = False, dangerous: bool = False):
        self.settings = settings
        self.json = json_output
        self.dangerous = dangerous
        self.client: Optional[ProxmoxClient] = None


def _get_client(ctx: typer.Context) -> ProxmoxClient:
    state: State = ctx.obj
    if state.client is None:
        state.settings.validate()
        state.client = _client_factory(state.settings)
    return state.client


def _require_dangerous(ctx: typer.Context) -> None:
    """Refuse a mutating command unless dangerous (write) mode is enabled."""
    require_dangerous(ctx.obj.dangerous)


def _resolve_node_or_die(client: ProxmoxClient, vmid: int) -> str:
    node = client.resolve_node(vmid)
    if not node:
        err_console.print(
            f"[red]Error:[/red] Could not locate guest {vmid} in the cluster. "
            "Pass --node to specify it explicitly."
        )
        raise typer.Exit(1)
    return node


def _ok(ctx: typer.Context, message: str, result=None) -> None:
    state: State = ctx.obj
    if state.json:
        print(json.dumps({"ok": True, "message": message, "result": result}, default=str, indent=2))
    else:
        console.print(f"[green]✓[/green] {message}")
        if result:
            console.print(f"  [dim]{result}[/dim]")


@contextmanager
def error_boundary():
    """Translate exceptions into friendly messages and distinct exit codes."""
    try:
        yield
    except typer.Exit:
        raise
    except DangerousNotEnabled as exc:
        err_console.print(f"[yellow]Read-only:[/yellow] {exc}")
        raise typer.Exit(4)
    except ConfirmationRequired as exc:
        err_console.print(f"[yellow]Aborted:[/yellow] {exc}")
        raise typer.Exit(3)
    except ConfigError as exc:
        err_console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(2)
    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)


# ---- shared option / argument definitions ----
vmid_arg = typer.Argument(..., metavar="VMID", help="Numeric VM/CT id.")
node_opt = typer.Option(None, "--node", "-n", help="Node name (auto-resolved from the cluster if omitted).")
yes_opt = typer.Option(False, "--yes", "-y", help="Confirm a destructive operation (required when non-interactive).")


# ---- column specifications ----
NODE_COLUMNS = [
    Column("Node", "node"),
    Column("Status", "status", status_fmt),
    Column("CPU", "cpu", percent),
    Column("Cores", "maxcpu"),
    Column("Mem", "mem", human_bytes),
    Column("Max Mem", "maxmem", human_bytes),
    Column("Uptime", "uptime", human_uptime),
]

GUEST_COLUMNS = [
    Column("VMID", "vmid"),
    Column("Name", "name"),
    Column("Type", "type"),
    Column("Status", "status", status_fmt),
    Column("Node", "node"),
    Column("CPU", "cpu", percent),
    Column("Mem", "mem", human_bytes),
    Column("Max Mem", "maxmem", human_bytes),
    Column("Disk", "maxdisk", human_bytes),
    Column("Uptime", "uptime", human_uptime),
]

STORAGE_COLUMNS = [
    Column("Storage", "storage"),
    Column("Node", "node"),
    Column("Status", "status", status_fmt),
    Column("Backend", "plugintype"),
    Column("Used", "disk", human_bytes),
    Column("Total", "maxdisk", human_bytes),
    Column("Use%", row_formatter=lambda r: percent(r.get("disk"), r.get("maxdisk"))),
]

CONTENT_COLUMNS = [
    Column("Volid", "volid"),
    Column("Content", "content"),
    Column("Format", "format"),
    Column("Size", "size", human_bytes),
    Column("VMID", "vmid"),
]

SNAPSHOT_COLUMNS = [
    Column("Name", "name"),
    Column("Description", "description"),
    Column("Created", "snaptime", fmt_epoch),
    Column("Parent", "parent"),
]

TASK_COLUMNS = [
    Column("Type", "type"),
    Column("Status", "status", status_fmt),
    Column("Start", "starttime", fmt_epoch),
    Column("User", "user"),
    Column("ID", "id"),
    Column("UPID", row_formatter=lambda r: (r.get("upid", "") or "")[:48]),
]

CLUSTER_NODE_COLUMNS = [
    Column("Name", "name"),
    Column("Online", row_formatter=lambda r: status_fmt("online" if r.get("online") else "offline")),
    Column("IP", "ip"),
    Column("Level", "level"),
    Column("Node ID", "nodeid"),
]

RESOURCE_COLUMNS = [
    Column("Type", "type"),
    Column("Name", row_formatter=lambda r: str(r.get("name") or r.get("storage") or r.get("id") or "-")),
    Column("Node", "node"),
    Column("Status", "status", status_fmt),
    Column("CPU", "cpu", percent),
    Column("Mem", "mem", human_bytes),
    Column("Max Mem", "maxmem", human_bytes),
]


# ---- root app ----
def _version_callback(value: bool):
    if value:
        print(f"pmox {__version__}")
        raise typer.Exit()


app = typer.Typer(
    help="Explore and manage a Proxmox VE cluster from the command line (AI-friendly).",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main_callback(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON (machine-readable; great for AI use)."),
    dangerous: bool = typer.Option(
        False, "--dangerous", help="Enable dangerous (write/management) mode. Default is read-only (safe for AI exploration)."
    ),
    host: Optional[str] = typer.Option(None, "--host", help="Proxmox host or IP."),
    port: Optional[int] = typer.Option(None, "--port", help="API port (default 8006)."),
    token_id: Optional[str] = typer.Option(None, "--token-id", help="API token id: user@realm!tokenname."),
    token_secret: Optional[str] = typer.Option(None, "--token-secret", help="API token secret."),
    verify_ssl: Optional[bool] = typer.Option(None, "--verify-ssl/--no-verify-ssl", help="Verify TLS cert (default: no)."),
    config: Optional[str] = typer.Option(None, "--config", help="Path to a TOML config file."),
    version: Optional[bool] = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True, help="Show pmox version and exit."
    ),
):
    # Load a local .env if python-dotenv is available (never fatal).
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    overrides = {
        "host": host,
        "port": port,
        "token_id": token_id,
        "token_secret": token_secret,
        "verify_ssl": verify_ssl,
    }
    config_path = Path(config) if config else None
    try:
        settings = load_settings(config_path=config_path, overrides=overrides)
    except ConfigError as exc:
        err_console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(2)

    json_on = json_output or _parse_bool(os.environ.get("PMOX_JSON"))
    dangerous_on = dangerous or _parse_bool(os.environ.get("PMOX_DANGEROUS"))
    ctx.obj = State(settings=settings, json_output=json_on, dangerous=dangerous_on)


@app.command("version")
def server_version(ctx: typer.Context):
    """Show the Proxmox VE version of the connected node."""
    with error_boundary():
        client = _get_client(ctx)
        emit(client.version(), json_output=ctx.obj.json, title="Proxmox version")


# ---- nodes ----
nodes_app = typer.Typer(help="Inspect cluster nodes.", no_args_is_help=True)


@nodes_app.command("list")
def nodes_list(ctx: typer.Context):
    """List all nodes and their resource usage."""
    with error_boundary():
        client = _get_client(ctx)
        emit(client.list_nodes(), columns=NODE_COLUMNS, json_output=ctx.obj.json, title="Nodes")


@nodes_app.command("status")
def nodes_status(ctx: typer.Context, node: str = typer.Argument(..., help="Node name.")):
    """Show detailed status for a node."""
    with error_boundary():
        client = _get_client(ctx)
        emit(client.node_status(node), json_output=ctx.obj.json, title=f"Node {node}")


# ---- guest (vm / ct) app factory ----
def _make_power_command(group, kind, label, action, destructive, description):
    @group.command(action, help=f"{description} a {label}.")
    def _cmd(ctx: typer.Context, vmid: int = vmid_arg, node: Optional[str] = node_opt, yes: bool = yes_opt):
        with error_boundary():
            _require_dangerous(ctx)
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            if destructive:
                confirm(f"{action} {label.lower()} {vmid} on {resolved}", assume_yes=yes)
            result = client.guest_power(resolved, kind, vmid, action)
            _ok(ctx, f"{description}: {label.lower()} {vmid} on {resolved}", result)

    return _cmd


def build_guest_app(kind: str, label: str) -> typer.Typer:
    descr = "QEMU VMs" if kind == "qemu" else "LXC containers"
    group = typer.Typer(help=f"Manage {label}s ({descr}).", no_args_is_help=True)

    @group.command("list")
    def _list(ctx: typer.Context, node: Optional[str] = node_opt):
        with error_boundary():
            client = _get_client(ctx)
            emit(
                client.list_guests(kind, node=node),
                columns=GUEST_COLUMNS,
                json_output=ctx.obj.json,
                title=f"{label}s",
            )

    @group.command("status")
    def _status(ctx: typer.Context, vmid: int = vmid_arg, node: Optional[str] = node_opt):
        with error_boundary():
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            emit(client.guest_status(resolved, kind, vmid), json_output=ctx.obj.json, title=f"{label} {vmid} status")

    @group.command("config")
    def _config(ctx: typer.Context, vmid: int = vmid_arg, node: Optional[str] = node_opt):
        with error_boundary():
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            emit(client.guest_config(resolved, kind, vmid), json_output=ctx.obj.json, title=f"{label} {vmid} config")

    for action, destructive, description in [
        ("start", False, "Start"),
        ("shutdown", False, "Gracefully shut down"),
        ("reboot", False, "Reboot"),
        ("suspend", False, "Suspend"),
        ("resume", False, "Resume"),
        ("stop", True, "Hard-stop"),
        ("reset", True, "Hard-reset"),
    ]:
        _make_power_command(group, kind, label, action, destructive, description)

    @group.command("create")
    def _create(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        node: str = typer.Option(..., "--node", "-n", help="Node to create the guest on."),
        name: Optional[str] = typer.Option(None, "--name", help="Name (VM) / hostname (CT)."),
        option: Optional[List[str]] = typer.Option(
            None, "--option", "-o", help="Extra API parameter key=value (repeatable)."
        ),
    ):
        with error_boundary():
            _require_dangerous(ctx)
            client = _get_client(ctx)
            params = {}
            if name:
                params["name" if kind == "qemu" else "hostname"] = name
            for item in option or []:
                if "=" not in item:
                    raise ValueError(f"--option must be key=value (got {item!r}).")
                key, value = item.split("=", 1)
                params[key] = value
            result = client.create_guest(node, kind, vmid, **params)
            _ok(ctx, f"Creating {label.lower()} {vmid} on {node}", result)

    @group.command("clone")
    def _clone(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        newid: int = typer.Option(..., "--newid", help="VMID for the new clone."),
        name: Optional[str] = typer.Option(None, "--name", help="Name for the clone."),
        full: bool = typer.Option(False, "--full", help="Full clone (default: linked)."),
        target: Optional[str] = typer.Option(None, "--target", help="Target node for the clone."),
        node: Optional[str] = node_opt,
    ):
        with error_boundary():
            _require_dangerous(ctx)
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            params = {}
            if name:
                params["name"] = name
            if full:
                params["full"] = 1
            if target:
                params["target"] = target
            result = client.clone_guest(resolved, kind, vmid, newid, **params)
            _ok(ctx, f"Cloning {label.lower()} {vmid} → {newid}", result)

    @group.command("migrate")
    def _migrate(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        target: str = typer.Option(..., "--target", help="Destination node."),
        online: bool = typer.Option(False, "--online", help="Online/live migration."),
        node: Optional[str] = node_opt,
        yes: bool = yes_opt,
    ):
        with error_boundary():
            _require_dangerous(ctx)
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            confirm(f"migrate {label.lower()} {vmid} from {resolved} to {target}", assume_yes=yes)
            params = {}
            if online:
                params["online"] = 1
            result = client.migrate_guest(resolved, kind, vmid, target, **params)
            _ok(ctx, f"Migrating {label.lower()} {vmid} → {target}", result)

    @group.command("delete")
    def _delete(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        node: Optional[str] = node_opt,
        purge: bool = typer.Option(False, "--purge", help="Also remove from backup jobs / HA."),
        yes: bool = yes_opt,
    ):
        with error_boundary():
            _require_dangerous(ctx)
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            confirm(f"DELETE {label.lower()} {vmid} on {resolved} (irreversible)", assume_yes=yes)
            result = client.delete_guest(resolved, kind, vmid, purge=purge)
            _ok(ctx, f"Deleted {label.lower()} {vmid} on {resolved}", result)

    # snapshots (nested under the guest group)
    snap = typer.Typer(help=f"Manage {label} snapshots.", no_args_is_help=True)

    @snap.command("list")
    def _snap_list(ctx: typer.Context, vmid: int = vmid_arg, node: Optional[str] = node_opt):
        with error_boundary():
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            emit(
                client.list_snapshots(resolved, kind, vmid),
                columns=SNAPSHOT_COLUMNS,
                json_output=ctx.obj.json,
                title=f"{label} {vmid} snapshots",
            )

    @snap.command("create")
    def _snap_create(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        name: str = typer.Argument(..., help="Snapshot name."),
        description: Optional[str] = typer.Option(None, "--description", "-d"),
        vmstate: bool = typer.Option(False, "--vmstate", help="Include RAM state."),
        node: Optional[str] = node_opt,
    ):
        with error_boundary():
            _require_dangerous(ctx)
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            params = {}
            if description:
                params["description"] = description
            if vmstate:
                params["vmstate"] = 1
            result = client.create_snapshot(resolved, kind, vmid, name, **params)
            _ok(ctx, f"Creating snapshot {name!r} of {label.lower()} {vmid}", result)

    @snap.command("delete")
    def _snap_delete(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        name: str = typer.Argument(..., help="Snapshot name."),
        node: Optional[str] = node_opt,
        yes: bool = yes_opt,
    ):
        with error_boundary():
            _require_dangerous(ctx)
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            confirm(f"delete snapshot {name!r} of {label.lower()} {vmid}", assume_yes=yes)
            result = client.delete_snapshot(resolved, kind, vmid, name)
            _ok(ctx, f"Deleted snapshot {name!r} of {label.lower()} {vmid}", result)

    @snap.command("rollback")
    def _snap_rollback(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        name: str = typer.Argument(..., help="Snapshot name."),
        node: Optional[str] = node_opt,
        yes: bool = yes_opt,
    ):
        with error_boundary():
            _require_dangerous(ctx)
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            confirm(f"ROLLBACK {label.lower()} {vmid} to snapshot {name!r} (loses current state)", assume_yes=yes)
            result = client.rollback_snapshot(resolved, kind, vmid, name)
            _ok(ctx, f"Rolling back {label.lower()} {vmid} → {name!r}", result)

    group.add_typer(snap, name="snapshot")
    return group


vm_app = build_guest_app("qemu", "VM")
ct_app = build_guest_app("lxc", "CT")


# ---- storage ----
storage_app = typer.Typer(help="Inspect storage.", no_args_is_help=True)


@storage_app.command("list")
def storage_list(ctx: typer.Context, node: Optional[str] = node_opt):
    with error_boundary():
        client = _get_client(ctx)
        rows = client.cluster_resources(type="storage")
        if node:
            rows = [r for r in rows if r.get("node") == node]
        emit(rows, columns=STORAGE_COLUMNS, json_output=ctx.obj.json, title="Storage")


@storage_app.command("content")
def storage_content(
    ctx: typer.Context,
    storage: str = typer.Argument(..., help="Storage id."),
    node: str = typer.Option(..., "--node", "-n", help="Node name."),
):
    with error_boundary():
        client = _get_client(ctx)
        emit(
            client.storage_content(node, storage),
            columns=CONTENT_COLUMNS,
            json_output=ctx.obj.json,
            title=f"{storage} content",
        )


# ---- cluster ----
cluster_app = typer.Typer(help="Cluster-wide views.", no_args_is_help=True)


@cluster_app.command("status")
def cluster_status(ctx: typer.Context):
    with error_boundary():
        client = _get_client(ctx)
        data = client.cluster_status()
        if ctx.obj.json:
            emit(data, json_output=True)
            return
        nodes = [d for d in data if d.get("type") == "node"]
        emit(nodes, columns=CLUSTER_NODE_COLUMNS, json_output=False, title="Cluster nodes")


@cluster_app.command("resources")
def cluster_resources(
    ctx: typer.Context,
    type: Optional[str] = typer.Option(None, "--type", help="Filter: vm | node | storage | sdn | pool."),
):
    with error_boundary():
        client = _get_client(ctx)
        emit(
            client.cluster_resources(type=type),
            columns=RESOURCE_COLUMNS,
            json_output=ctx.obj.json,
            title="Cluster resources",
        )


# ---- tasks ----
task_app = typer.Typer(help="Inspect node tasks.", no_args_is_help=True)


@task_app.command("list")
def task_list(
    ctx: typer.Context,
    node: str = typer.Option(..., "--node", "-n", help="Node name."),
    limit: int = typer.Option(50, "--limit", help="Max tasks to show."),
):
    with error_boundary():
        client = _get_client(ctx)
        emit(client.list_tasks(node, limit=limit), columns=TASK_COLUMNS, json_output=ctx.obj.json, title=f"{node} tasks")


@task_app.command("status")
def task_status(
    ctx: typer.Context,
    upid: str = typer.Argument(..., help="Task UPID."),
    node: str = typer.Option(..., "--node", "-n", help="Node name."),
):
    with error_boundary():
        client = _get_client(ctx)
        emit(client.task_status(node, upid), json_output=ctx.obj.json, title="Task status")


@task_app.command("log")
def task_log(
    ctx: typer.Context,
    upid: str = typer.Argument(..., help="Task UPID."),
    node: str = typer.Option(..., "--node", "-n", help="Node name."),
):
    with error_boundary():
        client = _get_client(ctx)
        data = client.task_log(node, upid)
        if ctx.obj.json:
            emit(data, json_output=True)
            return
        for line in data:
            console.print(line.get("t", "") if isinstance(line, dict) else str(line))


app.add_typer(nodes_app, name="nodes")
app.add_typer(vm_app, name="vm")
app.add_typer(ct_app, name="ct")
app.add_typer(storage_app, name="storage")
app.add_typer(cluster_app, name="cluster")
app.add_typer(task_app, name="task")


def main():
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
