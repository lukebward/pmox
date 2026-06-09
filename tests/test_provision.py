import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from pmox import provision
from pmox.errors import PlanError, TaskTimeout


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


def _storage_client(storages):
    c = MagicMock()
    c.list_storage.return_value = storages
    return c


def test_resolve_import_storage_auto_picks_import_capable():
    c = _storage_client([
        {"storage": "local-lvm", "content": "images,rootdir", "plugintype": "lvmthin"},
        {"storage": "local", "content": "import,iso,vztmpl,backup", "plugintype": "dir"},
    ])
    assert provision.resolve_import_storage(c, "p1") == "local"


def test_resolve_import_storage_prefers_dir_then_name():
    c = _storage_client([
        {"storage": "zfsimp", "content": "import,images", "plugintype": "zfspool"},
        {"storage": "diry", "content": "import", "plugintype": "dir"},
        {"storage": "dirx", "content": "import", "plugintype": "dir"},
    ])
    assert provision.resolve_import_storage(c, "p1") == "dirx"


def test_resolve_import_storage_explicit_ok():
    c = _storage_client([{"storage": "local", "content": "import,iso", "plugintype": "dir"}])
    assert provision.resolve_import_storage(c, "p1", "local") == "local"


def test_resolve_import_storage_explicit_not_found_raises():
    c = _storage_client([{"storage": "local", "content": "import", "plugintype": "dir"}])
    with pytest.raises(LookupError):
        provision.resolve_import_storage(c, "p1", "nope")


def test_resolve_import_storage_explicit_lacks_import_raises():
    c = _storage_client([{"storage": "local-lvm", "content": "images,rootdir", "plugintype": "lvmthin"}])
    with pytest.raises(RuntimeError, match="import"):
        provision.resolve_import_storage(c, "p1", "local-lvm")


def test_resolve_import_storage_none_available_raises():
    c = _storage_client([{"storage": "local-lvm", "content": "images,rootdir", "plugintype": "lvmthin"}])
    with pytest.raises(RuntimeError, match="No storage"):
        provision.resolve_import_storage(c, "p1")


def test_build_vm_image_plan_routes_import_to_separate_storage():
    c = _content_client(existing_volids=[])
    plan = provision.build_vm_image_plan(
        c, node="p1", vmid=100, name="web", cores=1, memory=1024, disk=50,
        storage="local-lvm", import_storage="local", image="ubuntu-24.04",
        sshkeys=None, ipconfig=None, ciuser=None, cipassword=None, nameserver=None, start=True,
    )
    download = next(s for s in plan if s["op"] == "download_url")["args"]
    assert download["storage"] == "local"
    create = next(s for s in plan if s["op"] == "create_guest")["args"]
    assert create["scsi0"] == "local-lvm:0,import-from=local:import/noble-server-cloudimg-amd64.qcow2,iothread=1"
    assert create["ide2"] == "local-lvm:cloudinit"
    c.storage_content.assert_called_with("p1", "local")


def test_build_vm_image_plan_rejects_iso_volid():
    c = _content_client()
    with pytest.raises(ValueError, match="installer ISO"):
        provision.build_vm_image_plan(
            c, node="p1", vmid=100, name=None, cores=1, memory=1024, disk=None,
            storage="local-lvm", image="local:iso/ubuntu-24.04.1-live-server-amd64.iso",
            sshkeys=None, ipconfig=None, ciuser=None, cipassword=None, nameserver=None, start=False,
        )


# ---- read_ssh_keys ----------------------------------------------------------


def test_read_ssh_keys_expands_tilde(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "k.pub").write_text("ssh-ed25519 TILDE u@h\n")
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows expanduser
    monkeypatch.setenv("HOME", str(home))         # POSIX expanduser
    assert provision.read_ssh_keys(["~/.ssh/k.pub"]) == "ssh-ed25519 TILDE u@h"


def test_read_ssh_keys_joins_multiple(tmp_path):
    a = tmp_path / "a.pub"
    a.write_text("keyA\n")
    b = tmp_path / "b.pub"
    b.write_text("keyB\n")
    assert provision.read_ssh_keys([str(a), str(b)]) == "keyA\nkeyB"


def test_read_ssh_keys_empty_returns_none():
    assert provision.read_ssh_keys([]) is None
    assert provision.read_ssh_keys(None) is None


