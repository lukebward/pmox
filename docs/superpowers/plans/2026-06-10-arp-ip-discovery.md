# ARP-based IP Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `pmox vm ip` resolves agent-less DHCP VMs by matching the guest's MAC against the local ARP table (with a UDP nudge sweep on miss), as a third source after guest-agent and static config.

**Architecture:** New pure-ish module `pmox/arp.py` (parsing core pure; subprocess/socket edges are module functions tests monkeypatch). `views.guest_ip_addresses` gains a `scan: Optional[arp.ScanConfig]` parameter and tries ARP last, only when `scan` is provided and kind is qemu. The CLI builds `ScanConfig` from settings for the `vm ip` path only; `describe` never scans. Spec: `docs/superpowers/specs/2026-06-10-arp-ip-discovery-design.md`.

**Tech Stack:** Python 3.11+ stdlib only (`ipaddress`, `socket`, `subprocess`, `re`). pytest with the project's 100% coverage gate (`.venv\Scripts\python.exe -m pytest`; use `--no-cov` for subset runs).

---

### Task 1: arp.py parsing core (normalize, extract, parse)

**Files:**
- Create: `pmox/arp.py`
- Test: `tests/test_arp.py`

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_arp.py"""
import ipaddress

import pytest

from pmox import arp


# ---- normalize_mac ----

def test_normalize_mac_strips_and_lowers():
    assert arp.normalize_mac("BC:24:11:40:C4:A3") == "bc241140c4a3"
    assert arp.normalize_mac("bc-24-11-40-c4-a3") == "bc241140c4a3"

def test_normalize_mac_pads_single_digit_octets():
    # macOS arp -a prints octets without leading zeros
    assert arp.normalize_mac("a4:83:e7:4:b0:6f") == "a483e704b06f"

def test_normalize_mac_non_mac_passthrough():
    assert arp.normalize_mac(" ODD ") == "odd"


# ---- extract_macs ----

def test_extract_macs_qemu_models_in_net_order():
    config = {
        "net1": "e1000=AA:BB:CC:DD:EE:02,bridge=vmbr1",
        "net0": "virtio=BC:24:11:40:C4:A3,bridge=vmbr0,firewall=1",
        "cores": 2,
    }
    assert arp.extract_macs(config) == [
        ("net0", "BC:24:11:40:C4:A3"),
        ("net1", "AA:BB:CC:DD:EE:02"),
    ]

def test_extract_macs_numeric_order_not_lexical():
    config = {"net10": "virtio=AA:00:00:00:00:10", "net2": "virtio=AA:00:00:00:00:02"}
    assert [k for k, _ in arp.extract_macs(config)] == ["net2", "net10"]

def test_extract_macs_lxc_hwaddr():
    config = {"net0": "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:AA:BB:CC,ip=dhcp"}
    assert arp.extract_macs(config) == [("net0", "BC:24:11:AA:BB:CC")]

def test_extract_macs_none_found():
    assert arp.extract_macs({"net0": "bridge=vmbr0", "name": "x"}) == []


# ---- parse_neighbors ----

WINDOWS_ARP = """
Interface: 192.168.0.193 --- 0x10
  Internet Address      Physical Address      Type
  192.168.0.1           a8-6e-84-11-22-33     dynamic
  192.168.0.253         bc-24-11-40-c4-a3     dynamic
  224.0.0.22            01-00-5e-00-00-16     static
  255.255.255.255       ff-ff-ff-ff-ff-ff     static
