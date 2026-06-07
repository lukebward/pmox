from unittest.mock import MagicMock

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


def _content_client(existing_volids=()):
    c = MagicMock()
    c.storage_content.return_value = [{"volid": v} for v in existing_volids]
    return c


def test_build_vm_image_plan_downloads_when_absent():
    c = _content_client(existing_volids=[])
    plan = provision.build_vm_image_plan(
        c, node="p1", vmid=100, name="web", cores=1, memory=1024, disk=50,
        storage="local", image="ubuntu-24.04", sshkeys="ssh-ed25519 AAAA u@h",
        ipconfig="ip=dhcp", ciuser="ubuntu", cipassword=None, nameserver=None, start=True,
    )
    ops = [s["op"] for s in plan]
    assert ops == ["download_url", "create_guest", "resize_disk", "guest_power"]
    create = next(s for s in plan if s["op"] == "create_guest")["args"]
    assert create["scsi0"] == "local:0,import-from=local:import/noble-server-cloudimg-amd64.qcow2,iothread=1"
    assert create["ide2"] == "local:cloudinit"
    assert create["serial0"] == "socket" and create["vga"] == "serial0"
    assert create["sshkeys"] == provision.encode_sshkeys("ssh-ed25519 AAAA u@h")
    assert create["ipconfig0"] == "ip=dhcp" and create["ciuser"] == "ubuntu"
    assert create["cores"] == 1 and create["memory"] == 1024 and create["name"] == "web"
    # download + create wait; resize does not; start waits
    waits = {s["op"]: s["await_task"] for s in plan}
    assert waits == {"download_url": True, "create_guest": True, "resize_disk": False, "guest_power": True}


def test_build_vm_image_plan_skips_download_when_cached():
    c = _content_client(existing_volids=["local:import/noble-server-cloudimg-amd64.qcow2"])
    plan = provision.build_vm_image_plan(
        c, node="p1", vmid=100, name=None, cores=1, memory=1024, disk=None,
        storage="local", image="ubuntu-24.04", sshkeys=None, ipconfig=None,
        ciuser=None, cipassword=None, nameserver=None, start=False,
    )
    ops = [s["op"] for s in plan]
    assert ops == ["create_guest"]  # cached → no download; no disk → no resize; start=False → no power
    create = next(s for s in plan if s["op"] == "create_guest")["args"]
    assert "sshkeys" not in create and "name" not in create


def test_build_vm_image_plan_with_volid_image():
    c = _content_client()
    plan = provision.build_vm_image_plan(
        c, node="p1", vmid=100, name=None, cores=1, memory=1024, disk=None,
        storage="local", image="otherstore:import/x.qcow2", sshkeys=None, ipconfig=None,
        ciuser=None, cipassword=None, nameserver=None, start=False,
    )
    assert [s["op"] for s in plan] == ["create_guest"]  # explicit volid → no download
    create = next(s for s in plan if s["op"] == "create_guest")["args"]
    assert create["scsi0"] == "local:0,import-from=otherstore:import/x.qcow2,iothread=1"


def test_build_vm_image_plan_cipassword_and_nameserver():
    c = _content_client(existing_volids=["local:import/noble-server-cloudimg-amd64.qcow2"])
    plan = provision.build_vm_image_plan(
        c, node="p1", vmid=100, name=None, cores=1, memory=1024, disk=None,
        storage="local", image="ubuntu-24.04", sshkeys=None, ipconfig=None,
        ciuser=None, cipassword="s3cr3t", nameserver="8.8.8.8", start=False,
    )
    create = next(s for s in plan if s["op"] == "create_guest")["args"]
    assert create["cipassword"] == "s3cr3t"
    assert create["nameserver"] == "8.8.8.8"
