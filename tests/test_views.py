from unittest.mock import MagicMock

import pytest

from pmox import views
from pmox.errors import NotFoundError


def _client():
    c = MagicMock()
    c.locate_guest.return_value = {"vmid": 100, "node": "pve1", "name": "web", "type": "qemu"}
    c.guest_status.return_value = {"status": "running"}
    c.guest_config.return_value = {"cores": 2}
    c.list_snapshots.return_value = [{"name": "pre"}]
    c.list_tasks.return_value = [
        {"id": "100", "type": "qmstart"},
        {"id": "999", "type": "qmstart"},
    ]
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
    c.locate_guest.assert_called_with(100)


def test_describe_guest_uses_explicit_node_for_its_own_reads():
    c = _client()
    views.describe_guest(c, "qemu", 100, node="pve2")
    c.guest_status.assert_called_once_with("pve2", "qemu", 100)


def test_describe_guest_not_found_raises():
    c = _client()
    c.locate_guest.return_value = None
    with pytest.raises(LookupError, match="not found"):
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
    c.list_tasks.return_value = []

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
    assert out["guests"] == {"running": 1, "stopped": 1, "templates": 0}
    assert any("pve2" in w for w in out["warnings"])


def test_summarize_health_handles_zero_maxima_and_no_quorum():
    c = MagicMock()
    c.cluster_status.return_value = [{"type": "cluster", "quorate": 0}]
    c.list_nodes.return_value = [{"node": "pve1", "status": "online", "cpu": 0, "mem": 0, "maxmem": 0}]
    c.list_tasks.return_value = []
    c.cluster_resources.side_effect = lambda type=None: [] if type == "storage" else []
    out = views.summarize_health(c)
    assert out["quorate"] is False
    assert out["nodes"][0]["mem_pct"] == 0.0
    assert out["guests"] == {"running": 0, "stopped": 0, "templates": 0}
    # quorum_lost is still an issue/warning even though nothing else is wrong
    assert [i["code"] for i in out["issues"]] == ["quorum_lost"]
    assert out["warnings"] == ["cluster has lost quorum"]


# ---- Task 5: structured issues ---------------------------------------------


def test_health_flags_lost_quorum_and_offline_node():
    c = MagicMock()
    c.cluster_status.return_value = [
        {"type": "cluster", "quorate": 0},
        {"type": "node", "name": "pve1", "online": 0},
    ]
    c.list_nodes.return_value = [
        {"node": "pve1", "status": "offline", "cpu": 0, "mem": 0, "maxmem": 0},
    ]
    c.cluster_resources.side_effect = lambda type=None: []
    result = views.summarize_health(c)
    codes = {i["code"] for i in result["issues"]}
    assert "quorum_lost" in codes
    assert "node_offline" in codes
    assert all(
        i["severity"] == "critical"
        for i in result["issues"]
        if i["code"] in ("quorum_lost", "node_offline")
    )
    # every issue is mirrored into warnings
    for issue in result["issues"]:
        assert issue["message"] in result["warnings"]
    # offline node's task API is unreachable -- must not be queried
    c.list_tasks.assert_not_called()


def test_health_flags_unavailable_storage():
    c = MagicMock()
    c.cluster_status.return_value = [{"type": "cluster", "quorate": 1}]
    c.list_nodes.return_value = []
    c.list_tasks.return_value = []
    c.cluster_resources.side_effect = lambda type=None: (
        [{"storage": "backup", "node": "pve1", "status": "unknown", "disk": 0, "maxdisk": 0}]
        if type == "storage"
        else []
    )
    result = views.summarize_health(c)
    assert any(i["code"] == "storage_unavailable" for i in result["issues"])
    issue = next(i for i in result["issues"] if i["code"] == "storage_unavailable")
    assert issue["severity"] == "warning"
    assert issue["message"] in result["warnings"]