"""

LINUX_IP_NEIGH = """\
192.168.0.1 dev eth0 lladdr a8:6e:84:11:22:33 REACHABLE
192.168.0.9 dev eth0  FAILED
192.168.0.253 dev eth0 lladdr bc:24:11:40:c4:a3 STALE
"""

MACOS_ARP = """\
? (192.168.0.1) at a8:6e:84:11:22:33 on en0 ifscope [ethernet]
? (192.168.0.77) at (incomplete) on en0 ifscope [ethernet]
? (192.168.0.253) at bc:24:11:40:c4:a3 on en0 ifscope [ethernet]
? (192.168.0.80) at a4:83:e7:4:b0:6f on en0 ifscope [ethernet]
"""

@pytest.mark.parametrize("text", [WINDOWS_ARP, LINUX_IP_NEIGH, MACOS_ARP])
def test_parse_neighbors_finds_the_guest(text):
    table = arp.parse_neighbors(text)
    assert table["bc241140c4a3"] == "192.168.0.253"

def test_parse_neighbors_skips_incomplete_and_headers():
    table = arp.parse_neighbors(MACOS_ARP)
    assert "192.168.0.77" not in table.values()
    assert arp.parse_neighbors(WINDOWS_ARP)  # header line contributed nothing fatal

def test_parse_neighbors_pads_macos_short_octets():
    assert arp.parse_neighbors(MACOS_ARP)["a483e704b06f"] == "192.168.0.80"

def test_parse_neighbors_first_occurrence_wins():
    text = "1.1.1.1 dev e lladdr aa:bb:cc:dd:ee:ff STALE\n2.2.2.2 dev e lladdr aa:bb:cc:dd:ee:ff REACHABLE\n"
    assert arp.parse_neighbors(text)["aabbccddeeff"] == "1.1.1.1"

def test_parse_neighbors_empty():
    assert arp.parse_neighbors("") == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_arp.py -q --no-cov`
Expected: collection error — `ModuleNotFoundError: No module named 'pmox.arp'`

- [ ] **Step 3: Minimal implementation**

```python
"""pmox/arp.py — same-LAN ARP discovery: find a guest's IPv4 by its MAC.

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
        m = _MAC_RE.search(str(config[key]))
        if m:
            pairs.append((key, m.group(0)))
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
        ip_m = _IPV4_RE.search(line)
        mac_m = _MAC_RE.search(line)
        if ip_m and mac_m:
            table.setdefault(normalize_mac(mac_m.group(0)), ip_m.group(1))
    return table
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_arp.py -q --no-cov`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add pmox/arp.py tests/test_arp.py
git commit -m "feat(arp): parsing core - MAC extraction, normalization, neighbor-table parser"
```

### Task 2: arp.py system edges (neighbor read, nudges, outbound IP)

