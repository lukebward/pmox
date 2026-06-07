from unittest.mock import MagicMock

import pytest

from pmox import ipam


def test_static_ips_from_ipconfig_collects_statics():
    cfg = {
        "cores": 2,
        "ipconfig0": "ip=192.168.0.50/24,gw=192.168.0.1,ip6=2001:db8::5/64",
        "ipconfig1": "ip=dhcp,ip6=auto",
    }
    assert ipam.static_ips_from_ipconfig(cfg) == ["192.168.0.50", "2001:db8::5"]


def test_parse_pool_range_ok():
    assert ipam.parse_pool_range("192.168.0.200-192.168.0.250") == ("192.168.0.200", "192.168.0.250")


def test_parse_pool_range_bad():
    with pytest.raises(ValueError):
        ipam.parse_pool_range("192.168.0.200")


def _ipam_client(guests):
    c = MagicMock()
    c.cluster_resources.return_value = [{"node": "n1", "type": "qemu", "vmid": vmid} for vmid, _ in guests]
    cfgs = {vmid: cfg for vmid, cfg in guests}
    c.guest_config.side_effect = lambda node, kind, vmid: cfgs[vmid]
    return c


def test_allocate_ip_returns_lowest_free():
    c = _ipam_client([
        (100, {"ipconfig0": "ip=192.168.0.200/24,gw=192.168.0.1"}),
        (101, {"ipconfig0": "ip=192.168.0.201/24,gw=192.168.0.1"}),
    ])
    out = ipam.allocate_ip(c, cidr="192.168.0.0/24", gateway="192.168.0.1", pool="192.168.0.200-192.168.0.250")
    assert out == "192.168.0.202/24"


def test_allocate_ip_reserves_gateway():
    c = _ipam_client([])
    out = ipam.allocate_ip(c, cidr="192.168.0.0/24", gateway="192.168.0.200", pool="192.168.0.200-192.168.0.250")
    assert out == "192.168.0.201/24"


def test_allocate_ip_exhausted_raises():
    c = _ipam_client([(100, {"ipconfig0": "ip=192.168.0.200/24"})])
    with pytest.raises(RuntimeError, match="exhausted"):
        ipam.allocate_ip(c, cidr="192.168.0.0/24", gateway="192.168.0.1", pool="192.168.0.200-192.168.0.200")
