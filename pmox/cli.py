"""The pmox command-line interface.

Command groups: ``nodes``, ``vm``, ``ct``, ``storage``, ``cluster``, ``task``
(with a nested ``snapshot`` group under ``vm``/``ct``), plus a top-level
``version``.

Global options are position-independent — pmox lifts them before parsing, so
both of these work::

    pmox vm list --json
    pmox --json vm list
    pmox nodes list --host 10.0.0.2

Output format auto-detects: when stdout is piped or captured (e.g. an AI driving
the CLI) pmox emits JSON; at an interactive terminal it prints tables. Override
per-command with ``--json`` / ``--no-json``, or globally with ``PMOX_JSON``
(``1``/``0``/``auto``).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

import typer

from . import __version__, catalog, ipam, provision, views
from .client import ProxmoxClient
from .config import ConfigError, Settings, _parse_bool, load_settings
from .output import (
    Column,
    build_kv_table,
    console,
    emit,
    err_console,
    fmt_epoch,
    human_bytes,
    human_uptime,
    percent,
    status_fmt,
)
from .safety import (
    ConfirmationRequired,
    DangerousNotEnabled,
    confirm,
    require_dangerous,
    set_requires_confirmation,
)

# Global flags accepted in any position (hoisted to the front before Typer parses).
# These global flags are registered on main_callback below; listed here so the hoist shim handles them too.
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


# Indirection so tests can inject a fake client factory.
_client_factory = ProxmoxClient.from_settings


def _stream_isatty(stream) -> bool:
    """True if ``stream`` is an interactive terminal; False if unknown or it errors."""
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001 - a stream that can't answer is treated as non-TTY
        return False


def resolve_json_output(flag: Optional[bool], env_value: Optional[str], stdout_isatty: bool) -> bool:
    """Decide whether to emit JSON, in precedence order (highest first):

    1. an explicit ``--json`` / ``--no-json`` flag,
    2. the ``PMOX_JSON`` env var (``1``/``0``/``true``/``false``, or ``auto``),
    3. auto-detect: when stdout is **not** a TTY (piped or captured, e.g. by an AI
       driving the CLI) default to JSON; at an interactive terminal, tables.
    """
    if flag is not None:
        return flag
    if env_value is not None and env_value.strip() != "":
        token = env_value.strip().lower()
        if token == "auto":
            return not stdout_isatty
        return _parse_bool(token)
    return not stdout_isatty


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


def _get_client(ctx: typer.Context) -> ProxmoxClient:
    state: State = ctx.obj
    if state.client is None:
        state.settings.validate()
        state.client = _client_factory(state.settings)
    return state.client


def _resolve_node_or_die(client: ProxmoxClient, vmid: int) -> str:
    node = client.resolve_node(vmid)
    if not node:
        err_console.print(
            f"[red]Error:[/red] Could not locate guest {vmid} in the cluster. "
            "Pass --node to specify it explicitly."
        )
        raise typer.Exit(1)
    return node


def _single_node_or_die(client: ProxmoxClient) -> str:
    """Return the only node's name, or error if the cluster has 0 or >1 nodes."""
    nodes = client.list_nodes()
    if len(nodes) == 1:
        return nodes[0]["node"]
    raise ValueError("Cluster has multiple nodes; pass --node to choose where to create.")


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
            exitstatus = status.get("exitstatus")
            if exitstatus is not None and exitstatus != "OK":
                raise RuntimeError(f"Task {result} failed: {exitstatus}")
            return status
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Task {result} did not finish within {ctx.obj.timeout}s.")
        time.sleep(_POLL_SECONDS)


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


def parse_options(items: Optional[List[str]]) -> dict:
    """Parse repeatable ``-o key=value`` options into a dict."""
    params: dict = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--option must be key=value (got {item!r}).")
        key, value = item.split("=", 1)
        params[key] = value
    return params


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


def _ok(ctx: typer.Context, message: str, result=None) -> None:
    state: State = ctx.obj
    if state.json:
        print(json.dumps({"ok": True, "message": message, "result": result}, default=str, indent=2))
    else:
        console.print(f"[green]✓[/green] {message}")
        if result:
            console.print(f"  [dim]{result}[/dim]")


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

HEALTH_NODE_COLUMNS = [
    Column("Node", "node"),
    Column("Status", "status", status_fmt),
    Column("CPU", "cpu_pct", percent),
    Column("Mem", "mem_pct", percent),
    Column("Flags", row_formatter=lambda r: ", ".join(r.get("flags") or []) or "-"),
]

