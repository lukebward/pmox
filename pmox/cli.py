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

import requests
import typer

try:  # typer >= 0.26 vendors click as typer._click; older typer uses the real package
    import click
except ModuleNotFoundError:
    from typer import _click as click

from . import __version__, arp, catalog, guestops, guide, ipam, provision, views
from .client import ProxmoxClient
from .config import ConfigError, Settings, _parse_bool, load_settings
from .errors import PlanError, PmoxError, TaskFailed, TaskTimeout
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
        "--no-dangerous",
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

    Known limitation: a command-level option *value* that happens to spell a
    global flag (e.g. ``vm tag 100 --add --json``) is hoisted too; quote-proof
    such values by placing them after ``--``.
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


def _resolve_node_or_die(client: ProxmoxClient, kind: str, vmid: int) -> str:
    """Owning node for ``vmid``, validated against ``kind``. Raises ``LookupError``
    (which the error boundary turns into a proper envelope) when the guest is
    missing or is the other kind."""
    row = views.locate_guest_checked(client, kind, vmid)
    if row is None or not row.get("node"):
        raise views.guest_not_found(vmid)
    return row["node"]


def _single_node_or_die(client: ProxmoxClient) -> str:
    """Return the only node's name, or error listing the choices when there are several."""
    nodes = sorted(n["node"] for n in client.list_nodes())
    if len(nodes) == 1:
        return nodes[0]
    if not nodes:
        raise RuntimeError("The cluster reports no nodes; check connectivity and token permissions.")
    raise ValueError(f"This cluster has multiple nodes ({', '.join(nodes)}); pass --node to choose one.")


def _node_from_upid(upid: str) -> Optional[str]:
    """Node name embedded in a UPID (``UPID:<node>:...``), or None."""
    parts = upid.split(":")
    if len(parts) > 2 and parts[0] == "UPID" and parts[1]:
        return parts[1]
    return None


def _node_for_task(node: Optional[str], upid: str) -> str:
    resolved = node or _node_from_upid(upid)
    if not resolved:
        raise ValueError(f"Cannot determine the node from {upid!r}; pass --node explicitly.")
    return resolved


def _select_fields(rows: List[dict], fields: str) -> List[dict]:
    """Project list rows onto a comma-separated subset of keys (missing keys → null)."""
    keys = [k.strip() for k in fields.split(",") if k.strip()]
    if not keys:
        raise ValueError("--fields needs at least one field name.")
    return [{k: r.get(k) for k in keys} for r in rows]


_POLL_SECONDS = 2


