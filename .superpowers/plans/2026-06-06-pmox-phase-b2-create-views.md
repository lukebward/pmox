# pmox Phase B2: Ergonomic Create + Read Views — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ergonomic VM creation (`vm new` with sizing profiles, auto-VMID, sane defaults) and read-side composition commands (`vm/ct describe`, `health`) to the `pmox` CLI.

**Architecture:** Builds directly on Phase B1. A new pure-data module `catalog.py` holds sizing profiles and health thresholds. A new `views.py` holds read-side compositions (`describe_guest`, `summarize_health`) over the existing `ProxmoxClient` — keeping `client.py` single-endpoint. `vm new` is added to the guest factory **for QEMU only** (a container needs an ostemplate, so `ct new` is Phase C). `describe` is added for both kinds; `health` is a new top-level command. All creation routes through B1's `_execute` (so it gets `--dry-run`/`--wait`/gates for free). **No new `client.py` methods are needed** — everything composes existing endpoints plus B1's `cluster_nextid`.

**Tech Stack:** Python 3.11+, Typer, proxmoxer (mocked in tests), Rich, pytest + pytest-cov.

**Source of truth:** `.superpowers/specs/2026-06-06-pmox-agent-native-design.md` (§5.2, §5.3, §3 module layout).

---

## Conventions for every task