def test_health_flags_repeated_task_failures():
    c = MagicMock()
    c.cluster_status.return_value = [
        {"type": "cluster", "quorate": 1},
        {"type": "node", "name": "pve1", "online": 1},
    ]
    c.list_nodes.return_value = [
        {"node": "pve1", "status": "online", "cpu": 0, "mem": 0, "maxmem": 10},
    ]
    c.cluster_resources.side_effect = lambda type=None: []
    c.list_tasks.return_value = [
        {"type": "aptupdate", "status": "stopped", "exitstatus": "unknown error",
         "starttime": 400, "upid": "UPID:pve1:0004:aptupdate"},
        {"type": "aptupdate", "status": "stopped", "exitstatus": "unknown error",
         "starttime": 300, "upid": "UPID:pve1:0003:aptupdate"},
        {"type": "aptupdate", "status": "stopped", "exitstatus": "unknown error",
         "starttime": 200, "upid": "UPID:pve1:0002:aptupdate"},
        {"type": "aptupdate", "status": "stopped", "exitstatus": "OK",
         "starttime": 100, "upid": "UPID:pve1:0001:aptupdate"},
        {"type": "qmstart", "status": "stopped", "exitstatus": "OK",
         "starttime": 350, "upid": "UPID:pve1:0005:qmstart"},
        # still-running and typeless rows must be skipped, not counted
        {"type": "aptupdate", "status": "running", "starttime": 500, "upid": "UPID:pve1:0006:aptupdate"},
        {"status": "stopped", "exitstatus": "unknown error", "starttime": 450},
    ]
    result = views.summarize_health(c)
    issue = next(i for i in result["issues"] if i["code"] == "task_failures")
    assert "aptupdate" in issue["message"]
    assert "pmox task log UPID:" in issue["message"]
    assert issue["message"] in result["warnings"]


def test_health_excludes_templates_from_guest_counts():
    c = MagicMock()
    c.cluster_status.return_value = [{"type": "cluster", "quorate": 1}]
    c.list_nodes.return_value = []
    c.list_tasks.return_value = []
    c.cluster_resources.side_effect = lambda type=None: (
        []
        if type == "storage"
        else [
            {"vmid": 100, "status": "running", "template": 0},
            {"vmid": 101, "status": "stopped", "template": 0},
            {"vmid": 102, "status": "stopped", "template": 1},
        ]
    )
    result = views.summarize_health(c)
    assert result["guests"] == {"running": 1, "stopped": 1, "templates": 1}


def test_health_no_issues_on_healthy_cluster():
    c = MagicMock()
    c.cluster_status.return_value = [
        {"type": "cluster", "quorate": 1},
        {"type": "node", "name": "pve1", "online": 1},
    ]
    c.list_nodes.return_value = [
        {"node": "pve1", "status": "online", "cpu": 0.1, "mem": 1, "maxmem": 10},
    ]
    c.list_tasks.return_value = []
    c.cluster_resources.side_effect = lambda type=None: (
        [{"storage": "local", "node": "pve1", "status": "active", "disk": 1, "maxdisk": 10}]
        if type == "storage"
        else [{"vmid": 100, "status": "running", "template": 0}]
    )
    result = views.summarize_health(c)
    assert result["issues"] == []


# ---- guest IP addresses --------------------------------------------------------


def test_addr_scope_classifies():
    assert views._addr_scope("ipv4", "127.0.0.1") == "loopback"
    assert views._addr_scope("ipv4", "169.254.1.1") == "link"
    assert views._addr_scope("ipv4", "192.168.1.50") == "global"
    assert views._addr_scope("ipv6", "::1") == "loopback"
    assert views._addr_scope("ipv6", "fe80::1") == "link"
    assert views._addr_scope("ipv6", "2001:db8::5") == "global"


def test_locate_guest_checked_passes_matching_kind():
    c = MagicMock()
    c.locate_guest.return_value = {"vmid": 100, "node": "pve1", "type": "qemu"}
    assert views.locate_guest_checked(c, "qemu", 100)["node"] == "pve1"
    c.locate_guest.assert_called_once_with(100)


def test_locate_guest_checked_none_when_missing():
    c = MagicMock()
    c.locate_guest.return_value = None
    assert views.locate_guest_checked(c, "qemu", 999) is None


