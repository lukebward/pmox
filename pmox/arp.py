"""Same-LAN ARP discovery: find a guest's IPv4 by its MAC.

Token-only pmox can't ask Proxmox for a DHCP guest's address when the guest
agent isn't installed, but the guest config gives us the MAC, and on the same
L2 network the OS neighbor (ARP) table maps MAC -> IP. One empty UDP datagram
per candidate address forces the OS to ARP-resolve hosts that haven't talked
recently (unprivileged, and works through guest firewalls because ARP happens
below ICMP/UDP filtering).

Subprocess/socket edges are module-level functions so tests monkeypatch them;
the parsing core is pure.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# macOS prints MAC octets without leading zeros, so groups are 1-2 hex digits.
_MAC_RE = re.compile(r"\b[0-9A-Fa-f]{1,2}(?:[:-][0-9A-Fa-f]{1,2}){5}\b")
_IPV4_RE = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
_NET_KEY_RE = re.compile(r"net(\d+)")

_MAX_SWEEP_PREFIX = 22  # never sweep anything larger than a /22 (1024 hosts)
_SETTLE_SECONDS = 1.0   # ARP-reply settle time after the nudge sweep
_NUDGE_PORT = 9         # UDP discard port


@dataclass(frozen=True)
class ScanConfig:
    """What discovery needs from settings: the declared VM subnet (optional)
    and the Proxmox endpoint, used as a route hint to pick the local interface."""

    cidr: Optional[str] = None
    host: Optional[str] = None
    port: int = 8006


def normalize_mac(mac: str) -> str:
    """Comparable form: lowercase hex, separators stripped, octets zero-padded."""
    parts = re.split(r"[:-]", mac.strip().lower())
    if len(parts) == 6:
        return "".join(p.zfill(2) for p in parts)
    return "".join(parts)


def extract_macs(config: dict) -> List[Tuple[str, str]]:
    """(net-key, MAC) pairs from a guest config, in numeric netN order.

    Proxmox net values are comma-separated key=value lists where the MAC is the
    value of the model key (``virtio=BC:...``, ``e1000=...``; ``hwaddr=`` for
    LXC) — any MAC-shaped value counts, so model names need no enumeration.
    """
    keys = sorted(
        (k for k in config if _NET_KEY_RE.fullmatch(str(k))),
        key=lambda k: int(_NET_KEY_RE.fullmatch(str(k)).group(1)),
    )
    pairs: List[Tuple[str, str]] = []
    for key in keys:
        match = _MAC_RE.search(str(config[key]))
        if match:
            pairs.append((key, match.group(0)))
    return pairs


def parse_neighbors(text: str) -> Dict[str, str]:
    """Normalized MAC -> IPv4 from neighbor-table output.

    One parser covers Windows/macOS ``arp -a`` and Linux ``ip neigh``: per
    line, the first IPv4 literal plus the first MAC-shaped token. Lines
    lacking either (headers, FAILED/incomplete entries) are ignored. The first
    occurrence of a MAC wins.
    """
    table: Dict[str, str] = {}
    for line in text.splitlines():
        ip_match = _IPV4_RE.search(line)
        mac_match = _MAC_RE.search(line)
        if ip_match and mac_match:
            table.setdefault(normalize_mac(mac_match.group(0)), ip_match.group(1))
    return table