**Files:**
- Modify: `pmox/arp.py` (append)
- Test: `tests/test_arp.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
# ---- read_neighbor_table ----

class _FakeProc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def test_read_neighbor_table_windows_uses_arp(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeProc(stdout="WIN")

    monkeypatch.setattr(arp.subprocess, "run", fake_run)
    assert arp.read_neighbor_table(platform="win32") == "WIN"
    assert calls == [["arp", "-a"]]


def test_read_neighbor_table_linux_prefers_ip_neigh(monkeypatch):
    monkeypatch.setattr(arp.subprocess, "run", lambda cmd, **k: _FakeProc(stdout="NEIGH"))
    assert arp.read_neighbor_table(platform="linux") == "NEIGH"


def test_read_neighbor_table_linux_falls_back_to_arp(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "ip":
            raise OSError("no ip binary")
        return _FakeProc(stdout="ARP")

    monkeypatch.setattr(arp.subprocess, "run", fake_run)
    assert arp.read_neighbor_table(platform="linux") == "ARP"


def test_read_neighbor_table_skips_failures_and_timeouts(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "ip":
            raise arp.subprocess.TimeoutExpired(cmd, 10)
        return _FakeProc(returncode=1, stdout="junk")

    monkeypatch.setattr(arp.subprocess, "run", fake_run)
    assert arp.read_neighbor_table(platform="linux") == ""


def test_read_neighbor_table_defaults_to_current_platform(monkeypatch):
    monkeypatch.setattr(arp.subprocess, "run", lambda cmd, **k: _FakeProc(stdout="X"))
    assert arp.read_neighbor_table() == "X"


# ---- _send_nudges ----

class _FakeSock:
    def __init__(self, fail_on=None, name=("192.168.0.193", 0)):
        self.sent = []
        self.fail_on = fail_on or set()
        self.name = name
        self.connected = None
        self.closed = False

    def sendto(self, data, addr):
        if addr[0] in self.fail_on:
            raise OSError("unreachable")
        self.sent.append(addr)

    def connect(self, addr):
        if addr[0] in self.fail_on:
            raise OSError("no route")
        self.connected = addr

    def getsockname(self):
        return self.name

    def close(self):
        self.closed = True


def test_send_nudges_fires_and_survives_errors(monkeypatch):
    sock = _FakeSock(fail_on={"10.0.0.2"})
    monkeypatch.setattr(arp.socket, "socket", lambda *a, **k: sock)
    arp._send_nudges(["10.0.0.1", "10.0.0.2", "10.0.0.3"])
    assert [a for a, _ in sock.sent] == ["10.0.0.1", "10.0.0.3"]
    assert all(port == arp._NUDGE_PORT for _, port in sock.sent)
    assert sock.closed


# ---- _outbound_ip ----

def test_outbound_ip_routes_toward_proxmox_host(monkeypatch):
    sock = _FakeSock(name=("192.168.0.193", 50000))
    monkeypatch.setattr(arp.socket, "socket", lambda *a, **k: sock)
    assert arp._outbound_ip("pve.local", 443) == "192.168.0.193"
    assert sock.connected == ("pve.local", 443)


def test_outbound_ip_falls_back_to_public_route(monkeypatch):
    socks = []

    def make_sock(*a, **k):
        s = _FakeSock(fail_on={"unresolvable.host"})
        socks.append(s)
        return s

    monkeypatch.setattr(arp.socket, "socket", make_sock)
    assert arp._outbound_ip("unresolvable.host", 8006) == "192.168.0.193"
    assert socks[-1].connected == ("8.8.8.8", 8006)


def test_outbound_ip_none_when_no_route(monkeypatch):
    monkeypatch.setattr(
        arp.socket, "socket",
        lambda *a, **k: _FakeSock(fail_on={"unresolvable.host", "8.8.8.8"}),
    )
    assert arp._outbound_ip("unresolvable.host", 8006) is None


def test_outbound_ip_no_host_uses_public_route(monkeypatch):
    sock = _FakeSock()
    monkeypatch.setattr(arp.socket, "socket", lambda *a, **k: sock)
    assert arp._outbound_ip(None, 8006) == "192.168.0.193"
    assert sock.connected == ("8.8.8.8", 8006)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_arp.py -q --no-cov`
Expected: AttributeError — `read_neighbor_table` / `_send_nudges` / `_outbound_ip` missing

- [ ] **Step 3: Implementation (append to pmox/arp.py)**

```python
def read_neighbor_table(platform: Optional[str] = None) -> str:
    """Raw neighbor-table text from the OS, or '' when every command fails.

    Windows and macOS ship ``arp -a``; on Linux ``ip neigh`` is preferred with
    net-tools ``arp -a`` as the fallback.
    """
    platform = platform or sys.platform
    commands = (
        [["arp", "-a"]]
        if platform in ("win32", "darwin")
        else [["ip", "neigh"], ["arp", "-a"]]
    )
    for cmd in commands:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    return ""


def _send_nudges(addresses: List[str]) -> None:
    """One empty UDP datagram per address (discard port): forces ARP resolution.

    Fire-and-forget — nothing needs to receive these, and per-address send
    errors are irrelevant.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for address in addresses:
            try:
                sock.sendto(b"", (address, _NUDGE_PORT))
            except OSError:
                continue
    finally:
        sock.close()


def _outbound_ip(host: Optional[str], port: int) -> Optional[str]:
    """Local IPv4 the OS would use to reach ``host`` (UDP connect = pure route
    lookup, no packet sent). Tries the Proxmox host first so a VPN default
    route can't mislead the subnet guess; falls back to a public address."""
    targets = ([host] if host else []) + ["8.8.8.8"]
    for target in targets:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((target, port or 8006))
            return sock.getsockname()[0]
        except OSError:
            continue
        finally:
            sock.close()
    return None
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_arp.py -q --no-cov`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add pmox/arp.py tests/test_arp.py
git commit -m "feat(arp): system edges - neighbor-table read, UDP nudges, outbound-IP route hint"
```

### Task 3: arp.py orchestration (candidate_network, find_ips_by_mac)

**Files:**
- Modify: `pmox/arp.py` (append)
- Test: `tests/test_arp.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
# ---- candidate_network ----