def test_read_ssh_keys_missing_file_is_actionable(tmp_path):
    with pytest.raises(FileNotFoundError, match="SSH public key not found"):
        provision.read_ssh_keys([str(tmp_path / "nope.pub")])


# ---- fail-fast validators ----------------------------------------------------


def test_validate_guest_name_accepts_dns_names():
    assert provision.validate_guest_name("web-01") == "web-01"
    assert provision.validate_guest_name("a.b-c.example") == "a.b-c.example"
    assert provision.validate_guest_name("X1") == "X1"


@pytest.mark.parametrize("bad", ["web_01", "-web", "web-", "web..x", "", "a" * 64])
def test_validate_guest_name_rejects_invalid(bad):
    with pytest.raises(ValueError, match="Invalid guest name"):
        provision.validate_guest_name(bad)


def test_validate_ip_spec_dhcp_normalizes():
    assert provision.validate_ip_spec("dhcp") == "dhcp"
    assert provision.validate_ip_spec(" DHCP ") == "dhcp"


def test_validate_ip_spec_static_ok():
    spec = "192.168.1.50/24,gw=192.168.1.1"
    assert provision.validate_ip_spec(spec) == spec


def test_validate_ip_spec_ip6_options_ok():
    spec = "10.0.0.5/24,gw=10.0.0.1,gw6=fe80::1,ip6=2001:db8::5/64"
    assert provision.validate_ip_spec(spec) == spec
    assert provision.validate_ip_spec("10.0.0.5/24,ip6=auto") == "10.0.0.5/24,ip6=auto"


@pytest.mark.parametrize("bad", [
    "10.0.0.5",                 # missing prefix length
    "banana/24",                # not an address
    "10.0.0.5/24,gw=banana",    # bad gateway
    "10.0.0.5/24,foo=1",        # unknown option key
    "10.0.0.5/24,gw",           # option without '='
    "10.0.0.5/24,ip6=banana",   # bad ip6
])
def test_validate_ip_spec_rejects_invalid(bad):
    with pytest.raises(ValueError, match="Invalid"):
        provision.validate_ip_spec(bad)


def test_build_ipconfig_validates_and_normalizes():
    assert provision.build_ipconfig("DHCP") == "ip=dhcp"
    with pytest.raises(ValueError):
        provision.build_ipconfig("10.0.0.5")


def test_build_ct_plan_rejects_bad_ip():
    with pytest.raises(ValueError, match="Invalid"):
        provision.build_ct_plan(
            _appliance_client(), node="p1", vmid=300, hostname=None, template="ubuntu-24.04",
            storage="local-lvm", template_storage="local", disk=8, cores=1, memory=1024,
            sshkeys=None, ip="not-an-ip", password=None, start=False,
        )


def test_build_vm_image_plan_rejects_bad_name():
    with pytest.raises(ValueError, match="Invalid guest name"):
        provision.build_vm_image_plan(
            _content_client(), node="p1", vmid=100, name="web_01", cores=1, memory=1024,
            disk=None, storage="local", image="ubuntu-24.04", sshkeys=None, ipconfig=None,
            ciuser=None, cipassword=None, nameserver=None, start=False,
        )


def test_build_vm_clone_plan_rejects_bad_name():
    with pytest.raises(ValueError, match="Invalid guest name"):
        provision.build_vm_clone_plan(
            MagicMock(), node="p1", template_id=9000, newid=120, name="bad name", disk=None,
            sshkeys=None, ipconfig=None, ciuser=None, cipassword=None, nameserver=None,
            full=True, start=True,
        )


def test_build_ct_plan_rejects_bad_hostname():
    with pytest.raises(ValueError, match="Invalid guest name"):
        provision.build_ct_plan(
            _appliance_client(), node="p1", vmid=300, hostname="box_1", template="ubuntu-24.04",
            storage="local-lvm", template_storage="local", disk=8, cores=1, memory=1024,
            sshkeys=None, ip="dhcp", password=None, start=False,
        )


# ---- resolve_disk_storage ----------------------------------------------------


def test_resolve_disk_storage_prefers_local_lvm():
    c = _storage_client([
        {"storage": "tank", "content": "images,rootdir"},
        {"storage": "local-lvm", "content": "images,rootdir"},
    ])
    assert provision.resolve_disk_storage(c, "p1") == "local-lvm"


def test_resolve_disk_storage_falls_back_alphabetical():
    c = _storage_client([
        {"storage": "zfs-b", "content": "images"},
        {"storage": "zfs-a", "content": "images"},
        {"storage": "local", "content": "iso,vztmpl"},
    ])
    assert provision.resolve_disk_storage(c, "p1") == "zfs-a"


