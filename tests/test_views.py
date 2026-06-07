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