def test_candidate_network_uses_settings_cidr():
    net = arp.candidate_network(arp.ScanConfig(cidr="192.168.0.0/24"))
    assert str(net) == "192.168.0.0/24"

def test_candidate_network_rejects_oversized_cidr():
    assert arp.candidate_network(arp.ScanConfig(cidr="10.0.0.0/16")) is None

def test_candidate_network_rejects_bad_and_ipv6_cidr():
    assert arp.candidate_network(arp.ScanConfig(cidr="banana")) is None
    assert arp.candidate_network(arp.ScanConfig(cidr="2001:db8::/64")) is None

def test_candidate_network_falls_back_to_local_slash24(monkeypatch):
    monkeypatch.setattr(arp, "_outbound_ip", lambda host, port: "192.168.0.193")
    net = arp.candidate_network(arp.ScanConfig(host="pve.local", port=443))
    assert str(net) == "192.168.0.0/24"

def test_candidate_network_none_without_local_ip(monkeypatch):
    monkeypatch.setattr(arp, "_outbound_ip", lambda host, port: None)
    assert arp.candidate_network(arp.ScanConfig()) is None


# ---- find_ips_by_mac ----

def test_find_ips_cache_hit_skips_sweep(monkeypatch):
    monkeypatch.setattr(arp, "read_neighbor_table", lambda platform=None: LINUX_IP_NEIGH)
    nudged = []
    monkeypatch.setattr(arp, "_send_nudges", lambda addrs: nudged.append(len(addrs)))
    matches, swept = arp.find_ips_by_mac(["BC:24:11:40:C4:A3"], arp.ScanConfig(cidr="192.168.0.0/24"))
    assert matches == {"bc241140c4a3": "192.168.0.253"}
    assert swept is False
    assert nudged == []

def test_find_ips_sweeps_on_miss_then_matches(monkeypatch):
    reads = iter(["", LINUX_IP_NEIGH])
    monkeypatch.setattr(arp, "read_neighbor_table", lambda platform=None: next(reads))
    nudged = []
    monkeypatch.setattr(arp, "_send_nudges", lambda addrs: nudged.append(len(addrs)))
    monkeypatch.setattr(arp.time, "sleep", lambda s: None)
    matches, swept = arp.find_ips_by_mac(["BC:24:11:40:C4:A3"], arp.ScanConfig(cidr="192.168.0.0/24"))
    assert matches == {"bc241140c4a3": "192.168.0.253"}
    assert swept is True
    assert nudged == [254]  # /24 host addresses, no network/broadcast

def test_find_ips_miss_with_no_candidate_network(monkeypatch):
    monkeypatch.setattr(arp, "read_neighbor_table", lambda platform=None: "")
    monkeypatch.setattr(arp, "_outbound_ip", lambda host, port: None)
    matches, swept = arp.find_ips_by_mac(["BC:24:11:40:C4:A3"], arp.ScanConfig())
    assert matches == {} and swept is False