HEALTH_STORAGE_COLUMNS = [
    Column("Storage", "storage"),
    Column("Node", "node"),
    Column("Use%", "used_pct", percent),
    Column("Flags", row_formatter=lambda r: ", ".join(r.get("flags") or []) or "-"),
]

IP_COLUMNS = [
    Column("Interface", "name"),
    Column("IPv4", "ipv4"),
    Column("IPv6", "ipv6"),
]

IP_ALL_COLUMNS = [
    Column("Interface", "name"),
    Column("MAC", "mac"),
    Column("IPv4", "ipv4"),
    Column("IPv6", "ipv6"),
]


def _ip_rows_filtered(interfaces) -> List[dict]:
    """Non-loopback interfaces with their global IPv4/IPv6 (default view)."""
    rows = []
    for iface in interfaces:
        v4 = [a["address"] for a in iface["addresses"] if a["family"] == "ipv4" and a["scope"] == "global"]
        v6 = [a["address"] for a in iface["addresses"] if a["family"] == "ipv6" and a["scope"] == "global"]
        if not v4 and not v6:
            continue
        rows.append({"name": iface["name"], "ipv4": ", ".join(v4) or "-", "ipv6": ", ".join(v6) or "-"})
    return rows


def _ip_rows_all(interfaces) -> List[dict]:
    """Every interface and address with prefixes + MAC (--all view)."""
    rows = []
    for iface in interfaces:
        v4 = [f'{a["address"]}/{a["prefix"]}' for a in iface["addresses"] if a["family"] == "ipv4"]
        v6 = [f'{a["address"]}/{a["prefix"]}' for a in iface["addresses"] if a["family"] == "ipv6"]
        rows.append({
            "name": iface["name"], "mac": iface.get("mac") or "-",
            "ipv4": ", ".join(v4) or "-", "ipv6": ", ".join(v6) or "-",
        })
    return rows


