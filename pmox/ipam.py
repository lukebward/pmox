"""Token-only IPv4 allocation: the cluster's own static ipconfig is the ledger.

No local state file — to find a free address we read every guest's static
``ipconfigN`` across the cluster. The pool MUST sit outside the DHCP scope, since
we cannot see DHCP leases or non-Proxmox hosts.
"""

from __future__ import annotations

import ipaddress


def static_ips_from_ipconfig(config) -> list:
    """Static addresses declared in a guest's cloud-init ``ipconfigN`` keys."""
    ips: list = []
    for key, value in config.items():
        if not str(key).startswith("ipconfig"):
            continue
        fields = dict(p.split("=", 1) for p in str(value).split(",") if "=" in p)
        for fkey in ("ip", "ip6"):
            cidr = fields.get(fkey)
            if not cidr or cidr in ("dhcp", "auto", "manual"):
                continue
            ips.append(cidr.split("/", 1)[0])
    return ips


def parse_pool_range(pool: str):
    """Split ``'START-END'`` into (start, end) address strings."""
    start, sep, end = pool.partition("-")
    if not sep:
        raise ValueError(f"pool must be 'START-END' (got {pool!r}).")
    return start.strip(), end.strip()


def allocate_ip(client, *, cidr: str, gateway: str, pool: str) -> str:
    """Return the lowest free address in ``pool`` as ``'<ip>/<prefixlen>'``.

    Used addresses = every guest's static ipconfigN across the cluster + the
    gateway. Raises ``RuntimeError`` if the pool is exhausted.
    """
    network = ipaddress.ip_network(cidr, strict=False)
    start_s, end_s = parse_pool_range(pool)
    start = ipaddress.ip_address(start_s)
    end = ipaddress.ip_address(end_s)

    used = {gateway}
    for row in client.cluster_resources(type="vm"):
        cfg = client.guest_config(row.get("node"), row.get("type"), row.get("vmid"))
        used.update(static_ips_from_ipconfig(cfg))

    candidate = start
    while candidate <= end:
        if str(candidate) not in used:
            return f"{candidate}/{network.prefixlen}"
        candidate += 1
    raise RuntimeError(f"IP pool {pool} is exhausted; every address is in use.")
