import pytest
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


def test_build_vm_clone_plan_full():
    plan = provision.build_vm_clone_plan(
        MagicMock(), node="p1", template_id=9000, newid=120, name="web", disk=40,
        sshkeys="ssh-ed25519 AAAA u@h", ipconfig="ip=dhcp", ciuser="ubuntu",
        cipassword=None, nameserver=None, full=True, start=True,
    )
    assert [s["op"] for s in plan] == ["clone_guest", "update_config", "resize_disk", "guest_power"]
    clone = plan[0]["args"]
    assert clone == {"node": "p1", "kind": "qemu", "vmid": 9000, "newid": 120, "name": "web", "full": 1}
    assert plan[0]["await_task"] is True
    ci = plan[1]["args"]
    assert ci["sshkeys"] == provision.encode_sshkeys("ssh-ed25519 AAAA u@h")
    assert ci["ipconfig0"] == "ip=dhcp" and ci["ciuser"] == "ubuntu"
    assert ci["node"] == "p1" and ci["vmid"] == 120
    assert plan[1]["await_task"] is False  # config PUT is synchronous


def test_build_vm_clone_plan_minimal():
    plan = provision.build_vm_clone_plan(
        MagicMock(), node="p1", template_id=9000, newid=120, name=None, disk=None,
        sshkeys=None, ipconfig=None, ciuser=None, cipassword=None, nameserver=None,
        full=False, start=False,
    )
    assert [s["op"] for s in plan] == ["clone_guest"]  # no ci → no update; no disk; no start
    assert "full" not in plan[0]["args"] and "name" not in plan[0]["args"]


def test_build_vm_clone_plan_cipassword_and_nameserver():
    plan = provision.build_vm_clone_plan(
        MagicMock(), node="p1", template_id=9000, newid=120, name=None, disk=None,
        sshkeys=None, ipconfig=None, ciuser=None, cipassword="s3cr3t", nameserver="8.8.8.8",
        full=False, start=False,
    )
    assert [s["op"] for s in plan] == ["clone_guest", "update_config"]
    ci = plan[1]["args"]
    assert ci["cipassword"] == "s3cr3t"
    assert ci["nameserver"] == "8.8.8.8"


def test_build_template_plan_builds_and_converts():
    c = MagicMock()
    c.storage_content.return_value = []  # not cached → download
    plan = provision.build_template_plan(c, node="p1", vmid=9000, name="ubuntu-2404-tmpl", storage="local", image="ubuntu-24.04")
    assert [s["op"] for s in plan] == ["download_url", "create_guest", "convert_to_template"]
    create = next(s for s in plan if s["op"] == "create_guest")["args"]
    # template carries the cloud-init drive + import disk, but NO user cloud-init keys
    assert create["ide2"] == "local:cloudinit"
    assert "import-from=local:import/noble-server-cloudimg-amd64.qcow2" in create["scsi0"]
    assert "sshkeys" not in create and "ciuser" not in create
    convert = plan[-1]
    assert convert["args"] == {"node": "p1", "kind": "qemu", "vmid": 9000}
    assert convert["await_task"] is False
    # no start step for a template
    assert all(s["op"] != "guest_power" for s in plan)


def test_build_template_plan_cached_image():
    c = MagicMock()
    c.storage_content.return_value = [{"volid": "local:import/noble-server-cloudimg-amd64.qcow2"}]
    plan = provision.build_template_plan(c, node="p1", vmid=9000, name=None, storage="local", image="ubuntu-24.04")
    assert [s["op"] for s in plan] == ["create_guest", "convert_to_template"]


def _appliance_client(appliances=None, present_volids=()):
    c = MagicMock()
    c.list_appliances.return_value = appliances if appliances is not None else [
        {"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"},
    ]
    c.storage_content.return_value = [{"volid": v} for v in present_volids]
    return c


def test_build_ct_plan_downloads_and_creates():
    c = _appliance_client()
    plan = provision.build_ct_plan(
        c, node="p1", vmid=300, hostname="box", template="ubuntu-24.04",
        storage="local-lvm", template_storage="local", disk=8, cores=1, memory=1024,
        sshkeys="ssh-ed25519 AAAA u@h", ip="dhcp", password=None, start=True,
    )
    assert [s["op"] for s in plan] == ["download_appliance", "create_guest", "guest_power"]
    dl = plan[0]["args"]
    assert dl == {"node": "p1", "storage": "local", "template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}
    create = plan[1]["args"]
    assert create["node"] == "p1" and create["kind"] == "lxc" and create["vmid"] == 300
    assert create["ostemplate"] == "local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst"
    assert create["rootfs"] == "local-lvm:8"
    assert create["net0"] == "name=eth0,bridge=vmbr0,ip=dhcp"
    assert create["hostname"] == "box"
    assert create["ssh-public-keys"] == "ssh-ed25519 AAAA u@h"  # LXC takes raw keys (proxmoxer form-encodes)
    assert create["unprivileged"] == 1
    assert "password" not in create


def test_build_ct_plan_cached_template_and_password_static_ip():
    c = _appliance_client(present_volids=["local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst"])
    plan = provision.build_ct_plan(
        c, node="p1", vmid=300, hostname=None, template="ubuntu-24.04",
        storage="local-lvm", template_storage="local", disk=8, cores=2, memory=2048,
        sshkeys=None, ip="10.0.0.9/24,gw=10.0.0.1", password="s3cret", start=False,
    )
    assert [s["op"] for s in plan] == ["create_guest"]  # cached → no download; start=False → no power
    create = plan[0]["args"]
    assert create["net0"] == "name=eth0,bridge=vmbr0,ip=10.0.0.9/24,gw=10.0.0.1"
    assert create["password"] == "s3cret"
    assert "ssh-public-keys" not in create and "hostname" not in create


def test_build_ct_plan_explicit_volid_template():
    c = _appliance_client()
    plan = provision.build_ct_plan(
        c, node="p1", vmid=300, hostname=None, template="local:vztmpl/custom.tar.zst",
        storage="local-lvm", template_storage="local", disk=8, cores=1, memory=1024,
        sshkeys=None, ip="dhcp", password=None, start=False,
    )
    assert [s["op"] for s in plan] == ["create_guest"]  # explicit volid → no aplinfo lookup, no download
    c.list_appliances.assert_not_called()
    assert plan[0]["args"]["ostemplate"] == "local:vztmpl/custom.tar.zst"


def test_build_ct_plan_unknown_template_raises():
    c = _appliance_client(appliances=[{"template": "debian-12-standard_12.7-1_amd64.tar.zst"}])
    with pytest.raises(LookupError):
        provision.build_ct_plan(
            c, node="p1", vmid=300, hostname=None, template="ubuntu-24.04",
            storage="local-lvm", template_storage="local", disk=8, cores=1, memory=1024,
            sshkeys=None, ip="dhcp", password=None, start=False,
        )