def _print_network_section(network) -> None:
    """Render the network block inside `describe` (human mode)."""
    if not network.get("available"):
        console.print(f"[dim]network: unavailable ({network.get('reason', 'unknown')})[/dim]")
        return
    console.print(f"network · primary {network.get('primary') or '-'}")
    emit(_ip_rows_filtered(network.get("interfaces", [])), columns=IP_COLUMNS, json_output=False)


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
    json_output: Optional[bool] = typer.Option(
        None,
        "--json/--no-json",
        help="Force JSON or human tables. Default: auto — JSON when output is piped/captured "
        "(e.g. an AI driving the CLI), tables at an interactive terminal.",
    ),
    dangerous: bool = typer.Option(
        False, "--dangerous", help="Enable dangerous (write/management) mode. Default is read-only (safe for AI exploration)."
    ),
    wait: bool = typer.Option(
        False, "--wait/--no-wait", help="Wait for the resulting task to finish and report its outcome."
    ),
    timeout: int = typer.Option(600, "--timeout", help="Seconds to wait when --wait is set (default 600)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the intended API call as JSON and exit without changing anything."
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

    json_on = resolve_json_output(json_output, os.environ.get("PMOX_JSON"), _stream_isatty(sys.stdout))
    dangerous_on = dangerous or _parse_bool(os.environ.get("PMOX_DANGEROUS"))
    ctx.obj = State(
        settings=settings,
        json_output=json_on,
        dangerous=dangerous_on,
        wait=wait,
        timeout=timeout,
        dry_run=dry_run,
    )


@app.command("version")
def server_version(ctx: typer.Context):
    """Show the Proxmox VE version of the connected node."""
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        emit(client.version(), json_output=ctx.obj.json, title="Proxmox version")


@app.command("health")
def health(ctx: typer.Context):
    """One-shot cluster health triage (read-only)."""
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        data = views.summarize_health(client)
        if ctx.obj.json:
            emit(data, json_output=True)
            return
        quorum = "[green]quorate[/green]" if data["quorate"] else "[red]NO QUORUM[/red]"
        console.print(f"Cluster: {quorum} · nodes {data['nodes_online']}/{data['nodes_total']} online · "
                      f"guests {data['guests']['running']} running / {data['guests']['stopped']} stopped")
        emit(data["nodes"], columns=HEALTH_NODE_COLUMNS, json_output=False, title="Nodes")
        emit(data["storage"], columns=HEALTH_STORAGE_COLUMNS, json_output=False, title="Storage")
        for w in data["warnings"]:
            console.print(f"[yellow]![/yellow] {w}")


# ---- nodes ----
nodes_app = typer.Typer(help="Inspect cluster nodes.", no_args_is_help=True)


@nodes_app.command("list")
def nodes_list(ctx: typer.Context):
    """List all nodes and their resource usage."""
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        emit(client.list_nodes(), columns=NODE_COLUMNS, json_output=ctx.obj.json, title="Nodes")


@nodes_app.command("status")
def nodes_status(ctx: typer.Context, node: str = typer.Argument(..., help="Node name.")):
    """Show detailed status for a node."""
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        emit(client.node_status(node), json_output=ctx.obj.json, title=f"Node {node}")


# ---- guest (vm / ct) app factory ----
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


def build_guest_app(kind: str, label: str) -> typer.Typer:
    descr = "QEMU VMs" if kind == "qemu" else "LXC containers"
    group = typer.Typer(help=f"Manage {label}s ({descr}).", no_args_is_help=True)

    @group.command("list")
    def _list(ctx: typer.Context, node: Optional[str] = node_opt):
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            emit(
                client.list_guests(kind, node=node),
                columns=GUEST_COLUMNS,
                json_output=ctx.obj.json,
                title=f"{label}s",
            )

    @group.command("status")
    def _status(ctx: typer.Context, vmid: int = vmid_arg, node: Optional[str] = node_opt):
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            emit(client.guest_status(resolved, kind, vmid), json_output=ctx.obj.json, title=f"{label} {vmid} status")

    @group.command("config")
    def _config(ctx: typer.Context, vmid: int = vmid_arg, node: Optional[str] = node_opt):
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            emit(client.guest_config(resolved, kind, vmid), json_output=ctx.obj.json, title=f"{label} {vmid} config")

    @group.command("describe", help=f"Consolidated view of a {label}: status, config, snapshots, recent tasks.")
    def _describe(ctx: typer.Context, vmid: int = vmid_arg, node: Optional[str] = node_opt):
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            data = views.describe_guest(client, kind, vmid, node=node)
            if ctx.obj.json:
                emit(data, json_output=True)
            else:
                console.print(build_kv_table(data["status"], title=f"{label} {vmid} status"))
                console.print(build_kv_table(data["config"], title="config"))
                _print_network_section(data["network"])
                emit(data["snapshots"], columns=SNAPSHOT_COLUMNS, json_output=False, title="snapshots")
                emit(data["recent_tasks"], columns=TASK_COLUMNS, json_output=False, title="recent tasks")

    @group.command("ip", help=f"Show the live IP address(es) of a {label} (VM: via guest agent; CT: via interfaces).")
    def _ip(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        node: Optional[str] = node_opt,
        all_: bool = typer.Option(False, "--all", "-a", help="Include loopback, IPv6 link-local, and MAC addresses."),
    ):
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            data = views.guest_ip_addresses(client, kind, vmid, node=node)
            if ctx.obj.json:
                emit(data, json_output=True)
                return
            name = f" ({data['name']})" if data.get("name") else ""
            console.print(f"{label} {vmid}{name} on {data['node']} · primary {data['primary'] or '-'}")
            rows = _ip_rows_all(data["interfaces"]) if all_ else _ip_rows_filtered(data["interfaces"])
            emit(rows, columns=(IP_ALL_COLUMNS if all_ else IP_COLUMNS), json_output=False)

    @group.command("set", help=f"Update configuration of a {label} (cores, memory, disks, nics, tags, …).")
    def _set(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        option: Optional[List[str]] = typer.Option(
            None, "--option", "-o", help="Config key=value to set (repeatable). Use delete=dev to remove (needs --yes)."
        ),
        node: Optional[str] = node_opt,
        yes: bool = yes_opt,
    ):
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

    @group.command("resize", help=f"Grow a disk of a {label} (grow-only).")
    def _resize(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        disk: str = typer.Option(..., "--disk", help="Disk to grow, e.g. scsi0."),
        size: str = typer.Option(..., "--size", help="+10G (grow by) or 50G (grow to)."),
        node: Optional[str] = node_opt,
    ):
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            _execute(
                ctx,
                op=f"{kind}.resize",
                message=f"Resize {label.lower()} {vmid} disk {disk} to {size}",
                node=resolved,
                call=lambda: client.resize_disk(resolved, kind, vmid, disk, size),
                params={"vmid": vmid, "disk": disk, "size": size},
            )

    @group.command("rename", help=f"Rename a {label}.")
    def _rename(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        newname: str = typer.Argument(..., help="New name (VM) / hostname (CT)."),
        node: Optional[str] = node_opt,
    ):
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
                params={"vmid": vmid, key: newname},
            )

    @group.command("tag", help=f"Add/remove/set tags on a {label}.")
    def _tag(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        add: Optional[str] = typer.Option(None, "--add", help="Comma-separated tags to add."),
        remove: Optional[str] = typer.Option(None, "--remove", help="Comma-separated tags to remove."),
        set_: Optional[str] = typer.Option(None, "--set", help="Comma-separated tags to set (replaces all)."),
        node: Optional[str] = node_opt,
    ):
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
                params={"vmid": vmid, "tags": new_tags},
            )

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
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            params = {}
            if name:
                params["name" if kind == "qemu" else "hostname"] = name
            params.update(parse_options(option))
            _execute(
                ctx,
                op=f"{kind}.create",
                message=f"Creating {label.lower()} {vmid} on {node}",
                node=node,
                call=lambda: client.create_guest(node, kind, vmid, **params),
                params={"vmid": vmid, **params},
            )

    if kind == "qemu":

        @group.command("new", help="Create a VM: blank shell, cloud-init server with --image, or clone from --from-template.")
        def _new(
            ctx: typer.Context,
            name: Optional[str] = typer.Argument(None, help="VM name (optional)."),
            size: str = typer.Option("small", "--size", help="Sizing profile: small | medium | large."),
            disk: Optional[int] = typer.Option(None, "--disk", help="Disk size in GiB."),
            storage: str = typer.Option("local-lvm", "--storage", help="Storage for the disk/cloud-init."),
            import_storage: Optional[str] = typer.Option(None, "--import-storage", help="Storage to hold the imported image (default: auto-detect one with 'import' content)."),
            node: Optional[str] = typer.Option(None, "--node", "-n", help="Node (auto-picked if one node)."),
            vmid: Optional[int] = typer.Option(None, "--vmid", help="VMID (auto-assigned if omitted)."),
            option: Optional[List[str]] = typer.Option(None, "--option", "-o", help="Extra create param key=value."),
            image: Optional[str] = typer.Option(None, "--image", help="Cloud image (catalog name, https URL, or volid). Enables cloud-init mode."),
            from_template: Optional[int] = typer.Option(None, "--from-template", help="Clone an existing template VMID into a cloud-init VM. Cloud-init mode."),
            ssh_key: Optional[List[str]] = typer.Option(None, "--ssh-key", help="Path to an SSH public key file (repeatable). Cloud-init mode."),
            ip: Optional[str] = typer.Option(None, "--ip", help="dhcp or <cidr>,gw=<ip>. Cloud-init mode."),
            ciuser: Optional[str] = typer.Option(None, "--ciuser", help="Cloud-init user. Cloud-init mode."),
            cipassword: Optional[str] = typer.Option(None, "--cipassword", help="Cloud-init password. Cloud-init mode."),
            nameserver: Optional[str] = typer.Option(None, "--nameserver", help="Cloud-init DNS server(s). Cloud-init mode."),
        ):
            with error_boundary(ctx.obj.json):
                client = _get_client(ctx)
                if image and from_template is not None:
                    raise ValueError("--image and --from-template are mutually exclusive.")
                target_vmid = vmid if vmid is not None else int(client.cluster_nextid())

                if from_template is not None:
                    target_node = node or client.resolve_node(from_template)
                    if not target_node:
                        raise LookupError(f"Could not locate template {from_template} in the cluster.")
                    sshkeys = "\n".join(Path(p).read_text().strip() for p in (ssh_key or [])) or None
                    plan = provision.build_vm_clone_plan(
                        client,
                        node=target_node,
                        template_id=from_template,
                        newid=target_vmid,
                        name=name,
                        disk=disk,
                        sshkeys=sshkeys,
                        ipconfig=provision.build_ipconfig(ip) if ip else None,
                        ciuser=ciuser,
                        cipassword=cipassword,
                        nameserver=nameserver,
                        start=True,
                    )
                    if ctx.obj.dry_run:
                        print(json.dumps({"dry_run": True, "op": "qemu.new.from_template", "node": target_node, "plan": plan}, default=str, indent=2))
                        return
                    require_dangerous(ctx.obj.dangerous)
                    provision.execute_plan(client, target_node, plan, waiter=lambda n, upid: _maybe_wait(ctx, n, upid))
                    _ok(ctx, f"Cloned template {from_template} -> VM {target_vmid} on {target_node}")
                    return

                target_node = node or _single_node_or_die(client)
                profile = catalog.size_params(size)

                if image:
                    sshkeys = "\n".join(Path(p).read_text().strip() for p in (ssh_key or [])) or None
                    needs_import = catalog.resolve_image(image)["kind"] != "volid"
                    resolved_import = (
                        provision.resolve_import_storage(client, target_node, import_storage)
                        if needs_import else None
                    )
                    plan = provision.build_vm_image_plan(
                        client,
                        node=target_node,
                        vmid=target_vmid,
                        name=name,
                        cores=profile["cores"],
                        memory=profile["memory"],
                        disk=disk,
                        storage=storage,
                        import_storage=resolved_import,
                        image=image,
                        sshkeys=sshkeys,
                        ipconfig=provision.build_ipconfig(ip) if ip else None,
                        ciuser=ciuser,
                        cipassword=cipassword,
                        nameserver=nameserver,
                        start=True,
                        extra=parse_options(option),
                    )
                    if ctx.obj.dry_run:
                        print(json.dumps({"dry_run": True, "op": "qemu.new.image", "node": target_node, "plan": plan}, default=str, indent=2))
                        return
                    require_dangerous(ctx.obj.dangerous)
                    provision.execute_plan(client, target_node, plan, waiter=lambda n, upid: _maybe_wait(ctx, n, upid))
                    _ok(ctx, f"Created cloud-init VM {target_vmid} on {target_node} from {image}")
                    return

                # blank shell (B2 behavior)
                params = dict(profile)
                params.update({"scsihw": "virtio-scsi-single", "net0": "virtio,bridge=vmbr0", "ostype": "l26"})
                if name:
                    params["name"] = name
                if disk:
                    params["scsi0"] = f"{storage}:{disk},iothread=1"
                    params["boot"] = "order=scsi0"
                params.update(parse_options(option))
                _execute(
                    ctx,
                    op="qemu.new",
                    message=f"Create VM {target_vmid} on {target_node}",
                    node=target_node,
                    call=lambda: client.create_guest(target_node, "qemu", target_vmid, **params),
                    params={"vmid": target_vmid, **params},
                )

        @group.command("up", help="Create a ready-to-SSH VM with an auto-allocated static IP (seamless, token-only).")
        def _up(
            ctx: typer.Context,
            name: str = typer.Argument(..., help="VM name."),
            image: str = typer.Option(..., "--image", help="Cloud image: catalog name, https URL, or import volid."),
            size: str = typer.Option("small", "--size", help="Sizing profile: small | medium | large."),
            disk: Optional[int] = typer.Option(None, "--disk", help="Disk size in GiB."),
            node: Optional[str] = typer.Option(None, "--node", "-n", help="Node (auto-picked if one node)."),
            storage: str = typer.Option("local-lvm", "--storage", help="Storage for the disk/cloud-init."),
            import_storage: Optional[str] = typer.Option(None, "--import-storage", help="Storage to hold the imported image (default: auto-detect)."),
            ip: Optional[str] = typer.Option(None, "--ip", help="Static <cidr>,gw=<ip> to use instead of auto-allocating."),
            ssh_key: Optional[str] = typer.Option(None, "--ssh-key", help="SSH public key path (default ~/.ssh/id_ed25519.pub; generated if missing)."),
            no_ssh_key: bool = typer.Option(False, "--no-ssh-key", help="Don't attach or generate an SSH key."),
            ciuser: Optional[str] = typer.Option(None, "--ciuser", help="Cloud-init user (default from config)."),
            vmid: Optional[int] = typer.Option(None, "--vmid", help="VMID (auto-assigned if omitted)."),
        ):
            with error_boundary(ctx.obj.json):
                client = _get_client(ctx)
                settings = ctx.obj.settings
                target_node = node or _single_node_or_die(client)
                target_vmid = vmid if vmid is not None else int(client.cluster_nextid())
                profile = catalog.size_params(size)

                if ip:
                    ipconfig = provision.build_ipconfig(ip)
                    chosen_ip = ip.split(",", 1)[0].split("/", 1)[0]
                else:
                    if not (settings.net_cidr and settings.net_gateway and settings.net_pool):
                        raise ValueError(
                            "vm up needs a static-IP pool. Set [network] cidr/gateway/pool in your "
                            "pmox config (or PROXMOX_NET_CIDR/GATEWAY/POOL), or pass --ip explicitly."
                        )
                    allocated = ipam.allocate_ip(
                        client, cidr=settings.net_cidr, gateway=settings.net_gateway, pool=settings.net_pool
                    )
                    ipconfig = f"ip={allocated},gw={settings.net_gateway}"
                    chosen_ip = allocated.split("/", 1)[0]

                needs_import = catalog.resolve_image(image)["kind"] != "volid"
                resolved_import = (
                    provision.resolve_import_storage(client, target_node, import_storage or settings.default_import_storage)
                    if needs_import else None
                )

                sshkeys = None
                if not no_ssh_key:
                    key_path = ssh_key or settings.default_ssh_key or str(Path.home() / ".ssh" / "id_ed25519.pub")
                    sshkeys = provision.ensure_ssh_key(key_path)

                chosen_ciuser = ciuser or settings.default_ciuser
                plan = provision.build_vm_image_plan(
                    client, node=target_node, vmid=target_vmid, name=name,
                    cores=profile["cores"], memory=profile["memory"], disk=disk,
                    storage=storage, import_storage=resolved_import, image=image,
                    sshkeys=sshkeys, ipconfig=ipconfig, ciuser=chosen_ciuser,
                    cipassword=None, nameserver=settings.net_nameserver, start=True,
                )
                if ctx.obj.dry_run:
                    print(json.dumps({"dry_run": True, "op": "qemu.up", "node": target_node, "plan": plan}, default=str, indent=2))
                    return
                require_dangerous(ctx.obj.dangerous)
                provision.execute_plan(client, target_node, plan, waiter=lambda n, upid: _maybe_wait(ctx, n, upid))

                if ctx.obj.json:
                    ssh_val = f"ssh {chosen_ciuser}@{chosen_ip}" if chosen_ciuser else None
                    emit({"vmid": target_vmid, "name": name, "node": target_node, "ip": chosen_ip, "ssh": ssh_val}, json_output=True)
                else:
                    console.print(f"VM {target_vmid}  {name}  ip {chosen_ip}")
                    console.print(f"ssh {chosen_ciuser}@{chosen_ip}" if chosen_ciuser else f"ssh <image's default user>@{chosen_ip}")

    if kind == "lxc":

        @group.command("new", help="Create an LXC container from a template, ready to SSH.")
        def _ct_new(
            ctx: typer.Context,
            name: Optional[str] = typer.Argument(None, help="Hostname (optional)."),
            template: str = typer.Option(..., "--template", help="Template: catalog/aplinfo name or a vztmpl volid."),
            size: str = typer.Option("small", "--size", help="Sizing profile: small | medium | large."),
            disk: int = typer.Option(8, "--disk", help="Root filesystem size in GiB."),
            storage: str = typer.Option("local-lvm", "--storage", help="Storage for the rootfs."),
            template_storage: str = typer.Option("local", "--template-storage", help="Storage to download the template into (vztmpl)."),
            node: Optional[str] = typer.Option(None, "--node", "-n", help="Node (auto-picked if one node)."),
            vmid: Optional[int] = typer.Option(None, "--vmid", help="VMID (auto-assigned if omitted)."),
            ssh_key: Optional[List[str]] = typer.Option(None, "--ssh-key", help="Path to an SSH public key file (repeatable)."),
            ip: str = typer.Option("dhcp", "--ip", help="dhcp or <cidr>,gw=<ip>."),
            password: Optional[str] = typer.Option(None, "--password", help="Root password."),
        ):
            with error_boundary(ctx.obj.json):
                client = _get_client(ctx)
                target_node = node or _single_node_or_die(client)
                target_vmid = vmid if vmid is not None else int(client.cluster_nextid())
                profile = catalog.size_params(size)
                sshkeys = "\n".join(Path(p).read_text().strip() for p in (ssh_key or [])) or None
                plan = provision.build_ct_plan(
                    client,
                    node=target_node,
                    vmid=target_vmid,
                    hostname=name,
                    template=template,
                    storage=storage,
                    template_storage=template_storage,
                    disk=disk,
                    cores=profile["cores"],
                    memory=profile["memory"],
                    sshkeys=sshkeys,
                    ip=ip,
                    password=password,
                    start=True,
                )
                if ctx.obj.dry_run:
                    print(json.dumps({"dry_run": True, "op": "lxc.new", "node": target_node, "plan": plan}, default=str, indent=2))
                    return
                require_dangerous(ctx.obj.dangerous)
                provision.execute_plan(client, target_node, plan, waiter=lambda n, upid: _maybe_wait(ctx, n, upid))
                _ok(ctx, f"Created container {target_vmid} on {target_node} from {template}")

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
                params={"vmid": vmid, "newid": newid, **params},
            )

    @group.command("migrate")
    def _migrate(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        target: str = typer.Option(..., "--target", help="Destination node."),
        online: bool = typer.Option(False, "--online", help="Online/live migration."),
        node: Optional[str] = node_opt,
        yes: bool = yes_opt,
    ):
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
                params={"vmid": vmid, "target": target, **params},
                destructive=True,
                yes=yes,
                confirm_msg=f"migrate {label.lower()} {vmid} from {resolved} to {target}",
            )

    @group.command("delete")
    def _delete(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        node: Optional[str] = node_opt,
        purge: bool = typer.Option(False, "--purge", help="Also remove from backup jobs / HA."),
        yes: bool = yes_opt,
    ):
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

    # snapshots (nested under the guest group)
    snap = typer.Typer(help=f"Manage {label} snapshots.", no_args_is_help=True)

    @snap.command("list")
    def _snap_list(ctx: typer.Context, vmid: int = vmid_arg, node: Optional[str] = node_opt):
        with error_boundary(ctx.obj.json):
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
                params={"vmid": vmid, "snapname": name, **params},
            )

    @snap.command("delete")
    def _snap_delete(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        name: str = typer.Argument(..., help="Snapshot name."),
        node: Optional[str] = node_opt,
        yes: bool = yes_opt,
    ):
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            _execute(
                ctx,
                op=f"{kind}.snapshot.delete",
                message=f"Deleted snapshot {name!r} of {label.lower()} {vmid}",
                node=resolved,
                call=lambda: client.delete_snapshot(resolved, kind, vmid, name),
                params={"vmid": vmid, "snapname": name},
                destructive=True,
                yes=yes,
                confirm_msg=f"delete snapshot {name!r} of {label.lower()} {vmid}",
            )

    @snap.command("rollback")
    def _snap_rollback(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        name: str = typer.Argument(..., help="Snapshot name."),
        node: Optional[str] = node_opt,
        yes: bool = yes_opt,
    ):
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, vmid)
            _execute(
                ctx,
                op=f"{kind}.snapshot.rollback",
                message=f"Rolling back {label.lower()} {vmid} → {name!r}",
                node=resolved,
                call=lambda: client.rollback_snapshot(resolved, kind, vmid, name),
                params={"vmid": vmid, "snapname": name},
                destructive=True,
                yes=yes,
                confirm_msg=f"ROLLBACK {label.lower()} {vmid} to snapshot {name!r} (loses current state)",
            )

    group.add_typer(snap, name="snapshot")
    return group