def test_locate_guest_checked_tolerates_rows_without_type():
    c = MagicMock()
    c.locate_guest.return_value = {"vmid": 100, "node": "pve1"}
    assert views.locate_guest_checked(c, "qemu", 100)["node"] == "pve1"


def test_locate_guest_checked_container_via_vm_raises():
    c = MagicMock()
    c.locate_guest.return_value = {"vmid": 100, "node": "pve1", "type": "lxc", "name": "wireguard"}
    with pytest.raises(LookupError, match="pmox ct"):
        views.locate_guest_checked(c, "qemu", 100)


def test_locate_guest_checked_vm_via_ct_raises():
    c = MagicMock()
    c.locate_guest.return_value = {"vmid": 101, "node": "pve1", "type": "qemu"}
    with pytest.raises(LookupError, match="pmox vm"):
        views.locate_guest_checked(c, "lxc", 101)


def test_guest_not_found_message_is_actionable():
    err = views.guest_not_found(999)
    assert "999" in str(err)
    assert "vm list" in str(err)


def test_guest_not_found_returns_not_found_error():
    err = views.guest_not_found(999)
    assert isinstance(err, NotFoundError)
    assert "999" in str(err)


def test_describe_guest_wrong_kind_raises():
    c = _client()  # locate_guest -> a qemu row
    with pytest.raises(LookupError, match="pmox vm"):
        views.describe_guest(c, "lxc", 100)


def _ip_client(row):
    c = MagicMock()
    c.locate_guest.return_value = row
    return c


def test_guest_ip_addresses_qemu_normalizes():
    c = _ip_client({"vmid": 150, "node": "lukeserver", "name": "web-01", "type": "qemu"})
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
    c = _ip_client({"vmid": 200, "node": "pve1", "name": "ct-db", "type": "lxc"})
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


def test_guest_ip_addresses_qemu_agent_down_no_static_ip_raises():
    c = _ip_client({"vmid": 150, "node": "lukeserver", "name": "web", "type": "qemu"})
    c.agent_network_interfaces.side_effect = RuntimeError("500 guest agent is not running")
    c.guest_config.return_value = {"ipconfig0": "ip=dhcp,ip6=auto"}  # nothing static to fall back to
    with pytest.raises(RuntimeError, match="agent: 1"):
        views.guest_ip_addresses(c, "qemu", 150)


def test_guest_ip_addresses_qemu_falls_back_to_static_ipconfig():
    c = _ip_client({"vmid": 150, "node": "lukeserver", "name": "web", "type": "qemu"})
    c.agent_network_interfaces.side_effect = RuntimeError("500 guest agent is not running")
    c.guest_config.return_value = {
        "cores": 2,  # non-ipconfig key is ignored
        "net0": "virtio=BC:24:11:DB:45:BF,bridge=vmbr0",
        "ipconfig0": "ip=192.168.0.240/24,gw=192.168.0.1,ip6=2001:db8::5/64",
    }
    out = views.guest_ip_addresses(c, "qemu", 150)
    assert out["source"] == "config"
    assert out["primary"] == "192.168.0.240"
    net0 = next(i for i in out["interfaces"] if i["name"] == "net0")
    assert net0["mac"] == "BC:24:11:DB:45:BF"
    addrs = {(a["address"], a["family"], a["prefix"], a["scope"]) for a in net0["addresses"]}
    assert ("192.168.0.240", "ipv4", 24, "global") in addrs
    assert ("2001:db8::5", "ipv6", 64, "global") in addrs
    c.guest_config.assert_called_once_with("lukeserver", "qemu", 150)


def test_guest_ip_addresses_lxc_stopped_raises():
    c = _ip_client({"vmid": 200, "node": "pve1", "name": "ct", "type": "lxc"})
    c.lxc_interfaces.side_effect = RuntimeError("500 not running")
    with pytest.raises(RuntimeError, match="stopped"):
        views.guest_ip_addresses(c, "lxc", 200)


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
