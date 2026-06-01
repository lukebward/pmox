"""Unit tests for ProxmoxClient.

The proxmoxer API is a fluent builder: ``api.nodes(node).qemu(vmid).status.start.post()``.
We inject a MagicMock as the api and assert both the returned value and that the
correct endpoint chain was invoked.
"""


def test_version(client, api):
    api.version.get.return_value = {"version": "8.1.4"}
    assert client.version() == {"version": "8.1.4"}
    api.version.get.assert_called_once_with()


def test_cluster_status(client, api):
    api.cluster.status.get.return_value = [{"type": "cluster"}]
    assert client.cluster_status() == [{"type": "cluster"}]


def test_cluster_resources_typed_and_untyped(client, api):
    api.cluster.resources.get.return_value = []
    client.cluster_resources(type="vm")
    api.cluster.resources.get.assert_called_with(type="vm")
    client.cluster_resources()
    api.cluster.resources.get.assert_called_with()


def test_list_nodes(client, api):
    api.nodes.get.return_value = [{"node": "pve1"}]
    assert client.list_nodes() == [{"node": "pve1"}]
    api.nodes.get.assert_called_once_with()


def test_node_status(client, api):
    api.nodes.return_value.status.get.return_value = {"uptime": 10}
    assert client.node_status("pve1") == {"uptime": 10}
    api.nodes.assert_called_with("pve1")


def test_list_guests_filters_by_kind_and_node(client, api):
    api.cluster.resources.get.return_value = [
        {"vmid": 100, "type": "qemu", "node": "pve1"},
        {"vmid": 200, "type": "lxc", "node": "pve1"},
        {"vmid": 101, "type": "qemu", "node": "pve2"},
    ]
    assert [v["vmid"] for v in client.list_guests("qemu")] == [100, 101]
    assert [v["vmid"] for v in client.list_guests("lxc")] == [200]
    assert [v["vmid"] for v in client.list_guests("qemu", node="pve1")] == [100]


def test_guest_status_and_config(client, api):
    guest = api.nodes.return_value.qemu.return_value
    guest.status.current.get.return_value = {"status": "running"}
    guest.config.get.return_value = {"cores": 2}
    assert client.guest_status("pve1", "qemu", 100) == {"status": "running"}
    assert client.guest_config("pve1", "qemu", 100) == {"cores": 2}
    api.nodes.assert_called_with("pve1")
    api.nodes.return_value.qemu.assert_called_with(100)


def test_guest_power_start(client, api):
    client.guest_power("pve1", "qemu", 100, "start")
    api.nodes.return_value.qemu.return_value.status.start.post.assert_called_once_with()


def test_guest_power_shutdown_lxc(client, api):
    client.guest_power("pve2", "lxc", 200, "shutdown")
    api.nodes.assert_called_with("pve2")
    api.nodes.return_value.lxc.assert_called_with(200)
    api.nodes.return_value.lxc.return_value.status.shutdown.post.assert_called_once_with()


def test_create_guest(client, api):
    client.create_guest("pve1", "qemu", 105, name="new", cores=2)
    api.nodes.return_value.qemu.post.assert_called_once_with(vmid=105, name="new", cores=2)


def test_clone_guest(client, api):
    client.clone_guest("pve1", "qemu", 100, 105, name="copy", full=1)
    api.nodes.return_value.qemu.return_value.clone.post.assert_called_once_with(
        newid=105, name="copy", full=1
    )


def test_migrate_guest(client, api):
    client.migrate_guest("pve1", "qemu", 100, "pve2", online=1)
    api.nodes.return_value.qemu.return_value.migrate.post.assert_called_once_with(
        target="pve2", online=1
    )


def test_delete_guest_purge_and_plain(client, api):
    client.delete_guest("pve1", "qemu", 100, purge=True)
    api.nodes.return_value.qemu.return_value.delete.assert_called_once_with(purge=1)

    api.reset_mock()
    client.delete_guest("pve1", "lxc", 200, purge=False)
    api.nodes.return_value.lxc.return_value.delete.assert_called_once_with()


def test_snapshots(client, api):
    guest = api.nodes.return_value.qemu.return_value
    guest.snapshot.get.return_value = [{"name": "pre"}]
    assert client.list_snapshots("pve1", "qemu", 100) == [{"name": "pre"}]

    client.create_snapshot("pve1", "qemu", 100, "snap1", description="d")
    guest.snapshot.post.assert_called_once_with(snapname="snap1", description="d")

    client.delete_snapshot("pve1", "qemu", 100, "snap1")
    guest.snapshot.assert_called_with("snap1")
    guest.snapshot.return_value.delete.assert_called_once_with()

    client.rollback_snapshot("pve1", "qemu", 100, "snap1")
    guest.snapshot.return_value.rollback.post.assert_called_once_with()


def test_storage(client, api):
    api.storage.get.return_value = [{"storage": "local"}]
    assert client.list_storage() == [{"storage": "local"}]

    api.nodes.return_value.storage.get.return_value = [{"storage": "local-lvm"}]
    assert client.list_storage(node="pve1") == [{"storage": "local-lvm"}]

    api.nodes.return_value.storage.return_value.content.get.return_value = [{"volid": "x"}]
    assert client.storage_content("pve1", "local") == [{"volid": "x"}]
    api.nodes.return_value.storage.assert_called_with("local")


def test_tasks(client, api):
    api.nodes.return_value.tasks.get.return_value = [{"upid": "U"}]
    assert client.list_tasks("pve1", limit=10) == [{"upid": "U"}]
    api.nodes.return_value.tasks.get.assert_called_once_with(limit=10)

    api.nodes.return_value.tasks.return_value.status.get.return_value = {"status": "OK"}
    assert client.task_status("pve1", "UPID:x") == {"status": "OK"}
    api.nodes.return_value.tasks.assert_called_with("UPID:x")


def test_resolve_node_and_kind(client, api):
    api.cluster.resources.get.return_value = [
        {"vmid": 100, "node": "pve3", "type": "qemu"},
        {"vmid": 200, "node": "pve1", "type": "lxc"},
    ]
    assert client.resolve_node(100) == "pve3"
    assert client.resolve_node("200") == "pve1"
    assert client.resolve_node(999) is None
    assert client.guest_kind(200) == "lxc"
    assert client.guest_kind(999) is None


def test_from_settings_builds_api(monkeypatch):
    import pmox.client as client_mod
    from pmox.config import Settings

    captured = {}

    class FakeAPI:
        def __init__(self, host, **kwargs):
            captured["host"] = host
            captured.update(kwargs)

    monkeypatch.setattr("proxmoxer.ProxmoxAPI", FakeAPI)

    settings = Settings(
        host="h", token_id="root@pam!t", token_secret="s", verify_ssl=False, port=8006, timeout=30
    )
    c = client_mod.ProxmoxClient.from_settings(settings)

    assert isinstance(c, client_mod.ProxmoxClient)
    assert captured["host"] == "h"
    assert captured["user"] == "root@pam"
    assert captured["token_name"] == "t"
    assert captured["token_value"] == "s"
    assert captured["verify_ssl"] is False
    assert captured["port"] == 8006
    assert captured["service"] == "PVE"


def test_task_log(client, api):
    api.nodes.return_value.tasks.return_value.log.get.return_value = [{"t": "x"}]
    assert client.task_log("pve1", "UPID:x", limit=5) == [{"t": "x"}]
    api.nodes.return_value.tasks.assert_called_with("UPID:x")
    api.nodes.return_value.tasks.return_value.log.get.assert_called_once_with(limit=5)
