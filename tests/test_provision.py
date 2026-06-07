from unittest.mock import MagicMock, call

from pmox import provision


def test_encode_sshkeys_url_encodes():
    out = provision.encode_sshkeys("ssh-ed25519 AAAA test@host")
    assert "/" not in out  # safe='' encodes everything incl. slashes
    assert out == "ssh-ed25519%20AAAA%20test%40host"


def test_build_ipconfig_dhcp():
    assert provision.build_ipconfig("dhcp") == "ip=dhcp"


def test_build_ipconfig_static():
    assert provision.build_ipconfig("10.0.0.5/24,gw=10.0.0.1") == "ip=10.0.0.5/24,gw=10.0.0.1"


def test_step_shape():
    s = provision.step("create_guest", {"vmid": 100}, await_task=True, describe="make it")
    assert s == {"op": "create_guest", "args": {"vmid": 100}, "await_task": True, "describe": "make it"}


def test_execute_plan_dispatches_and_awaits():
    client = MagicMock()
    client.download_url.return_value = "UPID:dl"
    client.create_guest.return_value = "UPID:create"
    waited = []
    plan = [
        provision.step("download_url", {"node": "p1", "storage": "local"}, await_task=True),
        provision.step("create_guest", {"node": "p1", "kind": "qemu", "vmid": 100}, await_task=True),
        provision.step("resize_disk", {"node": "p1", "kind": "qemu", "vmid": 100, "disk": "scsi0", "size": "50G"}, await_task=False),
    ]
    results = provision.execute_plan(client, "p1", plan, waiter=lambda node, upid: waited.append((node, upid)))
    assert client.download_url.call_count == 1
    assert client.create_guest.call_count == 1
    assert client.resize_disk.call_count == 1
    # only await_task steps were waited on, in order
    assert waited == [("p1", "UPID:dl"), ("p1", "UPID:create")]
    assert results == ["UPID:dl", "UPID:create", client.resize_disk.return_value]
