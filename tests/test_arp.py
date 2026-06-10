"""Same-LAN ARP discovery: parsing core, system edges, and orchestration."""

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
    assert arp.parse_neighbors(WINDOWS_ARP)  # header lines contribute nothing fatal


def test_parse_neighbors_pads_macos_short_octets():
    assert arp.parse_neighbors(MACOS_ARP)["a483e704b06f"] == "192.168.0.80"


def test_parse_neighbors_first_occurrence_wins():
    text = (
        "1.1.1.1 dev e lladdr aa:bb:cc:dd:ee:ff STALE\n"
        "2.2.2.2 dev e lladdr aa:bb:cc:dd:ee:ff REACHABLE\n"
    )
    assert arp.parse_neighbors(text)["aabbccddeeff"] == "1.1.1.1"


def test_parse_neighbors_empty():
    assert arp.parse_neighbors("") == {}


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


# ---- _send_nudges / _outbound_ip ----


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
    assert all(s.closed for s in socks)


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
