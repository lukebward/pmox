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