vm_app = build_guest_app("qemu", "VM")
ct_app = build_guest_app("lxc", "CT")


# ---- storage ----
storage_app = typer.Typer(help="Inspect storage.", no_args_is_help=True)


@storage_app.command("list")
def storage_list(ctx: typer.Context, node: Optional[str] = node_opt):
    with error_boundary(ctx.obj.json):
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
    with error_boundary(ctx.obj.json):
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
    with error_boundary(ctx.obj.json):
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
    with error_boundary(ctx.obj.json):
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
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        emit(client.list_tasks(node, limit=limit), columns=TASK_COLUMNS, json_output=ctx.obj.json, title=f"{node} tasks")


@task_app.command("status")
def task_status(
    ctx: typer.Context,
    upid: str = typer.Argument(..., help="Task UPID."),
    node: str = typer.Option(..., "--node", "-n", help="Node name."),
):
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        emit(client.task_status(node, upid), json_output=ctx.obj.json, title="Task status")


@task_app.command("log")
def task_log(
    ctx: typer.Context,
    upid: str = typer.Argument(..., help="Task UPID."),
    node: str = typer.Option(..., "--node", "-n", help="Node name."),
):
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        data = client.task_log(node, upid)
        if ctx.obj.json:
            emit(data, json_output=True)
            return
        for line in data:
            console.print(line.get("t", "") if isinstance(line, dict) else str(line))