def test_resolve_disk_storage_skips_inactive():
    c = _storage_client([
        {"storage": "dead", "content": "images", "active": 0},
        {"storage": "off", "content": "images", "enabled": 0},
        {"storage": "alive", "content": "images"},
    ])
    assert provision.resolve_disk_storage(c, "p1") == "alive"


def test_resolve_disk_storage_explicit_validated():
    c = _storage_client([{"storage": "local-lvm", "content": "images,rootdir"}])
    assert provision.resolve_disk_storage(c, "p1", "local-lvm") == "local-lvm"


def test_resolve_disk_storage_explicit_missing_raises():
    c = _storage_client([{"storage": "local-lvm", "content": "images"}])
    with pytest.raises(LookupError, match="nope"):
        provision.resolve_disk_storage(c, "p1", "nope")


def test_resolve_disk_storage_explicit_wrong_content_raises():
    c = _storage_client([{"storage": "local", "content": "import,iso"}])
    with pytest.raises(RuntimeError, match="images"):
        provision.resolve_disk_storage(c, "p1", "local")


def test_resolve_disk_storage_rootdir_content():
    c = _storage_client([{"storage": "ctpool", "content": "rootdir"}])
    assert provision.resolve_disk_storage(c, "p1", content="rootdir") == "ctpool"


def test_resolve_disk_storage_none_available_raises():
    c = _storage_client([{"storage": "local", "content": "iso"}])
    with pytest.raises(RuntimeError, match="No active storage"):
        provision.resolve_disk_storage(c, "p1")


# ---- appliance matching ------------------------------------------------------


def test_match_appliance_exact_wins():
    names = ["ubuntu-24.04-standard_24.04-2_amd64.tar.zst", "exact-name"]
    assert provision.match_appliance(names, "exact-name") == "exact-name"


def test_match_appliance_prefers_standard_prefix_latest():
    names = [
        "ubuntu-24.04-standard_24.04-1_amd64.tar.zst",
        "ubuntu-24.04-standard_24.04-2_amd64.tar.zst",
        "ubuntu-24.04-minimal_24.04-2_amd64.tar.zst",
    ]
    assert provision.match_appliance(names, "ubuntu-24.04") == "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"


def test_match_appliance_substring_fallback_latest():
    names = ["custom-ubuntu-24.04_1.tar.zst", "custom-ubuntu-24.04_2.tar.zst"]
    assert provision.match_appliance(names, "ubuntu-24.04") == "custom-ubuntu-24.04_2.tar.zst"


def test_match_appliance_no_match_returns_none():
    assert provision.match_appliance(["debian-12-standard_12.7-1_amd64.tar.zst"], "ubuntu-24.04") is None


def test_resolve_appliance_picks_latest_standard():
    c = _appliance_client(appliances=[
        {"template": "ubuntu-24.04-standard_24.04-1_amd64.tar.zst"},
        {"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"},
    ])
    plan = provision.build_ct_plan(
        c, node="p1", vmid=300, hostname=None, template="ubuntu-24.04",
        storage="local-lvm", template_storage="local", disk=8, cores=1, memory=1024,
        sshkeys=None, ip="dhcp", password=None, start=False,
    )
    dl = next(s for s in plan if s["op"] == "download_appliance")
    assert dl["args"]["template"] == "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"


# ---- checksum parsing --------------------------------------------------------


def test_parse_checksum_ok():
    assert provision.parse_checksum("sha256:ABCdef012345") == ("sha256", "ABCdef012345")
    assert provision.parse_checksum("SHA512:ff") == ("sha512", "ff")


@pytest.mark.parametrize("bad", ["nohex", "md6:abc", "sha256:", ":abc"])
def test_parse_checksum_rejects(bad):
    with pytest.raises(ValueError, match="--checksum"):
        provision.parse_checksum(bad)


# ---- plan failure context ----------------------------------------------------


def _failing_image_plan():
    return [
        provision.step("download_url", {"node": "p1", "storage": "local"}, await_task=True, describe="download img"),
        provision.step("create_guest", {"node": "p1", "kind": "qemu", "vmid": 150}, await_task=True, describe="create VM 150"),
        provision.step("guest_power", {"node": "p1", "kind": "qemu", "vmid": 150, "action": "start"}, await_task=True, describe="start"),
    ]


