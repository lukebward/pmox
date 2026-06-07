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


def _addr_scope(family: str, address: str) -> str:
    """Classify an IP as 'loopback', 'link', or 'global'."""
    addr = (address or "").lower()
    if family == "ipv4":
        if addr.startswith("127."):
            return "loopback"
        if addr.startswith("169.254."):
            return "link"
        return "global"
    if addr == "::1":
        return "loopback"
    if addr.startswith("fe80"):
        return "link"
    return "global"


def _locate_guest(client, vmid):
    """Cluster-resource row for a vmid (carries node + name), or None."""
    target = int(vmid)
    for r in client.cluster_resources(type="vm"):
        if int(r.get("vmid", -1)) == target:
            return r
    return None


def _parse_qemu_interfaces(payload) -> list:
    """Normalize the QEMU guest-agent network-get-interfaces payload."""
    result = payload.get("result", payload) if isinstance(payload, dict) else payload
    interfaces = []
    for iface in result or []:
        addresses = []
        for a in iface.get("ip-addresses") or []:
            family = a.get("ip-address-type")
            address = a.get("ip-address")
            if not family or not address:
                continue
            addresses.append(
                {"family": family, "address": address, "prefix": a.get("prefix"),
                 "scope": _addr_scope(family, address)}
            )
        interfaces.append({"name": iface.get("name"), "mac": iface.get("hardware-address"), "addresses": addresses})
    return interfaces


def _parse_lxc_interfaces(rows) -> list:
    """Normalize the LXC /interfaces payload (inet/inet6 CIDR strings)."""
    interfaces = []
    for iface in rows or []:
        addresses = []
        for family, key in (("ipv4", "inet"), ("ipv6", "inet6")):
            raw = iface.get(key)
            if not raw:
                continue
            for cidr in str(raw).split():
                address, _, prefix = cidr.partition("/")
                addresses.append(
                    {"family": family, "address": address,
                     "prefix": int(prefix) if prefix.isdigit() else None,
                     "scope": _addr_scope(family, address)}
                )
        interfaces.append({"name": iface.get("name"), "mac": iface.get("hwaddr"), "addresses": addresses})
    return interfaces


def _primary_ipv4(interfaces):
    """First global IPv4 across interfaces, in order (or None)."""
    for iface in interfaces:
        for a in iface["addresses"]:
            if a["family"] == "ipv4" and a["scope"] == "global":
                return a["address"]
    return None


def guest_ip_addresses(client, kind: str, vmid: int, node: Optional[str] = None) -> dict:
    """Live network interfaces + IPs for a guest, normalized across qemu/lxc.

    Raises ``LookupError`` if the guest can't be located, or ``RuntimeError`` with
    an actionable message if the agent/interfaces endpoint can't be read.
    """
    row = _locate_guest(client, vmid)
    node = node or (row.get("node") if row else None)
    if not node:
        raise LookupError(f"Could not locate guest {vmid} in the cluster.")
    name = row.get("name") if row else None

    if kind == "qemu":
        source = "guest-agent"
        try:
            payload = client.agent_network_interfaces(node, vmid)
        except Exception as exc:  # noqa: BLE001 - any agent failure -> actionable message
            raise RuntimeError(
                f"Could not read network interfaces for VM {vmid}: {exc}. "
                f"Ensure qemu-guest-agent is installed and running in the guest and "
                f"'agent: 1' is set (pmox vm set {vmid} -o agent=1 --dangerous)."
            ) from exc
        interfaces = _parse_qemu_interfaces(payload)
    else:
        source = "lxc-interfaces"
        try:
            rows = client.lxc_interfaces(node, vmid)
        except Exception as exc:  # noqa: BLE001 - any failure -> actionable message
            raise RuntimeError(
                f"Could not read network interfaces for CT {vmid}: {exc}. "
                f"The container may be stopped."
            ) from exc
        interfaces = _parse_lxc_interfaces(rows)

    return {
        "vmid": vmid, "node": node, "kind": kind, "name": name, "source": source,
        "primary": _primary_ipv4(interfaces), "interfaces": interfaces,
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