def test_find_ips_sweep_still_missing(monkeypatch):
    monkeypatch.setattr(arp, "read_neighbor_table", lambda platform=None: "")
    monkeypatch.setattr(arp, "_send_nudges", lambda addrs: None)
    monkeypatch.setattr(arp.time, "sleep", lambda s: None)
    matches, swept = arp.find_ips_by_mac(["BC:24:11:40:C4:A3"], arp.ScanConfig(cidr="192.168.0.0/24"))
    assert matches == {} and swept is True
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_arp.py -q --no-cov`
Expected: AttributeError — `candidate_network` / `find_ips_by_mac` missing

- [ ] **Step 3: Implementation (append to pmox/arp.py)**

```python
def candidate_network(scan: ScanConfig) -> Optional[ipaddress.IPv4Network]:
    """Subnet to sweep: the declared [network] cidr when configured, else a /24
    around the local outbound IPv4. None when unknown, IPv6, or larger than the
    /22 sweep cap."""
    if scan.cidr:
        try:
            net = ipaddress.ip_network(scan.cidr, strict=False)
        except ValueError:
            return None
        if not isinstance(net, ipaddress.IPv4Network) or net.prefixlen < _MAX_SWEEP_PREFIX:
            return None
        return net
    local = _outbound_ip(scan.host, scan.port)
    if not local:
        return None
    return ipaddress.ip_network(f"{local}/24", strict=False)


def find_ips_by_mac(macs: List[str], scan: ScanConfig) -> Tuple[Dict[str, str], bool]:
    """(normalized MAC -> IPv4 for every match, whether a nudge sweep ran).

    Free neighbor-table check first; only on zero matches does it nudge the
    candidate subnet, wait for ARP replies to settle, and re-read.
    """
    wanted = {normalize_mac(m) for m in macs}

    def _matches() -> Dict[str, str]:
        return {m: ip for m, ip in parse_neighbors(read_neighbor_table()).items() if m in wanted}

    found = _matches()
    if found:
        return found, False
    network = candidate_network(scan)
    if network is None:
        return {}, False
    _send_nudges([str(host) for host in network.hosts()])
    time.sleep(_SETTLE_SECONDS)
    return _matches(), True
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_arp.py -q --no-cov`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add pmox/arp.py tests/test_arp.py
git commit -m "feat(arp): orchestration - candidate subnet selection and neighbor-first lookup"
```

### Task 4: views fallback wiring

**Files:**
- Modify: `pmox/views.py` (import; `guest_ip_addresses` qemu branch; new `_arp_interfaces` helper; docstring)
- Test: `tests/test_views.py` (append)

- [ ] **Step 1: Write the failing tests (append to tests/test_views.py)**