def _maybe_wait(ctx: typer.Context, node: str, result):
    """If ``result`` is a task UPID, poll until the task finishes.

    Returns the final task-status dict, or the original ``result`` if it is not a
    UPID. Raises :class:`TaskFailed` / :class:`TaskTimeout` (both carry the upid
    and node in ``extra`` so the error envelope stays machine-actionable).
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
                raise TaskFailed(
                    f"Task {result} failed: {exitstatus}",
                    extra={
                        "upid": result,
                        "node": node,
                        "hint": f"`pmox task log {result} --node {node}` shows the failure details.",
                    },
                )
            return status
        if time.monotonic() >= deadline:
            raise TaskTimeout(
                f"Task {result} did not finish within {ctx.obj.timeout}s; "
                "it may still be running on the server.",
                extra={
                    "upid": result,
                    "node": node,
                    "hint": f"Resume waiting with `pmox task wait {result}`, or inspect with "
                            f"`pmox task status {result} --node {node}`.",
                },
            )
        time.sleep(_POLL_SECONDS)


def _execute(
    ctx: typer.Context,
    *,
    op: str,
    message: str,
    node,
    call,
    params=None,
    vmid=None,
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
        _emit_dry_run(op, node, params, vmid=vmid)
        return None
    require_dangerous(state.dangerous)
    if destructive:
        confirm(confirm_msg or message, assume_yes=yes)
    result = call()
    if state.wait:
        result = _maybe_wait(ctx, node, result)
    fields: dict = {"op": op, "node": node}
    if vmid is not None:
        fields["vmid"] = vmid
    if isinstance(result, str) and result.startswith("UPID:"):
        fields["upid"] = result
    elif isinstance(result, dict):
        fields["task"] = result
    elif result is not None:
        fields["result"] = result
    _ok(ctx, message, **fields)
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


def _ok(ctx: typer.Context, message: str, **fields) -> None:
    """Emit a success envelope: ``{"ok": true, "message": ..., **fields}``.

    Typed fields (``op``, ``vmid``, ``node``, ``upid``, ``task``, ``hint``, …)
    let an agent read results without parsing the message prose. Fields passed
    explicitly are kept even when ``None`` (stable shape).
    """
    state: State = ctx.obj
    if state.json:
        print(json.dumps({"ok": True, "message": message, **fields}, default=str, indent=2))
    else:
        console.print(f"[green]✓[/green] {message}")
        detail = fields.get("upid") or fields.get("task") or fields.get("result")
        if detail:
            console.print(f"  [dim]{detail}[/dim]")
        if fields.get("hint"):
            console.print(f"  [dim]{fields['hint']}[/dim]")


def _emit_error(json_output: bool, error: str, message: str, code: int, need=None, extra=None) -> None:
    if json_output:
        payload = {"ok": False, "error": error, "message": message}
        if need:
            payload["need"] = need
        if extra:
            payload.update(extra)
        print(json.dumps(payload, default=str, indent=2))
    else:
        label = {
            "read_only": ("yellow", "Read-only"),
            "confirm_required": ("yellow", "Aborted"),
            "config": ("red", "Config error"),
            "network": ("red", "Network error"),
            "usage": ("red", "Usage error"),
            "error": ("red", "Error"),
        }[error]
        err_console.print(f"[{label[0]}]{label[1]}:[/{label[0]}] {message}")
        if extra and extra.get("hint"):
            err_console.print(f"[dim]{extra['hint']}[/dim]")
    raise typer.Exit(code)


def _emit_dry_run(op: str, node, params, vmid=None) -> None:
    payload: dict = {"dry_run": True, "op": op, "node": node}
    if vmid is not None:
        payload["vmid"] = vmid
    payload["params"] = params or {}
    print(json.dumps(payload, default=str, indent=2))


def _concise_network_reason(exc: BaseException) -> str:
    """Boil a requests/urllib3 exception wall down to its root cause."""
    text = str(exc)
    m = re.search(r"Failed to resolve '[^']+'", text)
    if m:
        return m.group(0)
    m = re.search(r"\[(?:WinError|Errno) [^\]]+\][^'\")(]*", text)
    if m:
        return m.group(0).strip()
    m = re.search(
        r"(connection refused|connection reset[^'\")]*|timed out|certificate verify failed[^'\")]*)",
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(0)
    return text[:160]


@contextmanager
def error_boundary(json_output: bool = False):
    """Translate exceptions into friendly messages, structured envelopes, and exit codes."""
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
    except PmoxError as exc:
        _emit_error(json_output, "error", str(exc), 1, extra=exc.extra)
    except requests.exceptions.SSLError as exc:
        _emit_error(
            json_output, "network",
            f"TLS verification failed talking to the Proxmox API: {_concise_network_reason(exc)}. "
            "For self-signed certificates use --no-verify-ssl (or PROXMOX_VERIFY_SSL=false).", 1,
        )
    except requests.exceptions.Timeout as exc:
        _emit_error(
            json_output, "network",
            f"Timed out talking to the Proxmox API: {_concise_network_reason(exc)}. "
            "The node may be slow or unreachable; check PROXMOX_HOST/PROXMOX_PORT.", 1,
        )
    except requests.exceptions.ConnectionError as exc:
        _emit_error(
            json_output, "network",
            f"Cannot reach the Proxmox API: {_concise_network_reason(exc)}. "
            "Check PROXMOX_HOST/PROXMOX_PORT and network connectivity.", 1,
        )
    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        _emit_error(json_output, "error", str(exc), 1)


# ---- shared option / argument definitions ----
vmid_arg = typer.Argument(..., metavar="VMID", help="Numeric VM/CT id.")
node_opt = typer.Option(None, "--node", "-n", help="Node name (auto-resolved from the cluster if omitted).")
yes_opt = typer.Option(False, "--yes", "-y", help="Confirm a destructive operation (required when non-interactive).")
fields_opt = typer.Option(
    None, "--fields",
    help="Comma-separated keys to keep in list output (e.g. vmid,name,status); missing keys are null.",
)


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
    help="Explore and manage a Proxmox VE cluster from the command line (AI-friendly). "
    "Run `pmox guide` for the full agent guide.",
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
    dangerous: Optional[bool] = typer.Option(
        None,
        "--dangerous/--no-dangerous",
        help="Enable dangerous (write/management) mode. Default is read-only (safe for AI exploration); "
        "--no-dangerous forces read-only even when PMOX_DANGEROUS is set.",
    ),
    wait: bool = typer.Option(
        False, "--wait/--no-wait",
        help="Wait for the resulting task to finish and report its outcome "
        "(provisioning commands always wait on their internal steps).",
    ),
    timeout: int = typer.Option(600, "--timeout", help="Seconds to wait on tasks (default 600)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the intended API call as JSON and exit without changing anything."
    ),
    host: Optional[str] = typer.Option(None, "--host", help="Proxmox host or IP."),
    port: Optional[int] = typer.Option(None, "--port", help="API port (default 8006)."),
    token_id: Optional[str] = typer.Option(None, "--token-id", help="API token id: user@realm!tokenname."),
    token_secret: Optional[str] = typer.Option(
        None, "--token-secret", help="API token secret (prefer the PROXMOX_TOKEN_SECRET env var; argv is visible in process listings)."
    ),
    verify_ssl: Optional[bool] = typer.Option(None, "--verify-ssl/--no-verify-ssl", help="Verify TLS cert (default: no)."),
    config: Optional[str] = typer.Option(None, "--config", help="Path to a TOML config file."),
    version: Optional[bool] = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True, help="Show pmox version and exit."
    ),
):
    # The dangerous gate honors only the *real* environment — captured before a
    # cwd .env gets loaded, so a stray project file can't silently enable writes.
    env_dangerous = os.environ.get("PMOX_DANGEROUS")

    # Load a local .env if python-dotenv is available (never fatal).
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    json_on = resolve_json_output(json_output, os.environ.get("PMOX_JSON"), _stream_isatty(sys.stdout))

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
        _emit_error(json_on, "config", str(exc), 2)

    dangerous_on = dangerous if dangerous is not None else _parse_bool(env_dangerous)
    ctx.obj = State(
        settings=settings,
        json_output=json_on,
        dangerous=dangerous_on,
        wait=wait,
        timeout=timeout,
        dry_run=dry_run,
    )


@app.command("guide")
def guide_cmd():
    """Print the agent/automation guide: safety model, exit codes, JSON envelopes, recipes."""
    print(guide.GUIDE)


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
def nodes_list(ctx: typer.Context, fields: Optional[str] = fields_opt):
    """List all nodes and their resource usage."""
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        rows = client.list_nodes()
        if fields:
            rows = _select_fields(rows, fields)
        emit(rows, columns=None if fields else NODE_COLUMNS, json_output=ctx.obj.json, title="Nodes")


@nodes_app.command("status")
def nodes_status(ctx: typer.Context, node: str = typer.Argument(..., help="Node name.")):
    """Show detailed status for a node."""
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        emit(client.node_status(node), json_output=ctx.obj.json, title=f"Node {node}")


def _wait_for_ip(ctx: typer.Context, client, kind: str, vmid: int, node: Optional[str],
                 scan: Optional[arp.ScanConfig]) -> dict:
    """Poll guest_ip_addresses until a primary address appears (bounded by --timeout).

    Agent-not-up / guest-still-booting errors are retried; a missing guest fails
    immediately (waiting won't make it appear).
    """
    deadline = time.monotonic() + ctx.obj.timeout
    last_error: Optional[Exception] = None
    while True:
        try:
            data = views.guest_ip_addresses(client, kind, vmid, node=node, scan=scan)
            if data.get("primary"):
                return data
            last_error = None
        except LookupError:
            raise
        except Exception as exc:  # noqa: BLE001 - transient while the guest boots
            last_error = exc
        if time.monotonic() >= deadline:
            detail = f" Last error: {last_error}" if last_error else ""
            raise PmoxError(
                f"Guest {vmid} did not report an IP address within {ctx.obj.timeout}s.{detail}",
                extra={
                    "vmid": vmid,
                    "hint": "For VMs, ensure qemu-guest-agent is installed and running in the guest; "
                            "for containers, ensure the guest is started.",
                },
            )
        time.sleep(_POLL_SECONDS)


def _warn_ignored_with_template(flags) -> None:
    """Warn (stderr) about options that --from-template silently discards."""
    ignored = [flag for flag, given in flags if given]
    if ignored:
        err_console.print(
            f"[yellow]Warning:[/yellow] --from-template ignores: {', '.join(ignored)} "
            f"(the clone inherits the template's hardware)."
        )


# ---- guest (vm / ct) app factory ----
def _make_power_command(group, kind, label, action, destructive, description):
    @group.command(action, help=f"{description} a {label}.")
    def _cmd(ctx: typer.Context, vmid: int = vmid_arg, node: Optional[str] = node_opt, yes: bool = yes_opt):
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, kind, vmid)
            _execute(
                ctx,
                op=f"{kind}.{action}",
                message=f"{description}: {label.lower()} {vmid} on {resolved}",
                node=resolved,
                call=lambda: client.guest_power(resolved, kind, vmid, action),
                params={"vmid": vmid, "action": action},
                vmid=vmid,
                destructive=destructive,
                yes=yes,
                confirm_msg=f"{action} {label.lower()} {vmid} on {resolved}",
            )

    return _cmd


def build_guest_app(kind: str, label: str) -> typer.Typer:
    descr = "QEMU VMs" if kind == "qemu" else "LXC containers"
    group = typer.Typer(help=f"Manage {label}s ({descr}).", no_args_is_help=True)

    @group.command("list")
    def _list(ctx: typer.Context, node: Optional[str] = node_opt, fields: Optional[str] = fields_opt):
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            rows = client.list_guests(kind, node=node)
            if fields:
                rows = _select_fields(rows, fields)
            emit(
                rows,
                columns=None if fields else GUEST_COLUMNS,
                json_output=ctx.obj.json,
                title=f"{label}s",
            )

    @group.command("status")
    def _status(ctx: typer.Context, vmid: int = vmid_arg, node: Optional[str] = node_opt):
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, kind, vmid)
            emit(client.guest_status(resolved, kind, vmid), json_output=ctx.obj.json, title=f"{label} {vmid} status")

    @group.command("config")
    def _config(ctx: typer.Context, vmid: int = vmid_arg, node: Optional[str] = node_opt):
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            resolved = node or _resolve_node_or_die(client, kind, vmid)
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

    @group.command(
        "ip",
        help=f"Show the live IP address(es) of a {label} (VM: guest agent, static config, or a "
        "same-LAN ARP scan by MAC; CT: via interfaces). With --wait, poll until an address "
        "appears (bounded by --timeout).",
    )
    def _ip(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        node: Optional[str] = node_opt,
        all_: bool = typer.Option(False, "--all", "-a", help="Include loopback, IPv6 link-local, and MAC addresses."),
    ):
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            settings = ctx.obj.settings
            scan = arp.ScanConfig(cidr=settings.net_cidr, host=settings.host, port=settings.port or 8006)
            if ctx.obj.wait:
                data = _wait_for_ip(ctx, client, kind, vmid, node, scan)
            else:
                data = views.guest_ip_addresses(client, kind, vmid, node=node, scan=scan)
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
            resolved = node or _resolve_node_or_die(client, kind, vmid)
            _execute(
                ctx,
                op=f"{kind}.set",
                message=f"Set {label.lower()} {vmid} on {resolved}",
                node=resolved,
                call=lambda: client.update_config(resolved, kind, vmid, **params),
                params=params,
                vmid=vmid,
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
            resolved = node or _resolve_node_or_die(client, kind, vmid)
            _execute(
                ctx,
                op=f"{kind}.resize",
                message=f"Resize {label.lower()} {vmid} disk {disk} to {size}",
                node=resolved,
                call=lambda: client.resize_disk(resolved, kind, vmid, disk, size),
                params={"vmid": vmid, "disk": disk, "size": size},
                vmid=vmid,
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
            provision.validate_guest_name(newname)
            resolved = node or _resolve_node_or_die(client, kind, vmid)
            key = "name" if kind == "qemu" else "hostname"
            _execute(
                ctx,
                op=f"{kind}.rename",
                message=f"Rename {label.lower()} {vmid} to {newname}",
                node=resolved,
                call=lambda: client.update_config(resolved, kind, vmid, **{key: newname}),
                params={"vmid": vmid, key: newname},
                vmid=vmid,
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
            resolved = node or _resolve_node_or_die(client, kind, vmid)
            current = client.guest_config(resolved, kind, vmid).get("tags", "")
            new_tags = merge_tags(current, add=add, remove=remove, set_=set_)
            _execute(
                ctx,
                op=f"{kind}.tag",
                message=f"Set tags on {label.lower()} {vmid}: {new_tags!r}",
                node=resolved,
                call=lambda: client.update_config(resolved, kind, vmid, tags=new_tags),
                params={"vmid": vmid, "tags": new_tags},
                vmid=vmid,
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
                vmid=vmid,
            )

    if kind == "qemu":

        @group.command("new", help="Create a VM: blank shell, cloud-init server with --image, or clone from --from-template.")
        def _new(
            ctx: typer.Context,
            name: Optional[str] = typer.Argument(None, help="VM name (optional)."),
            size: Optional[str] = typer.Option(None, "--size", help="Sizing profile: small | medium | large (default small; ignored with --from-template)."),
            disk: Optional[int] = typer.Option(None, "--disk", help="Disk size in GiB."),
            storage: Optional[str] = typer.Option(None, "--storage", help="Storage for the disk/cloud-init (default: auto-detect; local-lvm preferred)."),
            import_storage: Optional[str] = typer.Option(None, "--import-storage", help="Storage to hold the imported image (default: auto-detect one with 'import' content)."),
            node: Optional[str] = typer.Option(None, "--node", "-n", help="Node (auto-picked if one node)."),
            vmid: Optional[int] = typer.Option(None, "--vmid", help="VMID (auto-assigned if omitted)."),
            option: Optional[List[str]] = typer.Option(None, "--option", "-o", help="Extra create param key=value."),
            image: Optional[str] = typer.Option(None, "--image", help="Cloud image (catalog name, https URL, or volid). Enables cloud-init mode."),
            from_template: Optional[int] = typer.Option(None, "--from-template", help="Clone an existing template VMID into a cloud-init VM. Cloud-init mode."),
            ssh_key: Optional[List[str]] = typer.Option(None, "--ssh-key", help="Path to an SSH public key file (repeatable; ~ is expanded). Cloud-init mode."),
            ip: Optional[str] = typer.Option(None, "--ip", help="dhcp or <cidr>,gw=<ip>. Cloud-init mode."),
            ciuser: Optional[str] = typer.Option(None, "--ciuser", help="Cloud-init user. Cloud-init mode."),
            cipassword: Optional[str] = typer.Option(None, "--cipassword", help="Cloud-init password (visible in process listings — prefer SSH keys). Cloud-init mode."),
            nameserver: Optional[str] = typer.Option(None, "--nameserver", help="Cloud-init DNS server(s). Cloud-init mode."),
        ):
            with error_boundary(ctx.obj.json):
                client = _get_client(ctx)
                if image and from_template is not None:
                    raise ValueError("--image and --from-template are mutually exclusive.")
                target_vmid = vmid if vmid is not None else int(client.cluster_nextid())

                if from_template is not None:
                    _warn_ignored_with_template([
                        ("--size", size is not None),
                        ("--storage", storage is not None),
                        ("--import-storage", import_storage is not None),
                        ("-o/--option", bool(option)),
                    ])
                    target_node = node or _resolve_node_or_die(client, "qemu", from_template)
                    sshkeys = provision.read_ssh_keys(ssh_key)
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
                    _ok(
                        ctx, f"Cloned template {from_template} -> VM {target_vmid} on {target_node}",
                        op="qemu.new.from_template", vmid=target_vmid, node=target_node, template=from_template,
                    )
                    return

                target_node = node or _single_node_or_die(client)
                profile = catalog.size_params(size or "small")

                if image:
                    sshkeys = provision.read_ssh_keys(ssh_key)
                    resolved_storage = provision.resolve_disk_storage(client, target_node, storage)
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
                        storage=resolved_storage,
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
                    _ok(
                        ctx, f"Created cloud-init VM {target_vmid} on {target_node} from {image}",
                        op="qemu.new.image", vmid=target_vmid, node=target_node,
                    )
                    return

                # blank shell (B2 behavior)
                if name:
                    provision.validate_guest_name(name)
                params = dict(profile)
                params.update({"scsihw": "virtio-scsi-single", "net0": "virtio,bridge=vmbr0", "ostype": "l26"})
                if name:
                    params["name"] = name
                if disk:
                    resolved_storage = provision.resolve_disk_storage(client, target_node, storage)
                    params["scsi0"] = f"{resolved_storage}:{disk},iothread=1"
                    params["boot"] = "order=scsi0"
                params.update(parse_options(option))
                _execute(
                    ctx,
                    op="qemu.new",
                    message=f"Create VM {target_vmid} on {target_node}",
                    node=target_node,
                    call=lambda: client.create_guest(target_node, "qemu", target_vmid, **params),
                    params={"vmid": target_vmid, **params},
                    vmid=target_vmid,
                )

        @group.command("up", help="Create a ready-to-SSH VM in one call (DHCP by default; pool/--ip for a static address).")
        def _up(
            ctx: typer.Context,
            name: str = typer.Argument(..., help="VM name."),
            image: Optional[str] = typer.Option(None, "--image", help="Cloud image: catalog name, https URL, or import volid."),
            from_template: Optional[int] = typer.Option(None, "--from-template", help="Clone an existing template VMID instead of importing an image."),
            size: Optional[str] = typer.Option(None, "--size", help="Sizing profile: small | medium | large (default small; ignored with --from-template)."),
            disk: Optional[int] = typer.Option(None, "--disk", help="Disk size in GiB."),
            node: Optional[str] = typer.Option(None, "--node", "-n", help="Node (auto-picked if one node)."),
            storage: Optional[str] = typer.Option(None, "--storage", help="Storage for the disk/cloud-init (default: auto-detect; local-lvm preferred)."),
            import_storage: Optional[str] = typer.Option(None, "--import-storage", help="Storage to hold the imported image (default: auto-detect)."),
            ip: Optional[str] = typer.Option(None, "--ip", help="dhcp, or <cidr>,gw=<ip> for a static address (default: [network] pool allocation if configured, else DHCP)."),
            ssh_key: Optional[List[str]] = typer.Option(None, "--ssh-key", help="SSH public key path (repeatable; ~ is expanded; default ~/.ssh/id_ed25519.pub, generated if missing)."),
            no_ssh_key: bool = typer.Option(False, "--no-ssh-key", help="Don't attach or generate an SSH key."),
            ciuser: Optional[str] = typer.Option(None, "--ciuser", help="Cloud-init user (default from config)."),
            vmid: Optional[int] = typer.Option(None, "--vmid", help="VMID (auto-assigned if omitted)."),
            no_agent_template: bool = typer.Option(
                False, "--no-agent-template",
                help="Build from the raw image even when an agent template for it exists (see `pmox template build`).",
            ),
        ):
            with error_boundary(ctx.obj.json):
                client = _get_client(ctx)
                settings = ctx.obj.settings
                if (image is None) == (from_template is None):
                    raise ValueError("vm up needs exactly one of --image or --from-template.")
                target_vmid = vmid if vmid is not None else int(client.cluster_nextid())
                profile = catalog.size_params(size or "small")

                agent_tpl = None
                if from_template is not None:
                    _warn_ignored_with_template([
                        ("--size", size is not None),
                        ("--storage", storage is not None),
                        ("--import-storage", import_storage is not None),
                    ])
                    target_node = node or _resolve_node_or_die(client, "qemu", from_template)
                    resolved_storage = None
                    resolved_import = None
                else:
                    target_node = node or _single_node_or_die(client)
                    if settings.agent_templates and not no_agent_template:
                        agent_tpl = views.find_agent_template(client, image, node=target_node)
                    if agent_tpl is not None:
                        # clone the agent template instead of importing the raw image
                        resolved_storage = None
                        resolved_import = None
                    else:
                        resolved_storage = provision.resolve_disk_storage(client, target_node, storage)
                        needs_import = catalog.resolve_image(image)["kind"] != "volid"
                        resolved_import = (
                            provision.resolve_import_storage(client, target_node, import_storage or settings.default_import_storage)
                            if needs_import else None
                        )

                # one-shot default flow: no agent template yet -> build one, then clone it
                would_build = False
                build_user = None
                if (from_template is None and agent_tpl is None and settings.agent_templates
                        and not no_agent_template and not no_ssh_key):
                    build_user = ciuser or settings.default_ciuser or catalog.resolve_image(image).get("user")
                    would_build = bool(build_user)
                template_built = False

                if ip and ip.strip().lower() != "dhcp":
                    ipconfig = provision.build_ipconfig(ip)
                    chosen_ip = ip.split(",", 1)[0].split("/", 1)[0]
                elif ip is None and settings.net_cidr and settings.net_gateway and settings.net_pool:
                    allocated = ipam.allocate_ip(
                        client, cidr=settings.net_cidr, gateway=settings.net_gateway, pool=settings.net_pool
                    )
                    ipconfig = f"ip={allocated},gw={settings.net_gateway}"
                    chosen_ip = allocated.split("/", 1)[0]
                else:
                    # zero-config default (or explicit --ip dhcp): let the VM DHCP.
                    ipconfig = provision.build_ipconfig("dhcp")
                    chosen_ip = None

                key_paths: List[str] = []
                if not no_ssh_key:
                    key_paths = list(ssh_key or []) or [
                        settings.default_ssh_key or str(Path.home() / ".ssh" / "id_ed25519.pub")
                    ]
                chosen_ciuser = ciuser or settings.default_ciuser

                def _build(sshkeys):
                    if from_template is not None:
                        return provision.build_vm_clone_plan(
                            client, node=target_node, template_id=from_template, newid=target_vmid,
                            name=name, disk=disk, sshkeys=sshkeys, ipconfig=ipconfig,
                            ciuser=chosen_ciuser, cipassword=None, nameserver=settings.net_nameserver,
                            full=True, start=True,
                        )
                    if agent_tpl is not None:
                        return provision.build_vm_clone_plan(
                            client, node=target_node, template_id=agent_tpl["vmid"], newid=target_vmid,
                            name=name, disk=disk, sshkeys=sshkeys, ipconfig=ipconfig,
                            ciuser=chosen_ciuser, cipassword=None, nameserver=settings.net_nameserver,
                            full=True, start=True,
                            cores=profile["cores"], memory=profile["memory"], storage=storage,
                        )
                    return provision.build_vm_image_plan(
                        client, node=target_node, vmid=target_vmid, name=name,
                        cores=profile["cores"], memory=profile["memory"], disk=disk,
                        storage=resolved_storage, import_storage=resolved_import, image=image,
                        sshkeys=sshkeys, ipconfig=ipconfig, ciuser=chosen_ciuser,
                        cipassword=None, nameserver=settings.net_nameserver, start=True,
                    )

                if ctx.obj.dry_run:
                    if would_build:
                        print(json.dumps({
                            "dry_run": True, "op": "qemu.up", "node": target_node, "vmid": target_vmid,
                            "agent_template": None, "agent_template_action": "build+clone",
                            "hint": (f"First run builds the agent template for {image}, then clones it "
                                     f"for this VM. Preview the build with `pmox --dry-run template build {image}`."),
                        }, default=str, indent=2))
                        return
                    # dry-run must be side-effect-free: read existing keys, never generate one
                    existing = "\n".join(
                        Path(p).expanduser().read_text().strip()
                        for p in key_paths if Path(p).expanduser().exists()
                    ) or None
                    print(json.dumps({
                        "dry_run": True, "op": "qemu.up", "node": target_node, "vmid": target_vmid,
                        "agent_template": agent_tpl["vmid"] if agent_tpl else None,
                        "plan": _build(existing),
                    }, default=str, indent=2))
                    return

                require_dangerous(ctx.obj.dangerous)
                if would_build:
                    if not ctx.obj.json:
                        err_console.print(
                            f"[dim]No agent template for {image} on {target_node} — building one now "
                            f"(one-time, a few minutes). Future `vm up --image {image}` calls will "
                            f"clone it in seconds.[/dim]"
                        )
                    built = _agent_template_build(
                        ctx, client, image=image, node=target_node,
                        user=build_user, ssh_key=ssh_key, import_storage=import_storage,
                    )
                    agent_tpl = {"vmid": built["vmid"]}
                    template_built = True
                    if vmid is None:
                        # the build consumed the VMID we had reserved for this VM
                        target_vmid = int(client.cluster_nextid())
                sshkeys = "\n".join(provision.ensure_ssh_key(p) for p in key_paths) or None
                provision.execute_plan(client, target_node, _build(sshkeys), waiter=lambda n, upid: _maybe_wait(ctx, n, upid))

                ssh_val = f"ssh {chosen_ciuser}@{chosen_ip}" if (chosen_ip and chosen_ciuser) else None
                if chosen_ip:
                    hint = f"Connect with `{ssh_val}`." if ssh_val else (
                        f"Connect to {chosen_ip} as the image's default user (e.g. 'ubuntu' on Ubuntu)."
                    )
                elif from_template is not None:
                    hint = (f"The address comes from DHCP; `pmox vm ip {target_vmid} --wait` returns it "
                            f"(via the template's guest agent, or a same-LAN ARP scan).")
                elif agent_tpl is not None:
                    hint = (f"Run `pmox vm ip {target_vmid} --wait` — the clone's guest agent reports "
                            f"the DHCP address (cloned from agent template {agent_tpl['vmid']}).")
                else:
                    hint = (f"The address comes from DHCP; run `pmox vm ip {target_vmid} --wait` — found via "
                            f"a same-LAN ARP scan (or check your DHCP leases). Run "
                            f"`pmox --dangerous template build {image}` once for instant, reliable "
                            f"guest-agent IPs, or use --ip / a [network] pool for a static address.")
                if ctx.obj.json:
                    _ok(
                        ctx, f"VM {target_vmid} ({name}) is up on {target_node}",
                        op="qemu.up", vmid=target_vmid, name=name, node=target_node,
                        ip=chosen_ip, ssh=ssh_val,
                        template=(agent_tpl["vmid"] if agent_tpl else from_template),
                        agent=agent_tpl is not None, template_built=template_built, hint=hint,
                    )
                elif chosen_ip:
                    console.print(f"VM {target_vmid}  {name}  ip {chosen_ip}")
                    console.print(ssh_val or f"ssh <image's default user>@{chosen_ip}")
                else:
                    console.print(f"VM {target_vmid}  {name}  ip via DHCP (not known yet)")
                    console.print(hint)

    if kind == "lxc":

        @group.command("new", help="Create an LXC container from a template, ready to SSH.")
        def _ct_new(
            ctx: typer.Context,
            name: Optional[str] = typer.Argument(None, help="Hostname (optional)."),
            template: str = typer.Option(..., "--template", help="Template: catalog/aplinfo name or a vztmpl volid."),
            size: str = typer.Option("small", "--size", help="Sizing profile: small | medium | large."),
            disk: int = typer.Option(8, "--disk", help="Root filesystem size in GiB."),
            storage: Optional[str] = typer.Option(None, "--storage", help="Storage for the rootfs (default: auto-detect; local-lvm preferred)."),
            template_storage: str = typer.Option("local", "--template-storage", help="Storage to download the template into (vztmpl)."),
            node: Optional[str] = typer.Option(None, "--node", "-n", help="Node (auto-picked if one node)."),
            vmid: Optional[int] = typer.Option(None, "--vmid", help="VMID (auto-assigned if omitted)."),
            ssh_key: Optional[List[str]] = typer.Option(None, "--ssh-key", help="Path to an SSH public key file (repeatable; ~ is expanded)."),
            ip: str = typer.Option("dhcp", "--ip", help="dhcp or <cidr>,gw=<ip>."),
            password: Optional[str] = typer.Option(None, "--password", help="Root password (visible in process listings — prefer SSH keys)."),
        ):
            with error_boundary(ctx.obj.json):
                client = _get_client(ctx)
                target_node = node or _single_node_or_die(client)
                target_vmid = vmid if vmid is not None else int(client.cluster_nextid())
                profile = catalog.size_params(size)
                sshkeys = provision.read_ssh_keys(ssh_key)
                resolved_storage = provision.resolve_disk_storage(client, target_node, storage, content="rootdir")
                plan = provision.build_ct_plan(
                    client,
                    node=target_node,
                    vmid=target_vmid,
                    hostname=name,
                    template=template,
                    storage=resolved_storage,
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
                _ok(
                    ctx, f"Created container {target_vmid} on {target_node} from {template}",
                    op="lxc.new", vmid=target_vmid, node=target_node,
                )

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
            resolved = node or _resolve_node_or_die(client, kind, vmid)
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
                vmid=newid,
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
            resolved = node or _resolve_node_or_die(client, kind, vmid)
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
                vmid=vmid,
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
            resolved = node or _resolve_node_or_die(client, kind, vmid)
            _execute(
                ctx,
                op=f"{kind}.delete",
                message=f"Deleted {label.lower()} {vmid} on {resolved}",
                node=resolved,
                call=lambda: client.delete_guest(resolved, kind, vmid, purge=purge),
                params={"vmid": vmid, "purge": purge},
                vmid=vmid,
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
            resolved = node or _resolve_node_or_die(client, kind, vmid)
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
            resolved = node or _resolve_node_or_die(client, kind, vmid)
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
                vmid=vmid,
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
            resolved = node or _resolve_node_or_die(client, kind, vmid)
            _execute(
                ctx,
                op=f"{kind}.snapshot.delete",
                message=f"Deleted snapshot {name!r} of {label.lower()} {vmid}",
                node=resolved,
                call=lambda: client.delete_snapshot(resolved, kind, vmid, name),
                params={"vmid": vmid, "snapname": name},
                vmid=vmid,
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
            resolved = node or _resolve_node_or_die(client, kind, vmid)
            _execute(
                ctx,
                op=f"{kind}.snapshot.rollback",
                message=f"Rolling back {label.lower()} {vmid} → {name!r}",
                node=resolved,
                call=lambda: client.rollback_snapshot(resolved, kind, vmid, name),
                params={"vmid": vmid, "snapname": name},
                vmid=vmid,
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
def storage_list(ctx: typer.Context, node: Optional[str] = node_opt, fields: Optional[str] = fields_opt):
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        rows = client.cluster_resources(type="storage")
        if node:
            rows = [r for r in rows if r.get("node") == node]
        if fields:
            rows = _select_fields(rows, fields)
        emit(rows, columns=None if fields else STORAGE_COLUMNS, json_output=ctx.obj.json, title="Storage")


@storage_app.command("content")
def storage_content(
    ctx: typer.Context,
    storage: str = typer.Argument(..., help="Storage id."),
    node: Optional[str] = typer.Option(None, "--node", "-n", help="Node name (auto-picked on a single-node cluster)."),
    fields: Optional[str] = fields_opt,
):
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        resolved = node or _single_node_or_die(client)
        rows = client.storage_content(resolved, storage)
        if fields:
            rows = _select_fields(rows, fields)
        emit(
            rows,
            columns=None if fields else CONTENT_COLUMNS,
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
    fields: Optional[str] = fields_opt,
):
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        rows = client.cluster_resources(type=type)
        if fields:
            rows = _select_fields(rows, fields)
        emit(
            rows,
            columns=None if fields else RESOURCE_COLUMNS,
            json_output=ctx.obj.json,
            title="Cluster resources",
        )


# ---- tasks ----
task_app = typer.Typer(help="Inspect node tasks.", no_args_is_help=True)


@task_app.command("list")
def task_list(
    ctx: typer.Context,
    node: Optional[str] = typer.Option(None, "--node", "-n", help="Node name (auto-picked on a single-node cluster)."),
    limit: int = typer.Option(50, "--limit", help="Max tasks to show."),
    fields: Optional[str] = fields_opt,
):
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        resolved = node or _single_node_or_die(client)
        rows = client.list_tasks(resolved, limit=limit)
        if fields:
            rows = _select_fields(rows, fields)
        emit(rows, columns=None if fields else TASK_COLUMNS, json_output=ctx.obj.json, title=f"{resolved} tasks")


@task_app.command("status")
def task_status(
    ctx: typer.Context,
    upid: str = typer.Argument(..., help="Task UPID."),
    node: Optional[str] = typer.Option(None, "--node", "-n", help="Node name (default: parsed from the UPID)."),
):
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        resolved = _node_for_task(node, upid)
        emit(client.task_status(resolved, upid), json_output=ctx.obj.json, title="Task status")


@task_app.command("log")
def task_log(
    ctx: typer.Context,
    upid: str = typer.Argument(..., help="Task UPID."),
    node: Optional[str] = typer.Option(None, "--node", "-n", help="Node name (default: parsed from the UPID)."),
):
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        resolved = _node_for_task(node, upid)
        data = client.task_log(resolved, upid)
        if ctx.obj.json:
            emit(data, json_output=True)
            return
        for line in data:
            console.print(line.get("t", "") if isinstance(line, dict) else str(line))


@task_app.command("wait")
def task_wait(
    ctx: typer.Context,
    upid: str = typer.Argument(..., help="Task UPID to wait for."),
    node: Optional[str] = typer.Option(None, "--node", "-n", help="Node name (default: parsed from the UPID)."),
):
    """Poll a task until it finishes (read-only; honors --timeout). Exit 1 if the task failed.

    Useful to resume waiting after a --wait timeout or an interrupted provisioning run.
    """
    with error_boundary(ctx.obj.json):
        if not upid.startswith("UPID:"):
            raise ValueError(f"{upid!r} is not a task UPID (expected 'UPID:<node>:...').")
        resolved = _node_for_task(node, upid)
        status = _maybe_wait(ctx, resolved, upid)
        _ok(
            ctx, f"Task finished: {status.get('exitstatus', 'OK')}",
            op="task.wait", node=resolved, upid=upid, task=status,
        )


# ---- image catalog / pull ----
image_app = typer.Typer(help="VM cloud images: list the catalog and pull them to storage.", no_args_is_help=True)


@image_app.command("list")
def image_list(
    ctx: typer.Context,
    ct: bool = typer.Option(False, "--ct", help="List LXC container templates (live, from the node) instead of the VM catalog."),
    node: Optional[str] = typer.Option(None, "--node", "-n", help="Node for --ct (auto-picked on a single-node cluster)."),
    fields: Optional[str] = fields_opt,
):
    """List VM cloud images (catalog) or, with --ct, container templates from a node."""
    with error_boundary(ctx.obj.json):
        if ct:
            client = _get_client(ctx)
            resolved = node or _single_node_or_die(client)
            rows = client.list_appliances(resolved)
            if fields:
                rows = _select_fields(rows, fields)
            emit(rows, json_output=ctx.obj.json, title="Container templates")
            return
        rows = [{"name": name, "url": e["url"], "filename": e["filename"]} for name, e in catalog.IMAGE_CATALOG.items()]
        if fields:
            rows = _select_fields(rows, fields)
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
    checksum: Optional[str] = typer.Option(None, "--checksum", help="Verify the download: <algo>:<hexdigest> (e.g. sha256:...). Plain image pulls only."),
):
    """Download a VM cloud image (or, with --ct, an LXC container template) to a storage; with --as-template, build a reusable golden VM template. Needs --dangerous."""
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)

        if ct and as_template:
            raise ValueError("--ct and --as-template are mutually exclusive.")
        if checksum and (ct or as_template):
            raise ValueError(
                "--checksum only applies to a plain image pull. Pull with --checksum first; "
                "the verified image is then reused by --as-template or vm new (downloads are cached)."
            )
        ck_algo, ck_digest = provision.parse_checksum(checksum) if checksum else (None, None)

        if ct:
            names = [a.get("template", "") for a in client.list_appliances(node)]
            filename = provision.match_appliance(names, image)
            if not filename:
                raise ValueError(
                    f"No container template matching {image!r} on {node}. "
                    f"See `pmox image list --ct --node {node}`."
                )
            volid = f"{storage}:vztmpl/{filename}"
            if ctx.obj.dry_run:
                print(json.dumps({"dry_run": True, "op": "image.pull.ct", "node": node, "params": {"volid": volid, "template": filename}}, default=str, indent=2))
                return
            require_dangerous(ctx.obj.dangerous)
            if any(c.get("volid") == volid for c in client.storage_content(node, storage)):
                _ok(ctx, f"Template already present: {volid}", op="image.pull.ct", node=node, volid=volid)
                return
            upid = client.download_appliance(node, storage, filename)
            _maybe_wait(ctx, node, upid)
            _ok(ctx, f"Pulled container template {image} -> {volid}", op="image.pull.ct", node=node, volid=volid, upid=upid)
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
            _ok(
                ctx, f"Built template {target_vmid} on {node} from {image}",
                op="image.pull.template", vmid=target_vmid, node=node,
            )
            return

        volid = f"{storage}:import/{spec['filename']}"
        if ctx.obj.dry_run:
            print(json.dumps({"dry_run": True, "op": "image.pull", "node": node, "params": {"volid": volid, "url": spec["url"]}}, default=str, indent=2))
            return
        require_dangerous(ctx.obj.dangerous)
        if any(c.get("volid") == volid for c in client.storage_content(node, storage)):
            _ok(ctx, f"Image already present: {volid}", op="image.pull", node=node, volid=volid)
            return
        upid = client.download_url(
            node, storage, url=spec["url"], content="import", filename=spec["filename"],
            checksum=ck_digest or spec["checksum"], checksum_algorithm=ck_algo or spec["algo"],
        )
        _maybe_wait(ctx, node, upid)
        _ok(ctx, f"Pulled {image} → {volid}", op="image.pull", node=node, volid=volid, upid=upid)


# ---- agent templates ----
template_app = typer.Typer(
    help="Agent-enabled golden templates: build one per image with `template build`; "
    "`vm up --image` then clones it automatically (guest-agent IPs, no ARP scans).",
    no_args_is_help=True,
)

TEMPLATE_COLUMNS = [
    Column("VMID", "vmid"),
    Column("Name", "name"),
    Column("Node", "node"),
    Column("Agent", row_formatter=lambda r: "yes" if r.get("agent") else "-"),
    Column("Tags", "tags"),
]


def _wait_for_agent(ctx: typer.Context, client, node: str, vmid: int) -> None:
    """Poll the guest agent through the PVE API until it answers (bounded by --timeout)."""
    deadline = time.monotonic() + ctx.obj.timeout
    while True:
        try:
            client.agent_network_interfaces(node, vmid)
            return
        except Exception as exc:  # noqa: BLE001 - agent service still starting
            if time.monotonic() >= deadline:
                raise PmoxError(
                    f"The guest agent in VM {vmid} did not respond within {ctx.obj.timeout}s: {exc}"
                ) from exc
        time.sleep(_POLL_SECONDS)


def _wait_for_stopped(ctx: typer.Context, client, node: str, vmid: int) -> None:
    """Poll until the guest reports 'stopped' (template conversion needs a stopped VM)."""
    deadline = time.monotonic() + ctx.obj.timeout
    while client.guest_status(node, "qemu", vmid).get("status") != "stopped":
        if time.monotonic() >= deadline:
            raise PmoxError(f"VM {vmid} did not stop within {ctx.obj.timeout}s.")
        time.sleep(_POLL_SECONDS)


@template_app.command("list")
def template_list(ctx: typer.Context, node: Optional[str] = node_opt):
    """List VM templates cluster-wide; 'agent' marks pmox-built agent templates."""
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        rows = []
        for r in client.cluster_resources(type="vm"):
            if r.get("type") != "qemu" or not r.get("template"):
                continue
            if node and r.get("node") != node:
                continue
            tags = str(r.get("tags") or "")
            rows.append({
                "vmid": r.get("vmid"), "name": r.get("name"), "node": r.get("node"),
                "tags": tags, "agent": guestops.AGENT_TAG in re.split(r"[;,]", tags),
            })
        rows.sort(key=lambda row: row["vmid"] or 0)
        emit(rows, columns=TEMPLATE_COLUMNS, json_output=ctx.obj.json, title="Templates")


@template_app.command("build")
def template_build(
    ctx: typer.Context,
    image: str = typer.Argument(..., help="Cloud image: catalog name (e.g. ubuntu-24.04), https URL, or import volid."),
    node: Optional[str] = typer.Option(None, "--node", "-n", help="Node to build on (auto-picked if one node)."),
    vmid: Optional[int] = typer.Option(None, "--vmid", help="VMID for the template (auto-assigned if omitted)."),
    name: Optional[str] = typer.Option(None, "--name", help="Template name (default: agent-<image>)."),
    ip: Optional[str] = typer.Option(None, "--ip", help="Static <cidr>,gw=<ip> for the build VM (default: [network] pool allocation, else DHCP + discovery)."),
    user: Optional[str] = typer.Option(None, "--user", help="Login user for the SSH step (default: the image's cloud-init user; known for catalog images)."),
    ssh_key: Optional[List[str]] = typer.Option(None, "--ssh-key", help="SSH public key path (default ~/.ssh/id_ed25519.pub, generated if missing). The private half must sit next to it."),
    storage: Optional[str] = typer.Option(None, "--storage", help="Disk storage (default: auto-detect; local-lvm preferred)."),
    import_storage: Optional[str] = typer.Option(None, "--import-storage", help="Storage for the imported image (default: auto-detect)."),
    size: str = typer.Option("small", "--size", help="Build VM sizing profile; clones resize per their own --size."),
    disk: Optional[int] = typer.Option(None, "--disk", help="Template disk size in GiB (default: the image's size; clones can grow it)."),
):
    """Build an agent-enabled golden template from a cloud image. Needs --dangerous.

    Boots a VM, SSHes in with the key pmox manages, installs qemu-guest-agent,
    cleans the guest for cloning (cloud-init state, machine-id, host keys), and
    converts it to a tagged template. `vm up --image <image>` clones it
    automatically from then on, so `vm ip --wait` gets agent-reported addresses.
    This is the one pmox operation that reaches inside a guest — over SSH, using
    the key it injected moments earlier.
    """
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        target_node = node or _single_node_or_die(client)

        existing = views.find_agent_template(client, image, node=target_node)
        if existing:
            _ok(
                ctx,
                f"Agent template for {image} already exists: {existing.get('name')} ({existing['vmid']}) on {target_node}",
                op="qemu.template.build", vmid=existing["vmid"], node=target_node,
                name=existing.get("name"), image=image, tags=existing.get("tags"), reused=True,
                hint=f"`pmox --dangerous vm up <name> --image {image}` clones it automatically. Delete the template to rebuild.",
            )
            return

        if ctx.obj.dry_run:
            payload = _agent_template_build(
                ctx, client, image=image, node=target_node, vmid=vmid, name=name, ip=ip,
                user=user, ssh_key=ssh_key, storage=storage, import_storage=import_storage,
                size=size, disk=disk, dry_run=True,
            )
            print(json.dumps(payload, default=str, indent=2))
            return

        require_dangerous(ctx.obj.dangerous)
        result = _agent_template_build(
            ctx, client, image=image, node=target_node, vmid=vmid, name=name, ip=ip,
            user=user, ssh_key=ssh_key, storage=storage, import_storage=import_storage,
            size=size, disk=disk,
        )
        _ok(
            ctx, f"Agent template {result['name']} ({result['vmid']}) ready on {target_node}",
            op="qemu.template.build", vmid=result["vmid"], node=target_node,
            name=result["name"], image=image, tags=result["tags"], user=result["user"],
            reused=False,
            hint=(f"`pmox --dangerous vm up <name> --image {image}` now clones this template "
                  f"automatically; `pmox vm ip <vmid> --wait` gets agent-reported IPs."),
        )


def _agent_template_build(
    ctx: typer.Context, client, *, image: str, node: str,
    vmid: Optional[int] = None, name: Optional[str] = None, ip: Optional[str] = None,
    user: Optional[str] = None, ssh_key: Optional[List[str]] = None,
    storage: Optional[str] = None, import_storage: Optional[str] = None,
    size: str = "small", disk: Optional[int] = None, dry_run: bool = False,
) -> dict:
    """Resolve and run an agent-template build on ``node``.

    Returns the result fields (vmid/name/tags/user) — or, with ``dry_run``, the
    preview payload. The caller owns the --dangerous gate and the
    existing-template short-circuit; this is shared by `template build` and the
    `vm up` first-use auto-build.
    """
    settings = ctx.obj.settings
    login_user = user or catalog.resolve_image(image).get("user")
    if not login_user:
        raise ValueError(
            f"Cannot determine the login user for {image!r}; pass --user "
            f"(the image's default cloud-init user, e.g. --user ubuntu)."
        )
    target_vmid = vmid if vmid is not None else int(client.cluster_nextid())
    tpl_name = name or guestops.agent_template_name(image)
    profile = catalog.size_params(size)
    resolved_storage = provision.resolve_disk_storage(client, node, storage)
    needs_import = catalog.resolve_image(image)["kind"] != "volid"
    resolved_import = (
        provision.resolve_import_storage(client, node, import_storage or settings.default_import_storage)
        if needs_import else None
    )

    if ip:
        ipconfig = provision.build_ipconfig(ip)
        boot_ip = ip.split(",", 1)[0].split("/", 1)[0]
        static_boot = True
    elif settings.net_cidr and settings.net_gateway and settings.net_pool:
        allocated = ipam.allocate_ip(
            client, cidr=settings.net_cidr, gateway=settings.net_gateway, pool=settings.net_pool
        )
        ipconfig = f"ip={allocated},gw={settings.net_gateway}"
        boot_ip = allocated.split("/", 1)[0]
        static_boot = True
    else:
        ipconfig = provision.build_ipconfig("dhcp")
        boot_ip = None
        static_boot = False

    key_paths = list(ssh_key or []) or [
        settings.default_ssh_key or str(Path.home() / ".ssh" / "id_ed25519.pub")
    ]

    def _plan(sshkeys):
        return provision.build_vm_image_plan(
            client, node=node, vmid=target_vmid, name=tpl_name,
            cores=profile["cores"], memory=profile["memory"], disk=disk,
            storage=resolved_storage, import_storage=resolved_import, image=image,
            sshkeys=sshkeys, ipconfig=ipconfig, ciuser=login_user,
            cipassword=None, nameserver=settings.net_nameserver, start=True,
        )

    post_steps = [
        "install agent", "verify agent",
        "shutdown", "finalize config", "convert to template",
    ]
    if dry_run:
        # dry-run must be side-effect-free: read existing keys, never generate one
        existing_keys = "\n".join(
            Path(p).expanduser().read_text().strip()
            for p in key_paths if Path(p).expanduser().exists()
        ) or None
        return {
            "dry_run": True, "op": "qemu.template.build", "node": node,
            "vmid": target_vmid, "plan": _plan(existing_keys), "post_steps": post_steps,
        }

    sshkeys = "\n".join(provision.ensure_ssh_key(p) for p in key_paths)
    priv_key = guestops.private_key_path(key_paths[0])
    plan = _plan(sshkeys)
    provision.execute_plan(client, node, plan, waiter=lambda n, upid: _maybe_wait(ctx, n, upid))
    completed = [s["describe"] or s["op"] for s in plan]

    def _install():
        # Transport order is deliberate: MAC-derived IPv6 link-local first (a pure
        # function of the config — NDP resolution, immune to ARP spoofing and DHCP
        # state), then the static IPv4 we assigned. DHCP discovery is the last
        # resort, reached only when nothing else can (no candidates at all, or
        # every link-local scope is dead — e.g. IPv6 disabled on this host).
        mac = next((m for _, m in arp.extract_macs(client.guest_config(node, "qemu", target_vmid))), None)
        candidates = guestops.link_local_candidates(mac) if mac else []
        if boot_ip:
            candidates.append(boot_ip)
        try:
            if not candidates:
                raise RuntimeError("the guest config exposes no MAC and no static address was assigned")
            guestops.establish_agent_ssh(login_user, candidates, priv_key)
            return
        except RuntimeError:
            if static_boot:
                raise  # discovery would only re-find the same static address
        scan = arp.ScanConfig(cidr=settings.net_cidr, host=settings.host, port=settings.port or 8006)
        primary = _wait_for_ip(ctx, client, "qemu", target_vmid, node, scan)["primary"]
        guestops.establish_agent_ssh(login_user, [primary], priv_key)

    def _shutdown():
        upid = client.guest_power(node, "qemu", target_vmid, action="shutdown")
        _maybe_wait(ctx, node, upid)
        _wait_for_stopped(ctx, client, node, target_vmid)

    def _finalize():
        params = {"tags": guestops.agent_tags(image)}
        if static_boot:
            params["ipconfig0"] = "ip=dhcp"  # release the bootstrap address for clones
        client.update_config(node, "qemu", target_vmid, **params)

    steps = [
        ("install agent", _install),
        ("verify agent", lambda: _wait_for_agent(ctx, client, node, target_vmid)),
        ("shutdown", _shutdown),
        ("finalize config", _finalize),
        ("convert to template", lambda: client.convert_to_template(node, "qemu", target_vmid)),
    ]
    for label, fn in steps:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - wrap with recovery context
            raise PmoxError(
                f"Template build failed at {label!r}: {exc}",
                extra={
                    "vmid": target_vmid, "node": node,
                    "failed_step": label, "completed_steps": list(completed),
                    "hint": (
                        f"VM {target_vmid} was created on {node} but the build stopped at "
                        f"{label!r}. Inspect with `pmox vm describe {target_vmid}`, then delete it "
                        f"(`pmox --dangerous vm delete {target_vmid} --yes`) and rerun, or finish manually."
                    ),
                },
            ) from exc
        completed.append(label)

    return {"vmid": target_vmid, "name": tpl_name, "tags": guestops.agent_tags(image), "user": login_user}


app.add_typer(nodes_app, name="nodes")
app.add_typer(vm_app, name="vm")
app.add_typer(ct_app, name="ct")
app.add_typer(storage_app, name="storage")
app.add_typer(cluster_app, name="cluster")
app.add_typer(task_app, name="task")
app.add_typer(image_app, name="image")
app.add_typer(template_app, name="template")


def _json_mode_from_argv(argv: List[str]) -> bool:
    """Best-effort JSON-mode resolution for errors raised before Typer has a context."""
    flag: Optional[bool] = None
    for tok in argv:
        if tok == "--":
            break
        if tok == "--json":
            flag = True
        elif tok == "--no-json":
            flag = False
    return resolve_json_output(flag, os.environ.get("PMOX_JSON"), _stream_isatty(sys.stdout))


def _is_no_args_help_error(exc) -> bool:
    no_args_cls = getattr(click.exceptions, "NoArgsIsHelpError", None)
    return no_args_cls is not None and isinstance(exc, no_args_cls)


def _handle_parse_error(argv: List[str], exc) -> int:
    """Emit a click parse failure as a JSON envelope (click's own text in human mode).

    Click usage errors exit 2 — same code as missing config — so the envelope's
    ``error`` field (``usage`` vs ``config``) is what disambiguates them for agents.
    """
    if not _json_mode_from_argv(argv):
        if not _is_no_args_help_error(exc):  # NoArgsIsHelp already printed the help as a side effect
            exc.show()
        return exc.exit_code
    error = "usage" if isinstance(exc, click.exceptions.UsageError) else "error"
    ctx = getattr(exc, "ctx", None)
    path = ctx.command_path if ctx else "pmox"
    if _is_no_args_help_error(exc):
        message = f"Missing command for `{path}`."  # exc.message would be the whole help screen
    else:
        message = exc.format_message()
    payload = {
        "ok": False,
        "error": error,
        "message": message,
        "hint": f"Run `{path} --help` for usage, or `pmox guide` for the full agent guide.",
    }
    print(json.dumps(payload, indent=2))
    return exc.exit_code


def _dangling_group_path(argv: List[str]) -> Optional[str]:
    """Display path ("pmox vm") if ``argv`` names a command group with no subcommand, else None.

    Typer's rich help prints to stdout as a side effect the moment a
    ``NoArgsIsHelpError`` is constructed, which would corrupt JSON output — so in
    JSON mode the dangling-group case must be caught *before* invoking the app.
    """
    i = 0
    while i < len(argv):
        name = argv[i].split("=", 1)[0]
        if name == "--version":
            return None  # eager option: parsing short-circuits before any subcommand is needed
        if name in _GLOBAL_BOOL_FLAGS:
            i += 1
        elif name in _GLOBAL_VALUE_FLAGS:
            i += 1 if "=" in argv[i] else 2
        else:
            break
    if i > len(argv):
        return None  # value flag missing its value: let click report it
    try:
        cmd = typer.main.get_command(app)
    except Exception:  # noqa: BLE001 - app may be monkeypatched/unusual; let click parse
        return None
    # Group-ness is structural: groups carry a non-empty ``commands`` dict, leaves don't.
    # (typer's vendored click has no Group class to isinstance-check against.)
    path = ["pmox"]
    for tok in argv[i:]:
        if tok.startswith("-"):
            return None
        sub = (getattr(cmd, "commands", None) or {}).get(tok)
        if sub is None:
            return None
        cmd = sub
        path.append(tok)
    return " ".join(path) if getattr(cmd, "commands", None) else None


def main():
    argv = hoist_global_flags(sys.argv[1:])
    if _json_mode_from_argv(argv):
        dangling = _dangling_group_path(argv)
        if dangling is not None:
            print(json.dumps({
                "ok": False,
                "error": "usage",
                "message": f"Missing command for `{dangling}`.",
                "hint": f"Run `{dangling} --help` for usage, or `pmox guide` for the full agent guide.",
            }, indent=2))
            raise SystemExit(2)
    try:
        rv = app(args=argv, standalone_mode=False)
    except click.exceptions.Abort:
        err_console.print("Aborted.")
        raise SystemExit(1)
    except click.exceptions.ClickException as exc:
        raise SystemExit(_handle_parse_error(argv, exc))
    raise SystemExit(rv if isinstance(rv, int) and not isinstance(rv, bool) else 0)


if __name__ == "__main__":  # pragma: no cover
    main()