def test_execute_plan_failure_carries_recovery_context():
    client = MagicMock()
    client.download_url.return_value = "UPID:dl"
    client.create_guest.return_value = "UPID:create"
    client.guest_power.side_effect = RuntimeError("start exploded")
    with pytest.raises(PlanError) as ei:
        provision.execute_plan(client, "p1", _failing_image_plan(), waiter=lambda n, u: None)
    e = ei.value
    assert "start" in str(e) and "start exploded" in str(e)
    assert e.extra["vmid"] == 150
    assert e.extra["node"] == "p1"
    assert e.extra["failed_step"] == "start"
    assert e.extra["completed_steps"] == ["download img", "create VM 150"]
    assert "pmox vm describe 150" in e.extra["hint"]


def test_execute_plan_failure_before_create_is_safe_to_retry():
    client = MagicMock()
    client.download_url.side_effect = RuntimeError("dl failed")
    with pytest.raises(PlanError) as ei:
        provision.execute_plan(client, "p1", _failing_image_plan(), waiter=lambda n, u: None)
    e = ei.value
    assert e.extra["completed_steps"] == []
    assert "retrying" in e.extra["hint"].lower()
    assert "safe" in e.extra["hint"].lower()


def test_execute_plan_failure_during_create_wait_counts_as_created():
    client = MagicMock()
    client.download_url.return_value = "UPID:dl"
    client.create_guest.return_value = "UPID:create"

    def waiter(node, upid):
        if upid == "UPID:create":
            raise TaskTimeout("too slow", extra={"upid": upid, "node": node})

    with pytest.raises(PlanError) as ei:
        provision.execute_plan(client, "p1", _failing_image_plan(), waiter=waiter)
    e = ei.value
    assert e.extra["upid"] == "UPID:create"  # cause's structured fields survive
    assert "pmox vm describe 150" in e.extra["hint"]


def test_execute_plan_failure_clone_reports_newid_and_ct_uses_ct():
    client = MagicMock()
    client.clone_guest.return_value = "UPID:clone"
    client.guest_power.side_effect = RuntimeError("boom")
    plan = [
        provision.step("clone_guest", {"node": "p1", "kind": "qemu", "vmid": 9000, "newid": 120}, await_task=True, describe="clone 9000 -> 120"),
        provision.step("guest_power", {"node": "p1", "kind": "qemu", "vmid": 120, "action": "start"}, await_task=True, describe="start"),
    ]
    with pytest.raises(PlanError) as ei:
        provision.execute_plan(client, "p1", plan, waiter=lambda n, u: None)
    assert ei.value.extra["vmid"] == 120
    assert "pmox vm describe 120" in ei.value.extra["hint"]

    ct_client = MagicMock()
    ct_client.create_guest.return_value = "UPID:create"
    ct_client.guest_power.side_effect = RuntimeError("boom")
    ct_plan = [
        provision.step("create_guest", {"node": "p1", "kind": "lxc", "vmid": 300}, await_task=True, describe="create CT 300"),
        provision.step("guest_power", {"node": "p1", "kind": "lxc", "vmid": 300, "action": "start"}, await_task=True, describe="start"),
    ]
    with pytest.raises(PlanError) as ei:
        provision.execute_plan(ct_client, "p1", ct_plan, waiter=lambda n, u: None)
    assert "pmox ct describe 300" in ei.value.extra["hint"]


def test_ensure_ssh_key_reads_existing(tmp_path):
    pub = tmp_path / "id_ed25519.pub"
    pub.write_text("ssh-ed25519 EXISTING u@h\n")
    assert provision.ensure_ssh_key(str(pub)) == "ssh-ed25519 EXISTING u@h"


def test_ensure_ssh_key_generates_when_missing(tmp_path, monkeypatch):
    pub = tmp_path / ".ssh" / "id_ed25519.pub"

    def fake_run(cmd, **kwargs):
        priv = cmd[cmd.index("-f") + 1]
        Path(priv + ".pub").write_text("ssh-ed25519 GENERATED pmox\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(provision.subprocess, "run", fake_run)
    assert provision.ensure_ssh_key(str(pub)) == "ssh-ed25519 GENERATED pmox"


def test_ensure_ssh_key_bad_suffix_raises(tmp_path):
    with pytest.raises(ValueError):
        provision.ensure_ssh_key(str(tmp_path / "id_ed25519"))  # missing and not .pub