```python
# ---- ARP fallback (source: "arp") ----


def _agentless_client(config=None):
    c = _ip_client({"vmid": 150, "node": "lukeserver", "name": "web", "type": "qemu"})
    c.agent_network_interfaces.side_effect = RuntimeError("guest agent is not running")
    c.guest_config.return_value = config if config is not None else {
        "net0": "virtio=BC:24:11:40:C4:A3,bridge=vmbr0",
        "ipconfig0": "ip=dhcp",
    }
    return c


def test_guest_ip_addresses_arp_fallback(monkeypatch):
    c = _agentless_client()
    monkeypatch.setattr(
        views.arp, "find_ips_by_mac",
        lambda macs, scan: ({"bc241140c4a3": "192.168.0.253"}, True),
    )
    out = views.guest_ip_addresses(c, "qemu", 150, scan=views.arp.ScanConfig())
    assert out["source"] == "arp"
    assert out["primary"] == "192.168.0.253"
    iface = out["interfaces"][0]
    assert iface["name"] == "net0" and iface["mac"] == "BC:24:11:40:C4:A3"
    assert iface["addresses"] == [
        {"family": "ipv4", "address": "192.168.0.253", "prefix": None, "scope": "global"}
    ]


def test_guest_ip_addresses_arp_only_matched_nics(monkeypatch):
    c = _agentless_client({
        "net0": "virtio=BC:24:11:40:C4:A3,bridge=vmbr0",
        "net1": "virtio=AA:BB:CC:DD:EE:02,bridge=vmbr0",
    })
    monkeypatch.setattr(
        views.arp, "find_ips_by_mac",
        lambda macs, scan: ({"aabbccddee02": "192.168.0.99"}, False),
    )
    out = views.guest_ip_addresses(c, "qemu", 150, scan=views.arp.ScanConfig())
    assert [i["name"] for i in out["interfaces"]] == ["net1"]
    assert out["primary"] == "192.168.0.99"


def test_guest_ip_addresses_no_scan_keeps_existing_error(monkeypatch):
    c = _agentless_client()
    monkeypatch.setattr(
        views.arp, "find_ips_by_mac",
        lambda macs, scan: (_ for _ in ()).throw(AssertionError("must not scan")),
    )
    with pytest.raises(RuntimeError, match="agent: 1"):
        views.guest_ip_addresses(c, "qemu", 150)


def test_guest_ip_addresses_scan_miss_notes_the_sweep(monkeypatch):
    c = _agentless_client()
    monkeypatch.setattr(views.arp, "find_ips_by_mac", lambda macs, scan: ({}, True))
    with pytest.raises(RuntimeError) as ei:
        views.guest_ip_addresses(c, "qemu", 150, scan=views.arp.ScanConfig())
    assert "ARP scan" in str(ei.value)
    assert "BC:24:11:40:C4:A3" in str(ei.value)
    assert "agent: 1" in str(ei.value)


def test_guest_ip_addresses_skipped_scan_keeps_message_unchanged(monkeypatch):
    c = _agentless_client()
    monkeypatch.setattr(views.arp, "find_ips_by_mac", lambda macs, scan: ({}, False))
    with pytest.raises(RuntimeError) as ei:
        views.guest_ip_addresses(c, "qemu", 150, scan=views.arp.ScanConfig())
    assert "ARP scan" not in str(ei.value)


def test_guest_ip_addresses_no_macs_keeps_message_unchanged(monkeypatch):
    c = _agentless_client({"ipconfig0": "ip=dhcp"})
    monkeypatch.setattr(
        views.arp, "find_ips_by_mac",
        lambda macs, scan: (_ for _ in ()).throw(AssertionError("must not scan")),
    )
    with pytest.raises(RuntimeError, match="agent: 1"):
        views.guest_ip_addresses(c, "qemu", 150, scan=views.arp.ScanConfig())


def test_guest_ip_addresses_static_config_beats_arp(monkeypatch):
    c = _agentless_client({
        "net0": "virtio=BC:24:11:40:C4:A3,bridge=vmbr0",
        "ipconfig0": "ip=192.168.0.240/24,gw=192.168.0.1",
    })
    monkeypatch.setattr(
        views.arp, "find_ips_by_mac",
        lambda macs, scan: (_ for _ in ()).throw(AssertionError("must not scan")),
    )
    out = views.guest_ip_addresses(c, "qemu", 150, scan=views.arp.ScanConfig())
    assert out["source"] == "config"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_views.py -q --no-cov`
Expected: failures — `views.arp` missing / unexpected `scan` kwarg

- [ ] **Step 3: Implementation**

In `pmox/views.py`, change the import block:

```python
from typing import Optional

from . import arp
from .catalog import CPU_PRESSURE, MEM_PRESSURE, STORAGE_PRESSURE
```

Add the helper above `guest_ip_addresses`:

```python
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
```

Rework the qemu branch of `guest_ip_addresses` (signature + agent-down path; the
lxc branch and the rest of the function are unchanged):

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_views.py tests/test_arp.py -q --no-cov`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add pmox/views.py tests/test_views.py
git commit -m "feat(views): ARP discovery as the last-resort vm ip source"
```

### Task 5: CLI wiring (vm ip scan, --wait passthrough, hints, help)

**Files:**
- Modify: `pmox/cli.py` — import `arp`; `_ip` command; `_wait_for_ip`; the two `vm up` DHCP hints
- Test: `tests/test_cli.py` (append)

- [ ] **Step 1: Write the failing tests (append to tests/test_cli.py)**

