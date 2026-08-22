"""Read-side compositions over :class:`pmox.client.ProxmoxClient`.

These functions assemble several single-endpoint client calls into one
consolidated view. They live here (not in ``client.py``) so the client stays a
thin one-endpoint-per-method wrapper.
"""

from __future__ import annotations

import re
from typing import Optional

from . import arp, guestops
from .catalog import CPU_PRESSURE, MEM_PRESSURE, STORAGE_PRESSURE
from .errors import NotFoundError

_RECENT_TASK_LIMIT = 50


def guest_not_found(vmid) -> NotFoundError:
    """The standard, actionable error for a vmid that isn't in /cluster/resources."""
    return NotFoundError(
        f"Guest {vmid} not found in the cluster. Check `pmox vm list` / `pmox ct list`; "
        f"if it was created moments ago, retry shortly — or pass --node explicitly."
    )


def locate_guest_checked(client, kind: str, vmid) -> Optional[dict]:
    """Cluster-resources row for ``vmid`` (or None), validated against ``kind``.

    Raises ``LookupError`` with the corrective command when the guest exists but
    is the other kind (e.g. ``pmox vm status`` against an LXC container) — the
    raw API error would otherwise be a misleading 500 about a missing config file.
    """
    row = client.locate_guest(vmid)
    if row is None:
        return None
    actual = row.get("type")
    if kind and actual and actual != kind:
        right = "ct" if actual == "lxc" else "vm"
        wrong = "vm" if kind == "qemu" else "ct"
        what = "an LXC container" if actual == "lxc" else "a QEMU VM"
        name = f" ({row['name']})" if row.get("name") else ""
        raise LookupError(
            f"Guest {vmid}{name} is {what} — use `pmox {right} ...` instead of `pmox {wrong} ...`."
        )
    return row


def find_agent_template(client, image: str, node: Optional[str] = None) -> Optional[dict]:
    """The cluster-resources row of the agent template built from ``image``.

    A match is a QEMU template carrying both the ``pmox-agent`` tag and the
    image marker tag (see :func:`pmox.guestops.agent_tags`), optionally pinned
    to ``node`` — clones happen on the template's node, so a template elsewhere
    in the cluster doesn't count.
    """
    wanted = {guestops.AGENT_TAG, guestops.image_tag(image)}
    for row in client.cluster_resources(type="vm"):
        if row.get("type") != "qemu" or not row.get("template"):
            continue
        if node and row.get("node") != node:
            continue
        tags = {t for t in re.split(r"[;,]", str(row.get("tags") or "")) if t}
        if wanted <= tags:
            return row
    return None


def describe_guest(client, kind: str, vmid: int, node: Optional[str] = None) -> dict:
    """Consolidate a guest's status, config, owning node, snapshots and recent tasks."""
    if node is None:
        row = locate_guest_checked(client, kind, vmid)
        if row is None or not row.get("node"):
            raise guest_not_found(vmid)
        node = row["node"]
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
        "network": _safe_ip_addresses(client, kind, vmid, node),
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


def _parse_ipconfig_interfaces(config) -> list:
    """Static IPs declared in cloud-init ipconfigN keys (no guest agent needed)."""
    interfaces = []
    for key in sorted(config):
        if not key.startswith("ipconfig"):
            continue
        fields = dict(p.split("=", 1) for p in str(config[key]).split(",") if "=" in p)
        addresses = []
        for family, fkey in (("ipv4", "ip"), ("ipv6", "ip6")):
            cidr = fields.get(fkey)
            if not cidr or cidr in ("dhcp", "auto", "manual"):
                continue
            address, _, prefix = cidr.partition("/")
            addresses.append(
                {"family": family, "address": address,
                 "prefix": int(prefix) if prefix.isdigit() else None,
                 "scope": _addr_scope(family, address)}
            )
        if not addresses:
            continue
        idx = key[len("ipconfig"):]
        mac = str(config.get("net" + idx, "")).split(",", 1)[0].partition("=")[2]
        interfaces.append({"name": "net" + idx, "mac": mac if ":" in mac else None,
                           "addresses": addresses})
    return interfaces


def _primary_ipv4(interfaces):
    """First global IPv4 across interfaces, in order (or None)."""
    for iface in interfaces:
        for a in iface["addresses"]:
            if a["family"] == "ipv4" and a["scope"] == "global":
                return a["address"]
    return None


def _safe_ip_addresses(client, kind, vmid, node) -> dict:
    """guest_ip_addresses wrapped for embedding in describe: never raises."""
    try:
        return {"available": True, **guest_ip_addresses(client, kind, vmid, node=node)}
    except Exception as exc:  # noqa: BLE001 - describe must not break if the agent is down
        return {"available": False, "reason": str(exc)}


