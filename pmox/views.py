"""Read-side compositions over :class:`pmox.client.ProxmoxClient`.

These functions assemble several single-endpoint client calls into one
consolidated view. They live here (not in ``client.py``) so the client stays a
thin one-endpoint-per-method wrapper.
"""

from __future__ import annotations

from typing import Optional

from .catalog import CPU_PRESSURE, MEM_PRESSURE, STORAGE_PRESSURE

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