```python
# ------------------------------------------------- ARP discovery via vm ip --


def _agentless_vm(fake_client):
    fake_client.locate_guest.return_value = _ip_row()
    fake_client.agent_network_interfaces.side_effect = RuntimeError("guest agent is not running")
    fake_client.guest_config.return_value = {
        "net0": "virtio=BC:24:11:40:C4:A3,bridge=vmbr0",
        "ipconfig0": "ip=dhcp",
    }


def test_vm_ip_resolves_via_arp(fake_client, creds, monkeypatch):
    _agentless_vm(fake_client)
    seen = {}

    def fake_find(macs, scan):
        seen["macs"], seen["scan"] = list(macs), scan
        return {"bc241140c4a3": "192.168.0.253"}, True

    monkeypatch.setattr(cli.views.arp, "find_ips_by_mac", fake_find)
    r = inv(["--json", "vm", "ip", "150"], dict(creds, PROXMOX_NET_CIDR="192.168.0.0/24"))
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["source"] == "arp"
    assert data["primary"] == "192.168.0.253"
    assert seen["macs"] == ["BC:24:11:40:C4:A3"]
    assert seen["scan"].cidr == "192.168.0.0/24"
    assert seen["scan"].host == "pve.local"


def test_vm_ip_wait_retries_until_arp_match(fake_client, creds, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    _agentless_vm(fake_client)
    results = iter([({}, True), ({"bc241140c4a3": "192.168.0.253"}, True)])
    monkeypatch.setattr(cli.views.arp, "find_ips_by_mac", lambda macs, scan: next(results))
    r = inv(["--json", "--wait", "vm", "ip", "150"], creds)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["primary"] == "192.168.0.253"


def test_vm_ip_arp_miss_keeps_actionable_error(fake_client, creds, monkeypatch):
    _agentless_vm(fake_client)
    monkeypatch.setattr(cli.views.arp, "find_ips_by_mac", lambda macs, scan: ({}, True))
    r = inv(["--json", "vm", "ip", "150"], creds)
    assert r.exit_code == 1, r.output
    msg = json.loads(r.output)["message"]
    assert "ARP scan" in msg and "agent: 1" in msg


def test_describe_never_scans(fake_client, creds, monkeypatch):
    fake_client.locate_guest.return_value = _ip_row()
    fake_client.guest_status.return_value = {"status": "running"}
    fake_client.guest_config.return_value = {"net0": "virtio=BC:24:11:40:C4:A3,bridge=vmbr0"}
    fake_client.list_snapshots.return_value = []
    fake_client.list_tasks.return_value = []
    fake_client.agent_network_interfaces.side_effect = RuntimeError("agent down")
    monkeypatch.setattr(
        cli.views.arp, "find_ips_by_mac",
        lambda macs, scan: (_ for _ in ()).throw(AssertionError("describe must not scan")),
    )
    r = inv(["--json", "vm", "describe", "150"], creds)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["network"]["available"] is False


def test_ct_ip_never_scans(fake_client, creds, monkeypatch):
    fake_client.locate_guest.return_value = {"vmid": 200, "node": "pve1", "name": "ct", "type": "lxc"}
    fake_client.lxc_interfaces.return_value = [{"name": "eth0", "hwaddr": "aa:bb", "inet": "10.0.0.5/24"}]
    monkeypatch.setattr(
        cli.views.arp, "find_ips_by_mac",
        lambda macs, scan: (_ for _ in ()).throw(AssertionError("ct must not scan")),
    )
    r = inv(["--json", "ct", "ip", "200"], creds)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["source"] == "lxc-interfaces"


def test_vm_up_dhcp_hint_mentions_arp(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.storage_content.return_value = []
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--json", "--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04",
             "--node", "pve1", "--ssh-key", str(key)], creds)
    assert r.exit_code == 0, r.output
    hint = json.loads(r.output)["hint"]
    assert "vm ip 150 --wait" in hint
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -q --no-cov -k "arp or never_scans or dhcp_hint_mentions"`
Expected: failures (scan not wired; hints unchanged)

- [ ] **Step 3: Implementation**

`pmox/cli.py` import line gains `arp`:

```python
from . import __version__, arp, catalog, guide, ipam, provision, views
```