def _arp_interfaces(config: dict, scan: Optional[arp.ScanConfig], base_error: str, cause: Exception) -> list:
    """Last-resort same-LAN ARP discovery; raises ``RuntimeError`` when it can't help.

    The error message gains a note only when a sweep actually ran and missed —
    a skipped scan (no ScanConfig, no MACs, no candidate subnet) keeps the
    original message so it reflects what was tried.
    """
    pairs = arp.extract_macs(config) if scan is not None else []
    if not pairs:
        raise RuntimeError(base_error) from cause
    matches, swept = arp.find_ips_by_mac([mac for _, mac in pairs], scan)
    if not matches:
        if swept:
            base_error += (
                " A same-LAN ARP scan also found no address for MAC(s) "
                + ", ".join(mac for _, mac in pairs)
                + " (the scan only works when pmox runs on the same network as the guest)."
            )
        raise RuntimeError(base_error) from cause
    return [
        {
            "name": key,
            "mac": mac,
            "addresses": [
                {"family": "ipv4", "address": matches[arp.normalize_mac(mac)],
                 "prefix": None, "scope": "global"}
            ],
        }
        for key, mac in pairs
        if arp.normalize_mac(mac) in matches
    ]


def guest_ip_addresses(client, kind: str, vmid: int, node: Optional[str] = None,
                       scan: Optional[arp.ScanConfig] = None) -> dict:
    """Live network interfaces + IPs for a guest, normalized across qemu/lxc.

    VM sources, in order: the QEMU guest agent; static cloud-init ``ipconfigN``
    (``source: "config"``); and — when ``scan`` is given — a same-LAN ARP
    lookup by the guest's MAC (``source: "arp"``, IPv4 only, no prefix).
    Raises ``LookupError`` if the guest can't be located, or ``RuntimeError``
    with an actionable message when no source yields data.
    """
    row = locate_guest_checked(client, kind, vmid)
    node = node or (row.get("node") if row else None)
    if not node:
        raise guest_not_found(vmid)
    name = row.get("name") if row else None

    if kind == "qemu":
        source = "guest-agent"
        try:
            payload = client.agent_network_interfaces(node, vmid)
        except Exception as agent_exc:  # noqa: BLE001 - agent down -> static config -> ARP
            config = client.guest_config(node, kind, vmid)
            interfaces = _parse_ipconfig_interfaces(config)
            if interfaces:
                source = "config"
            else:
                base_error = (
                    f"Could not read network interfaces for VM {vmid}: {agent_exc}. "
                    f"Ensure qemu-guest-agent is installed and running in the guest and "
                    f"'agent: 1' is set (pmox vm set {vmid} -o agent=1 --dangerous)."
                )
                interfaces = _arp_interfaces(config, scan, base_error, agent_exc)
                source = "arp"
        else:
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


def summarize_health(client) -> dict:
    """One-shot cluster triage: quorum, per-node CPU/mem pressure, storage near full,
    running/stopped guest counts, and a structured list of actionable issues (lost
    quorum, offline nodes, unavailable storage, repeated task failures), with a flat
    list of warnings."""
    status = client.cluster_status()
    cluster = next((e for e in status if e.get("type") == "cluster"), {})
    node_entries = [e for e in status if e.get("type") == "node"]
    warnings: list = []
    issues: list = []

    if not cluster.get("quorate", 0):
        issues.append({
            "code": "quorum_lost", "severity": "critical", "node": None,
            "message": "cluster has lost quorum",
        })

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
        if n.get("status") != "online":
            issues.append({
                "code": "node_offline", "severity": "critical", "node": n.get("node"),
                "message": f"node {n.get('node')} is {n.get('status') or 'unknown'}",
            })
        else:
            issues.extend(_task_failure_issues(client, n["node"]))
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
        status_ = s.get("status")
        if status_ and status_ not in ("active", "available"):
            issues.append({
                "code": "storage_unavailable", "severity": "warning", "node": s.get("node"),
                "message": f"storage {s.get('storage')} on {s.get('node')} is {status_}",
            })
        storage.append({"storage": s.get("storage"), "node": s.get("node"), "used_pct": used, "flags": flags})

    vms = client.cluster_resources(type="vm")
    template_count = sum(1 for v in vms if v.get("template"))
    guests = [v for v in vms if not v.get("template")]
    running = sum(1 for v in guests if v.get("status") == "running")
    warnings.extend(i["message"] for i in issues)
    return {
        "quorate": bool(cluster.get("quorate", 0)),
        "nodes_online": sum(1 for e in node_entries if e.get("online")),
        "nodes_total": len(node_entries),
        "nodes": nodes,
        "storage": storage,
        "guests": {"running": running, "stopped": len(guests) - running, "templates": template_count},
        "issues": issues,
        "warnings": warnings,
    }
