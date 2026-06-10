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