`_wait_for_ip` signature and the two `guest_ip_addresses` calls gain `scan`:

```python
def _wait_for_ip(ctx: typer.Context, client, kind: str, vmid: int, node: Optional[str],
                 scan: Optional[arp.ScanConfig]) -> dict:
    ...
            data = views.guest_ip_addresses(client, kind, vmid, node=node, scan=scan)
```

The `_ip` command body builds the scan config (replacing the current call sites):

```python
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            settings = ctx.obj.settings
            scan = arp.ScanConfig(cidr=settings.net_cidr, host=settings.host, port=settings.port or 8006)
            if ctx.obj.wait:
                data = _wait_for_ip(ctx, client, kind, vmid, node, scan)
            else:
                data = views.guest_ip_addresses(client, kind, vmid, node=node, scan=scan)
```

and its help text becomes:

```python
    @group.command(
        "ip",
        help=f"Show the live IP address(es) of a {label} (VM: guest agent, static config, or a "
        "same-LAN ARP scan by MAC; CT: via interfaces). With --wait, poll until an address "
        "appears (bounded by --timeout).",
    )
```

The two `vm up` DHCP hints become:

```python
                elif from_template is not None:
                    hint = (f"The address comes from DHCP; `pmox vm ip {target_vmid} --wait` returns it "
                            f"(via the template's guest agent, or a same-LAN ARP scan).")
                else:
                    hint = (f"The address comes from DHCP; run `pmox vm ip {target_vmid} --wait` — found via "
                            f"a same-LAN ARP scan (or check your DHCP leases). Use --ip or a [network] "
                            f"pool for a static address.")
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python.exe -m pytest -q --no-cov`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "feat(cli): wire ARP discovery into vm ip / --wait; reword DHCP hints"
```

### Task 6: docs + version bump

**Files:**
- Modify: `pmox/guide.py`, `plugin/skills/proxmox/SKILL.md`, `README.md`, `CHANGELOG.md`, `pyproject.toml`, `pmox/__init__.py`

- [ ] **Step 1: Apply doc updates**

- `guide.py`: the `vm ip` discovery line becomes
  `pmox vm ip <vmid> [--wait]           live address (agent, static config, or same-LAN ARP scan)`
  and the recovery section's DHCP bullet notes the agent-or-ARP chain and the
  same-L2 limitation (works from the LAN, not over a VPN; IPv4 only).
- `SKILL.md`: the discovery block's `vm ip` line and the recipes note gain the
  same agent → config → ARP chain + same-L2 caveat.
- `README.md`: the `vm ip` command line and the "Finding a guest's IP" section
  document the third source, its constraint, and `source: "arp"`.
- `CHANGELOG.md`: add `## [0.6.0] - 2026-06-10` with Added (ARP discovery
  source for `vm ip`, automatic, same-L2, IPv4-only, `source: "arp"`) and
  Changed (`vm up` DHCP hint wording); update the link refs.
- `pyproject.toml` + `pmox/__init__.py`: version `0.6.0`.

- [ ] **Step 2: Full suite**

Run: `.venv\Scripts\python.exe -m pytest`
Expected: all pass, coverage 100%

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "docs: 0.6.0 - ARP discovery documentation and version bump"
```

### Task 7: live verification + ship

- [ ] **Step 1: Live check against the real cluster (read-only)**

Run: `.venv\Scripts\python.exe -m pmox vm ip 112`
Expected: exit 0, JSON with `"source": "arp"`, `"primary": "192.168.0.253"`
(VM 112 `smoke-test` is running, agent-less, DHCP, MAC `BC:24:11:40:C4:A3`).
Also confirm `pmox vm describe 112` stays fast (network section `available: false`, no sweep delay).

- [ ] **Step 2: Merge + push (per standing instruction: merge+push when complete and tested)**

```bash
git checkout main
git merge --no-ff <feature-branch> -m "Merge: 0.6.0 - ARP-based IP discovery for agent-less DHCP guests"
.venv\Scripts\python.exe -m pytest
git push origin main
```
