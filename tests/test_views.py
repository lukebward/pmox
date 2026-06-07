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
    c.cluster_resources.return_value = [{"vmid": 100, "node": "pve1", "name": "web"}]
    c.agent_network_interfaces.return_value = {"result": []}
    c.lxc_interfaces.return_value = []
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


def test_describe_guest_embeds_network():
    c = _client()
    c.agent_network_interfaces.return_value = {"result": [
        {"name": "eth0", "hardware-address": "x",
         "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "10.0.0.9", "prefix": 24}]}
    ]}
    out = views.describe_guest(c, "qemu", 100)
    assert out["network"]["available"] is True
    assert out["network"]["primary"] == "10.0.0.9"


def test_describe_guest_network_degrades_when_agent_down():
    c = _client()
    c.agent_network_interfaces.side_effect = RuntimeError("agent down")
    out = views.describe_guest(c, "qemu", 100)
    assert out["network"]["available"] is False
    assert "reason" in out["network"]


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


# ---- guest IP addresses --------------------------------------------------------


def test_addr_scope_classifies():
    assert views._addr_scope("ipv4", "127.0.0.1") == "loopback"
    assert views._addr_scope("ipv4", "169.254.1.1") == "link"
    assert views._addr_scope("ipv4", "192.168.1.50") == "global"
    assert views._addr_scope("ipv6", "::1") == "loopback"
    assert views._addr_scope("ipv6", "fe80::1") == "link"
    assert views._addr_scope("ipv6", "2001:db8::5") == "global"


def test_locate_guest_finds_row():
    c = MagicMock()
    c.cluster_resources.return_value = [
        {"vmid": 100, "node": "pve1", "name": "web"},
        {"vmid": 200, "node": "pve2", "name": "db"},
    ]
    assert views._locate_guest(c, 200) == {"vmid": 200, "node": "pve2", "name": "db"}
    assert views._locate_guest(c, 999) is None


def _ip_client(row):
    c = MagicMock()
    c.cluster_resources.return_value = [row] if row else []
    return c


def test_guest_ip_addresses_qemu_normalizes():
    c = _ip_client({"vmid": 150, "node": "lukeserver", "name": "web-01"})
    c.agent_network_interfaces.return_value = {"result": [
        {"name": "lo", "hardware-address": "00:00:00:00:00:00",
         "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "127.0.0.1", "prefix": 8}]},
        {"name": "eth0", "hardware-address": "bc:24:11:aa:bb:cc",
         "ip-addresses": [
             {"ip-address-type": "ipv4", "ip-address": "192.168.1.50", "prefix": 24},
             {"ip-address-type": "ipv6", "ip-address": "fe80::1", "prefix": 64},
             {"ip-address-type": "ipv6", "ip-address": "2001:db8::5", "prefix": 64},
             {"ip-address-type": "ipv4"},  # incomplete -> skipped
         ]},
    ]}
    out = views.guest_ip_addresses(c, "qemu", 150)
    assert out["vmid"] == 150 and out["node"] == "lukeserver" and out["name"] == "web-01"
    assert out["kind"] == "qemu" and out["source"] == "guest-agent"
    assert out["primary"] == "192.168.1.50"
    eth0 = next(i for i in out["interfaces"] if i["name"] == "eth0")
    assert eth0["mac"] == "bc:24:11:aa:bb:cc"
    assert len(eth0["addresses"]) == 3  # incomplete entry dropped
    scopes = {(a["address"], a["scope"]) for a in eth0["addresses"]}
    assert ("192.168.1.50", "global") in scopes
    assert ("fe80::1", "link") in scopes
    assert ("2001:db8::5", "global") in scopes
    c.agent_network_interfaces.assert_called_once_with("lukeserver", 150)


def test_guest_ip_addresses_lxc_normalizes():
    c = _ip_client({"vmid": 200, "node": "pve1", "name": "ct-db"})
    c.lxc_interfaces.return_value = [
        {"name": "lo", "hwaddr": "00:00:00:00:00:00", "inet": "127.0.0.1/8"},
        {"name": "eth0", "hwaddr": "aa:bb:cc:dd:ee:ff", "inet": "10.0.0.5/24", "inet6": "fe80::2/64"},
    ]
    out = views.guest_ip_addresses(c, "lxc", 200)
    assert out["kind"] == "lxc" and out["source"] == "lxc-interfaces"
    assert out["primary"] == "10.0.0.5"
    eth0 = next(i for i in out["interfaces"] if i["name"] == "eth0")
    assert {"family": "ipv4", "address": "10.0.0.5", "prefix": 24, "scope": "global"} in eth0["addresses"]
    assert {"family": "ipv6", "address": "fe80::2", "prefix": 64, "scope": "link"} in eth0["addresses"]
    c.lxc_interfaces.assert_called_once_with("pve1", 200)


def test_guest_ip_addresses_explicit_node_no_row():
    c = _ip_client(None)  # vmid not present in cluster_resources
    c.agent_network_interfaces.return_value = {"result": []}
    out = views.guest_ip_addresses(c, "qemu", 150, node="pve9")
    assert out["node"] == "pve9" and out["name"] is None
    assert out["primary"] is None and out["interfaces"] == []
    c.agent_network_interfaces.assert_called_once_with("pve9", 150)


def test_guest_ip_addresses_not_found_raises():
    c = _ip_client(None)
    with pytest.raises(LookupError):
        views.guest_ip_addresses(c, "qemu", 999)


def test_guest_ip_addresses_qemu_agent_down_raises():
    c = _ip_client({"vmid": 150, "node": "lukeserver", "name": "web"})
    c.agent_network_interfaces.side_effect = RuntimeError("500 guest agent is not running")
    with pytest.raises(RuntimeError, match="agent: 1"):
        views.guest_ip_addresses(c, "qemu", 150)


def test_guest_ip_addresses_lxc_stopped_raises():
    c = _ip_client({"vmid": 200, "node": "pve1", "name": "ct"})
    c.lxc_interfaces.side_effect = RuntimeError("500 not running")
    with pytest.raises(RuntimeError, match="stopped"):
        views.guest_ip_addresses(c, "lxc", 200)