# ---- image catalog / pull ----
image_app = typer.Typer(help="VM cloud images: list the catalog and pull them to storage.", no_args_is_help=True)


@image_app.command("list")
def image_list(
    ctx: typer.Context,
    ct: bool = typer.Option(False, "--ct", help="List LXC container templates (live, from the node) instead of the VM catalog."),
    node: Optional[str] = typer.Option(None, "--node", "-n", help="Node (required with --ct)."),
):
    """List VM cloud images (catalog) or, with --ct, container templates from a node."""
    with error_boundary(ctx.obj.json):
        if ct:
            if not node:
                raise ValueError("--ct requires --node.")
            client = _get_client(ctx)
            emit(client.list_appliances(node), json_output=ctx.obj.json, title="Container templates")
            return
        rows = [{"name": name, "url": e["url"], "filename": e["filename"]} for name, e in catalog.IMAGE_CATALOG.items()]
        emit(rows, json_output=ctx.obj.json, title="Image catalog")


@image_app.command("pull")
def image_pull(
    ctx: typer.Context,
    image: str = typer.Argument(..., help="Catalog name or https URL."),
    storage: str = typer.Option(..., "--storage", help="Target storage (needs the 'import' content type)."),
    node: str = typer.Option(..., "--node", "-n", help="Node to download on."),
    as_template: bool = typer.Option(False, "--as-template", help="Build a reusable golden VM template instead of just downloading."),
    vmid: Optional[int] = typer.Option(None, "--vmid", help="VMID for the template (auto-assigned if omitted). Used with --as-template."),
    name: Optional[str] = typer.Option(None, "--name", help="Name for the template. Used with --as-template."),
    ct: bool = typer.Option(False, "--ct", help="Pull an LXC container template via aplinfo instead of a VM cloud image."),
):
    """Download a VM cloud image (or, with --ct, an LXC container template) to a storage; with --as-template, build a reusable golden VM template. Needs --dangerous."""
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)

        if ct and as_template:
            raise ValueError("--ct and --as-template are mutually exclusive.")

        if ct:
            matches = [a for a in client.list_appliances(node) if image in a.get("template", "")]
            if not matches:
                raise ValueError(f"No container template matching {image!r} on {node}.")
            filename = matches[0]["template"]
            volid = f"{storage}:vztmpl/{filename}"
            if ctx.obj.dry_run:
                print(json.dumps({"dry_run": True, "op": "image.pull.ct", "node": node, "params": {"volid": volid, "template": filename}}, default=str, indent=2))
                return
            require_dangerous(ctx.obj.dangerous)
            if any(c.get("volid") == volid for c in client.storage_content(node, storage)):
                _ok(ctx, f"Template already present: {volid}")
                return
            upid = client.download_appliance(node, storage, filename)
            _maybe_wait(ctx, node, upid)
            _ok(ctx, f"Pulled container template {image} -> {volid}", upid)
            return

        spec = catalog.resolve_image(image)
        if spec["kind"] == "volid":
            raise ValueError("image pull expects a catalog name or URL, not an existing volid.")

        if as_template:
            target_vmid = vmid if vmid is not None else int(client.cluster_nextid())
            plan = provision.build_template_plan(client, node=node, vmid=target_vmid, name=name, storage=storage, image=image)
            if ctx.obj.dry_run:
                print(json.dumps({"dry_run": True, "op": "image.pull.template", "node": node, "plan": plan}, default=str, indent=2))
                return
            require_dangerous(ctx.obj.dangerous)
            provision.execute_plan(client, node, plan, waiter=lambda n, upid: _maybe_wait(ctx, n, upid))
            _ok(ctx, f"Built template {target_vmid} on {node} from {image}")
            return

        volid = f"{storage}:import/{spec['filename']}"
        if ctx.obj.dry_run:
            print(json.dumps({"dry_run": True, "op": "image.pull", "node": node, "params": {"volid": volid, "url": spec["url"]}}, default=str, indent=2))
            return
        require_dangerous(ctx.obj.dangerous)
        if any(c.get("volid") == volid for c in client.storage_content(node, storage)):
            _ok(ctx, f"Image already present: {volid}")
            return
        upid = client.download_url(
            node, storage, url=spec["url"], content="import", filename=spec["filename"],
            checksum=spec["checksum"], checksum_algorithm=spec["algo"],
        )
        _maybe_wait(ctx, node, upid)
        _ok(ctx, f"Pulled {image} → {volid}", upid)


app.add_typer(nodes_app, name="nodes")
app.add_typer(vm_app, name="vm")
app.add_typer(ct_app, name="ct")
app.add_typer(storage_app, name="storage")
app.add_typer(cluster_app, name="cluster")
app.add_typer(task_app, name="task")
app.add_typer(image_app, name="image")


def main():
    app(args=hoist_global_flags(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