- **Focused test runs** (red/green) disable coverage: `.venv\Scripts\python.exe -m pytest tests/test_x.py::test_name -v --no-cov`
- **Before every commit**, run the full suite (enforces 100%): `.venv\Scripts\python.exe -m pytest` — must end with `Required test coverage of 100% reached`.
- Work in place in `C:\Users\Luke\Workspace\pmox` on branch `feat/agent-native-commands`. No worktree.
- **Help text for commands that vary by guest kind MUST be an f-string in the `@group.command(..., help=f"...")` decorator** (NOT a `{label}` docstring — a plain docstring won't interpolate and renders a literal `{label}`). This matches the power-command pattern and B1's `set`/`resize`/`rename`/`tag`.
- Follow existing `cli.py`/`client.py` style.

---

## Task 1: `catalog.py` — sizing profiles + health thresholds

**Files:**
- Create: `pmox/catalog.py`
- Test: `tests/test_catalog.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_catalog.py`:

```python
import pytest

from pmox import catalog


def test_size_profiles_values():
    assert catalog.SIZE_PROFILES["small"] == {"cores": 1, "memory": 1024}
    assert catalog.SIZE_PROFILES["medium"] == {"cores": 2, "memory": 4096}
    assert catalog.SIZE_PROFILES["large"] == {"cores": 4, "memory": 8192}


def test_size_params_returns_copy():
    p = catalog.size_params("small")
    assert p == {"cores": 1, "memory": 1024}
    p["cores"] = 99
    assert catalog.SIZE_PROFILES["small"]["cores"] == 1  # not mutated


def test_size_params_unknown_raises():
    with pytest.raises(ValueError):
        catalog.size_params("enormous")


def test_pressure_thresholds_are_fractions():
    assert 0 < catalog.CPU_PRESSURE <= 1
    assert 0 < catalog.MEM_PRESSURE <= 1
    assert 0 < catalog.STORAGE_PRESSURE <= 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_catalog.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'pmox.catalog'`

- [ ] **Step 3: Implement**

Create `pmox/catalog.py`:

```python
"""Static reference data for pmox: VM sizing profiles and cluster-health thresholds.

Kept separate from the API client and CLI so the magic numbers live in one
obvious place and can be unit-tested in isolation.
"""

from __future__ import annotations

# VM sizing profiles: friendly name -> create parameters.
SIZE_PROFILES = {
    "small": {"cores": 1, "memory": 1024},
    "medium": {"cores": 2, "memory": 4096},
    "large": {"cores": 4, "memory": 8192},
}

# Fractions (0-1) above which `pmox health` flags a resource as under pressure.
CPU_PRESSURE = 0.85
MEM_PRESSURE = 0.85
STORAGE_PRESSURE = 0.85


def size_params(size: str) -> dict:
    """Return a fresh copy of the create parameters for a sizing profile."""
    if size not in SIZE_PROFILES:
        raise ValueError(
            f"unknown size {size!r}; choose from: {', '.join(SIZE_PROFILES)}."
        )
    return dict(SIZE_PROFILES[size])
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_catalog.py -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/catalog.py tests/test_catalog.py
git commit -m "Add catalog module: VM sizing profiles and health thresholds"
```

---

## Task 2: `views.describe_guest`

**Files:**
- Create: `pmox/views.py`
- Test: `tests/test_views.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_views.py`:

```python
from unittest.mock import MagicMock

import pytest

from pmox import views


def _client():
    c = MagicMock()
    c.resolve_node.return_value = "pve1"
    c.guest_status.return_value = {"status": "running"}
    c.guest_config.return_value = {"cores": 2}
    c.list_snapshots.return_value = [{"name": "pre"}]
    c.list_tasks.return_value = [
        {"id": "100", "type": "qmstart"},
        {"id": "999", "type": "qmstart"},
    ]
    return c


def test_describe_guest_composes_and_filters_tasks():
    c = _client()
    out = views.describe_guest(c, "qemu", 100)
    assert out["vmid"] == 100
    assert out["node"] == "pve1"
    assert out["kind"] == "qemu"
    assert out["status"] == {"status": "running"}
    assert out["config"] == {"cores": 2}
    assert out["snapshots"] == [{"name": "pre"}]
    # only this guest's tasks (id == vmid)
    assert out["recent_tasks"] == [{"id": "100", "type": "qmstart"}]
    c.resolve_node.assert_called_once_with(100)


def test_describe_guest_uses_explicit_node_without_resolving():
    c = _client()
    views.describe_guest(c, "qemu", 100, node="pve2")
    c.resolve_node.assert_not_called()
    c.guest_status.assert_called_once_with("pve2", "qemu", 100)


def test_describe_guest_not_found_raises():
    c = _client()
    c.resolve_node.return_value = None
    with pytest.raises(LookupError):
        views.describe_guest(c, "qemu", 999)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_views.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'pmox.views'`

- [ ] **Step 3: Implement**

Create `pmox/views.py`:

```python
"""Read-side compositions over :class:`pmox.client.ProxmoxClient`.

These functions assemble several single-endpoint client calls into one
consolidated view. They live here (not in ``client.py``) so the client stays a
thin one-endpoint-per-method wrapper.
"""

from __future__ import annotations

from typing import Optional

_RECENT_TASK_LIMIT = 50


def describe_guest(client, kind: str, vmid: int, node: Optional[str] = None) -> dict:
    """Consolidate a guest's status, config, owning node, snapshots and recent tasks."""
    node = node or client.resolve_node(vmid)
    if not node:
        raise LookupError(f"Could not locate guest {vmid} in the cluster.")
    tasks = [
        t
        for t in client.list_tasks(node, limit=_RECENT_TASK_LIMIT)
        if str(t.get("id")) == str(vmid)
    ]
    return {
        "vmid": vmid,
        "node": node,
        "kind": kind,
        "status": client.guest_status(node, kind, vmid),
        "config": client.guest_config(node, kind, vmid),
        "snapshots": client.list_snapshots(node, kind, vmid),
        "recent_tasks": tasks,
    }
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_views.py -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/views.py tests/test_views.py
git commit -m "Add views.describe_guest read composition"
```

---

## Task 3: `vm/ct describe` command

**Files:**
- Modify: `pmox/cli.py` (add `import` of `views` + `build_kv_table`; add `describe` to `build_guest_app`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -k describe -v --no-cov`
Expected: FAIL with `No such command 'describe'`

- [ ] **Step 3: Implement**

In `pmox/cli.py`, add `views` to the package imports (near `from . import __version__`):

```python
from . import __version__, views
```

Ensure `build_kv_table` is imported from `.output` (extend the existing `from .output import (...)` block to include `build_kv_table`).

In `build_guest_app`, after `_config`:

```python
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
                emit(data["snapshots"], columns=SNAPSHOT_COLUMNS, json_output=False, title="snapshots")
                emit(data["recent_tasks"], columns=TASK_COLUMNS, json_output=False, title="recent tasks")
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -k describe -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Add vm/ct describe command"
```

---

## Task 4: `views.summarize_health`

**Files:**
- Modify: `pmox/views.py` (add `summarize_health`; import thresholds from `catalog`)
- Test: `tests/test_views.py`

- [ ] **Step 1: Write the failing tests**

```python
def _health_client():
    c = MagicMock()
    c.cluster_status.return_value = [
        {"type": "cluster", "quorate": 1},
        {"type": "node", "name": "pve1", "online": 1},
        {"type": "node", "name": "pve2", "online": 1},
    ]
    c.list_nodes.return_value = [
        {"node": "pve1", "status": "online", "cpu": 0.10, "mem": 2, "maxmem": 10},
        {"node": "pve2", "status": "online", "cpu": 0.90, "mem": 9, "maxmem": 10},
    ]

    def _resources(type=None):
        if type == "storage":
            return [
                {"storage": "local", "node": "pve1", "disk": 1, "maxdisk": 10},
                {"storage": "full-store", "node": "pve2", "disk": 95, "maxdisk": 100},
            ]
        return [
            {"vmid": 100, "status": "running"},
            {"vmid": 101, "status": "stopped"},
        ]

    c.cluster_resources.side_effect = _resources
    return c


def test_summarize_health_flags_pressure():
    out = views.summarize_health(_health_client())
    assert out["quorate"] is True
    assert out["nodes_online"] == 2 and out["nodes_total"] == 2
    pve2 = next(n for n in out["nodes"] if n["node"] == "pve2")
    assert "cpu-high" in pve2["flags"] and "mem-high" in pve2["flags"]
    pve1 = next(n for n in out["nodes"] if n["node"] == "pve1")
    assert pve1["flags"] == []
    full = next(s for s in out["storage"] if s["storage"] == "full-store")
    assert "storage-full" in full["flags"]
    assert out["guests"] == {"running": 1, "stopped": 1}
    assert any("pve2" in w for w in out["warnings"])


def test_summarize_health_handles_zero_maxima_and_no_quorum():
    c = MagicMock()
    c.cluster_status.return_value = [{"type": "cluster", "quorate": 0}]
    c.list_nodes.return_value = [{"node": "pve1", "status": "online", "cpu": 0, "mem": 0, "maxmem": 0}]
    c.cluster_resources.side_effect = lambda type=None: [] if type == "storage" else []
    out = views.summarize_health(c)
    assert out["quorate"] is False
    assert out["nodes"][0]["mem_pct"] == 0.0
    assert out["guests"] == {"running": 0, "stopped": 0}
    assert out["warnings"] == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_views.py -k summarize_health -v --no-cov`
Expected: FAIL with `AttributeError: module 'pmox.views' has no attribute 'summarize_health'`

- [ ] **Step 3: Implement**

Add to the top of `pmox/views.py` (after the existing imports):

```python
from .catalog import CPU_PRESSURE, MEM_PRESSURE, STORAGE_PRESSURE
```

Add the function:

```python
def summarize_health(client) -> dict:
    """One-shot cluster triage: quorum, per-node CPU/mem pressure, storage near full,
    and running/stopped guest counts, with a flat list of warnings."""
    status = client.cluster_status()
    cluster = next((e for e in status if e.get("type") == "cluster"), {})
    node_entries = [e for e in status if e.get("type") == "node"]
    warnings: list = []

    nodes = []
    for n in client.list_nodes():
        cpu = float(n.get("cpu") or 0)
        maxmem = float(n.get("maxmem") or 0)
        mem = (float(n.get("mem") or 0) / maxmem) if maxmem else 0.0
        flags = []
        if cpu >= CPU_PRESSURE:
            flags.append("cpu-high")
            warnings.append(f"node {n.get('node')} cpu {cpu:.0%}")
        if mem >= MEM_PRESSURE:
            flags.append("mem-high")
            warnings.append(f"node {n.get('node')} mem {mem:.0%}")
        nodes.append(
            {"node": n.get("node"), "status": n.get("status"), "cpu_pct": cpu, "mem_pct": mem, "flags": flags}
        )

    storage = []
    for s in client.cluster_resources(type="storage"):
        maxdisk = float(s.get("maxdisk") or 0)
        used = (float(s.get("disk") or 0) / maxdisk) if maxdisk else 0.0
        flags = []
        if used >= STORAGE_PRESSURE:
            flags.append("storage-full")
            warnings.append(f"storage {s.get('storage')} {used:.0%}")
        storage.append({"storage": s.get("storage"), "node": s.get("node"), "used_pct": used, "flags": flags})

    vms = client.cluster_resources(type="vm")
    running = sum(1 for v in vms if v.get("status") == "running")
    return {
        "quorate": bool(cluster.get("quorate", 0)),
        "nodes_online": sum(1 for e in node_entries if e.get("online")),
        "nodes_total": len(node_entries),
        "nodes": nodes,
        "storage": storage,
        "guests": {"running": running, "stopped": len(vms) - running},
        "warnings": warnings,
    }
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_views.py -k summarize_health -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/views.py tests/test_views.py
git commit -m "Add views.summarize_health cluster triage composition"
```

---

## Task 5: `health` top-level command

**Files:**
- Modify: `pmox/cli.py` (add a top-level `health` command; register nothing new — it's `@app.command`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -k health -v --no-cov`
Expected: FAIL with `No such command 'health'`

- [ ] **Step 3: Implement**

In `pmox/cli.py`, add a top-level command near `server_version` (after the `version` command, before the `nodes` app section). Columns are defined inline since they are health-specific:

```python
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
```

Note: `percent` accepts a fraction (0-1) and formats as a percentage — `cpu_pct`/`mem_pct`/`used_pct` are already fractions, so pass them directly (no `of=`).

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -k health -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Add health top-level cluster-triage command"
```

---

## Task 6: `vm new` ergonomic create (QEMU only)

**Files:**
- Modify: `pmox/cli.py` (add `catalog` import; add `_single_node_or_die` helper; add `new` command to `build_guest_app` guarded by `kind == "qemu"`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -k "vm_new or ct_has_no_new" -v --no-cov`
Expected: FAIL with `No such command 'new'`

- [ ] **Step 3: Implement**

In `pmox/cli.py`, add `catalog` to the package imports:

```python
from . import __version__, catalog, views
```

Add the helper near `_resolve_node_or_die`:

```python
def _single_node_or_die(client: ProxmoxClient) -> str:
    """Return the only node's name, or error if the cluster has 0 or >1 nodes."""
    nodes = client.list_nodes()
    if len(nodes) == 1:
        return nodes[0]["node"]
    raise ValueError("Cluster has multiple nodes; pass --node to choose where to create.")
```

In `build_guest_app`, add the `new` command **only for QEMU** (place it after `_create`):

```python
    if kind == "qemu":

        @group.command("new", help="Create a VM with a sizing profile and sane defaults.")
        def _new(
            ctx: typer.Context,
            name: Optional[str] = typer.Argument(None, help="VM name (optional)."),
            size: str = typer.Option("small", "--size", help="Sizing profile: small | medium | large."),
            disk: Optional[int] = typer.Option(None, "--disk", help="Disk size in GiB (created on --storage)."),
            storage: str = typer.Option("local-lvm", "--storage", help="Storage for the disk."),
            node: Optional[str] = typer.Option(None, "--node", "-n", help="Node (auto-picked if the cluster has one node)."),
            vmid: Optional[int] = typer.Option(None, "--vmid", help="VMID (auto-assigned from the cluster if omitted)."),
            option: Optional[List[str]] = typer.Option(None, "--option", "-o", help="Extra create param key=value (repeatable)."),
        ):
            with error_boundary(ctx.obj.json):
                client = _get_client(ctx)
                target_node = node or _single_node_or_die(client)
                target_vmid = vmid if vmid is not None else int(client.cluster_nextid())
                params = catalog.size_params(size)
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
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -k "vm_new or ct_has_no_new" -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest`
Expected: all green, 100% coverage.

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Add vm new ergonomic create (profiles, auto-vmid, node auto-pick)"
```

---

## Self-review (completed during planning)

- **Spec coverage (§5.2, §5.3):** `catalog.SIZE_PROFILES` + `size_params` (T1) ✓; `vm new` profiles/auto-VMID/node-auto-pick/defaults/`--storage`/`--disk`/`-o`, QEMU-only (T6) ✓; `vm/ct describe` via `views.describe_guest` (T2, T3) ✓; `health` via `views.summarize_health` (T4, T5) ✓. The existing raw `vm create` is untouched (kept). **Deferred to C/A:** `ct new` (needs ostemplate), `--like`, `vm ip`.
- **No new client methods:** confirmed — `describe`/`health`/`new` compose existing endpoints + B1's `cluster_nextid`.
- **Name/type consistency:** `catalog.size_params`, `catalog.{CPU,MEM,STORAGE}_PRESSURE`, `views.describe_guest(client, kind, vmid, node=None)`, `views.summarize_health(client)`, `_single_node_or_die(client)`, `_execute(op="qemu.new", ...)` — used consistently.
- **Help-text discipline:** every kind-varying command (`describe`) uses `help=f"..."` in the decorator (not a `{label}` docstring), per the B1 lesson. `new` and `health` use static help strings (no `{label}`), which is fine.
- **Coverage discipline:** every task ships tests covering its own lines; `summarize_health`'s flag branches and zero-maxmem/maxdisk guards are explicitly tested; the `kind == "qemu"` guard's both arms execute at import (qemu→register, lxc→skip).
- **Routing:** `vm new` goes through `_execute`, inheriting `--dry-run`/`--wait`/gates (tested).
```
