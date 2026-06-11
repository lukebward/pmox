"""Agent golden templates: `pmox template build/list` and the `vm up` default flow.

The build command is the one place pmox reaches inside a guest (over SSH, with
the key it injected) — every edge is monkeypatched here; no cluster, no SSH.
"""

import json
from unittest.mock import MagicMock, call

import pytest
from typer.testing import CliRunner

import pmox.cli as cli
from pmox import catalog, config, guestops, provision, views

runner = CliRunner()

_IMPORT_STORAGES = [
    {"storage": "local", "content": "import,iso,vztmpl,backup", "plugintype": "dir"},
    {"storage": "local-lvm", "content": "images,rootdir", "plugintype": "lvmthin"},
]

TPL_ROW = {
    "vmid": 9001, "name": "agent-ubuntu-24-04", "node": "pve1", "type": "qemu",
    "template": 1, "tags": "pmox-agent;img-ubuntu-24.04",
}


def inv(args, creds, **kwargs):
    return runner.invoke(cli.app, args, env=creds, **kwargs)


def _keypair(tmp_path):
    pub = tmp_path / "id_ed25519.pub"
    pub.write_text("ssh-ed25519 AAAA pmox")
    (tmp_path / "id_ed25519").write_text("priv")
    return str(pub)


@pytest.fixture
def build_edges(monkeypatch):
    """Silence sleeps and stub the SSH transport; returns the recorder."""
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    edges = MagicMock()
    edges.establish_agent_ssh.side_effect = lambda user, candidates, key, **kw: candidates[-1]
    monkeypatch.setattr(cli.guestops, "establish_agent_ssh", edges.establish_agent_ssh)
    return edges


def _wire_build_client(fake_client):
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.storage_content.return_value = []
    fake_client.cluster_resources.return_value = []
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.guest_power.return_value = "UPID:x"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    fake_client.guest_status.return_value = {"status": "stopped"}
    fake_client.guest_config.return_value = {"net0": "virtio=BC:24:11:0D:85:4E,bridge=vmbr0"}
    fake_client.agent_network_interfaces.return_value = {"result": []}
    return fake_client


# ------------------------------------------------------------------ views --


def test_find_agent_template_matches_tags_and_node():
    client = MagicMock()
    client.cluster_resources.return_value = [
        {"vmid": 1, "type": "qemu", "template": 0, "tags": "pmox-agent;img-ubuntu-24.04", "node": "pve1"},
        {"vmid": 2, "type": "qemu", "template": 1, "tags": "img-ubuntu-24.04", "node": "pve1"},
        {"vmid": 3, "type": "lxc", "template": 1, "tags": "pmox-agent;img-ubuntu-24.04", "node": "pve1"},
        dict(TPL_ROW),
    ]
    row = views.find_agent_template(client, "ubuntu-24.04")
    assert row["vmid"] == 9001
    assert views.find_agent_template(client, "ubuntu-24.04", node="pve2") is None
    assert views.find_agent_template(client, "debian-12") is None


def test_find_agent_template_accepts_comma_separated_tags():
    client = MagicMock()
    client.cluster_resources.return_value = [
        {"vmid": 7, "type": "qemu", "template": 1, "tags": "pmox-agent,img-ubuntu-24.04", "node": "n"},
    ]
    assert views.find_agent_template(client, "ubuntu-24.04")["vmid"] == 7


def test_find_agent_template_ignores_missing_tags():
    client = MagicMock()
    client.cluster_resources.return_value = [{"vmid": 7, "type": "qemu", "template": 1, "node": "n"}]
    assert views.find_agent_template(client, "ubuntu-24.04") is None


# ----------------------------------------------------------------- config --


def test_agent_templates_defaults_on():
    assert config.Settings().agent_templates is True


def test_agent_templates_env_off():
    s = config.load_settings(env={"PMOX_AGENT_TEMPLATES": "0"}, config_path=__nonexistent())
    assert s.agent_templates is False


def test_agent_templates_file_off(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[defaults]\nagent_templates = false\n")
    s = config.load_settings(env={}, config_path=p)
    assert s.agent_templates is False


def test_agent_templates_env_beats_file(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[defaults]\nagent_templates = false\n")
    s = config.load_settings(env={"PMOX_AGENT_TEMPLATES": "true"}, config_path=p)
    assert s.agent_templates is True


def __nonexistent():
    from pathlib import Path

    return Path("Z:/definitely/not/here/pmox.toml")


# ---------------------------------------------------------------- catalog --


def test_catalog_images_carry_default_user():
    assert catalog.resolve_image("ubuntu-24.04")["user"] == "ubuntu"
    assert catalog.resolve_image("debian-12")["user"] == "debian"
    assert catalog.resolve_image("https://x/y.img")["user"] is None
    assert catalog.resolve_image("local:import/foo.qcow2").get("user") is None


# -------------------------------------------------------------- provision --


def test_clone_plan_applies_cores_and_memory():
    plan = provision.build_vm_clone_plan(
        MagicMock(), node="pve1", template_id=9001, newid=150, name="web", disk=None,
        cores=2, memory=4096, start=True,
    )
    update = next(s for s in plan if s["op"] == "update_config")
    assert update["args"]["cores"] == 2
    assert update["args"]["memory"] == 4096


def test_clone_plan_passes_target_storage():
    plan = provision.build_vm_clone_plan(
        MagicMock(), node="pve1", template_id=9001, newid=150, name="web", disk=None,
        storage="fast", start=False,
    )
    assert plan[0]["args"]["storage"] == "fast"
    assert plan[0]["args"]["full"] == 1


# ----------------------------------------------------- template build CLI --


def test_template_build_needs_dangerous(fake_client, creds, tmp_path):
    _wire_build_client(fake_client)
    r = inv(["template", "build", "ubuntu-24.04", "--node", "pve1",
             "--ip", "192.168.0.198/24,gw=192.168.0.1", "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 4, r.output
    fake_client.create_guest.assert_not_called()


def test_template_build_dry_run(fake_client, creds, tmp_path):
    _wire_build_client(fake_client)
    r = inv(["--dry-run", "template", "build", "ubuntu-24.04", "--node", "pve1",
             "--ip", "192.168.0.198/24,gw=192.168.0.1", "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["dry_run"] is True and payload["op"] == "qemu.template.build"
    assert payload["post_steps"]  # ssh/convert phases are listed
    fake_client.create_guest.assert_not_called()


def test_template_build_static_ip_happy_path(fake_client, creds, tmp_path, build_edges):
    _wire_build_client(fake_client)
    key = _keypair(tmp_path)
    r = inv(["--json", "--dangerous", "template", "build", "ubuntu-24.04", "--node", "pve1",
             "--ip", "192.168.0.198/24,gw=192.168.0.1", "--ssh-key", key], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["ok"] is True and payload["op"] == "qemu.template.build"
    assert payload["vmid"] == 150 and payload["node"] == "pve1"
    assert payload["tags"] == "pmox-agent;img-ubuntu-24.04"
    assert payload["reused"] is False

    # SSH transport: MAC-derived IPv6 link-local candidates FIRST, static IPv4 last;
    # the catalog user and our private key are used
    user, candidates, priv = build_edges.establish_agent_ssh.call_args.args[:3]
    assert user == "ubuntu"
    assert candidates[0].startswith("fe80::be24:11ff:fe0d:854e%")
    assert candidates[-1] == "192.168.0.198"
    assert priv.endswith("id_ed25519")

    # agent verified through the PVE API, guest shut down, config finalized, converted
    fake_client.agent_network_interfaces.assert_called()
    assert any((c.args and c.args[-1] == "shutdown") or c.kwargs.get("action") == "shutdown"
               for c in fake_client.guest_power.call_args_list)
    final = fake_client.update_config.call_args_list[-1]
    assert final.kwargs["tags"] == "pmox-agent;img-ubuntu-24.04"
    assert final.kwargs["ipconfig0"] == "ip=dhcp"   # static bootstrap address released
    fake_client.convert_to_template.assert_called_once_with("pve1", "qemu", 150)


def test_template_build_pool_allocates_bootstrap_ip(fake_client, creds, tmp_path, build_edges, monkeypatch):
    _wire_build_client(fake_client)
    monkeypatch.setattr(cli.ipam, "allocate_ip", lambda *a, **k: "192.168.0.201/24")
    env = dict(creds, PROXMOX_NET_CIDR="192.168.0.0/24", PROXMOX_NET_GATEWAY="192.168.0.1",
               PROXMOX_NET_POOL="192.168.0.200-192.168.0.210")
    r = inv(["--json", "--dangerous", "template", "build", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", _keypair(tmp_path)], env)
    assert r.exit_code == 0, r.output
    assert "192.168.0.201" in build_edges.establish_agent_ssh.call_args.args[1]
    assert fake_client.update_config.call_args_list[-1].kwargs["ipconfig0"] == "ip=dhcp"


def test_template_build_dhcp_discovery_is_last_resort(fake_client, creds, tmp_path, build_edges, monkeypatch):
    """No static address and no MAC in the config: only then does discovery run."""
    _wire_build_client(fake_client)
    fake_client.guest_config.return_value = {}
    monkeypatch.setattr(cli, "_wait_for_ip", lambda *a, **k: {"primary": "192.168.0.77"})
    r = inv(["--json", "--dangerous", "template", "build", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 0, r.output
    assert build_edges.establish_agent_ssh.call_args.args[1] == ["192.168.0.77"]
    # DHCP bootstrap: nothing to release — no ipconfig0 reset in the final update
    assert "ipconfig0" not in fake_client.update_config.call_args_list[-1].kwargs


def test_template_build_dhcp_with_mac_needs_no_discovery(fake_client, creds, tmp_path, build_edges, monkeypatch):
    """DHCP build VM with a known MAC: link-local candidates suffice — discovery never runs."""
    _wire_build_client(fake_client)
    monkeypatch.setattr(cli, "_wait_for_ip",
                        lambda *a, **k: pytest.fail("discovery should not run"))
    r = inv(["--json", "--dangerous", "template", "build", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 0, r.output
    candidates = build_edges.establish_agent_ssh.call_args.args[1]
    assert candidates and all(c.startswith("fe80::") for c in candidates)


def test_template_build_reuses_existing(fake_client, creds, tmp_path, build_edges):
    _wire_build_client(fake_client)
    fake_client.cluster_resources.return_value = [dict(TPL_ROW)]
    r = inv(["--json", "--dangerous", "template", "build", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["reused"] is True and payload["vmid"] == 9001
    fake_client.create_guest.assert_not_called()
    build_edges.establish_agent_ssh.assert_not_called()


def test_template_build_ssh_failure_has_recovery_context(fake_client, creds, tmp_path, build_edges):
    _wire_build_client(fake_client)
    build_edges.establish_agent_ssh.side_effect = RuntimeError("apt exploded")
    r = inv(["--json", "--dangerous", "template", "build", "ubuntu-24.04", "--node", "pve1",
             "--ip", "192.168.0.198/24,gw=192.168.0.1", "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 1, r.output
    payload = json.loads(r.output)
    assert payload["ok"] is False
    assert payload["vmid"] == 150
    assert payload["failed_step"] == "install agent"
    assert "create VM" in " ".join(payload["completed_steps"])
    assert "150" in payload["hint"]
    fake_client.convert_to_template.assert_not_called()


def test_template_build_url_image_needs_user(fake_client, creds, tmp_path):
    _wire_build_client(fake_client)
    r = inv(["--json", "--dangerous", "template", "build", "https://x.test/img.qcow2", "--node", "pve1",
             "--ip", "192.168.0.198/24,gw=192.168.0.1", "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 1, r.output
    assert "--user" in json.loads(r.output)["message"]
    fake_client.create_guest.assert_not_called()


def test_template_build_user_override(fake_client, creds, tmp_path, build_edges):
    _wire_build_client(fake_client)
    r = inv(["--json", "--dangerous", "template", "build", "https://x.test/img.qcow2", "--node", "pve1",
             "--user", "admin", "--ip", "192.168.0.198/24,gw=192.168.0.1",
             "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 0, r.output
    assert build_edges.establish_agent_ssh.call_args.args[0] == "admin"


def test_template_build_single_node_autopick_and_default_key(fake_client, creds, tmp_path, build_edges, monkeypatch):
    _wire_build_client(fake_client)
    fake_client.list_nodes.return_value = [{"node": "pve1"}]
    key = _keypair(tmp_path)
    env = dict(creds, PROXMOX_DEFAULT_SSH_KEY=key)
    r = inv(["--json", "--dangerous", "template", "build", "ubuntu-24.04",
             "--vmid", "777", "--name", "golden",
             "--ip", "192.168.0.198/24,gw=192.168.0.1"], env)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["vmid"] == 777 and payload["name"] == "golden"
    assert build_edges.establish_agent_ssh.call_args.args[2].endswith("id_ed25519")


def test_template_build_agent_verify_recovers_after_retry(fake_client, creds, tmp_path, build_edges):
    _wire_build_client(fake_client)
    fake_client.agent_network_interfaces.side_effect = [RuntimeError("not yet"), {"result": []}]
    r = inv(["--json", "--dangerous", "template", "build", "ubuntu-24.04", "--node", "pve1",
             "--ip", "192.168.0.198/24,gw=192.168.0.1", "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 0, r.output
    assert fake_client.agent_network_interfaces.call_count == 2


def test_template_build_waits_for_guest_to_stop(fake_client, creds, tmp_path, build_edges):
    _wire_build_client(fake_client)
    fake_client.guest_status.side_effect = [{"status": "running"}, {"status": "stopped"}]
    r = inv(["--json", "--dangerous", "template", "build", "ubuntu-24.04", "--node", "pve1",
             "--ip", "192.168.0.198/24,gw=192.168.0.1", "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 0, r.output
    assert fake_client.guest_status.call_count == 2


def test_template_build_stop_wait_times_out(fake_client, creds, tmp_path, build_edges, monkeypatch):
    _wire_build_client(fake_client)
    fake_client.guest_status.return_value = {"status": "running"}
    ticks = iter([float(i) for i in range(0, 5000, 100)])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticks))
    r = inv(["--json", "--dangerous", "template", "build", "ubuntu-24.04", "--node", "pve1",
             "--ip", "192.168.0.198/24,gw=192.168.0.1", "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 1, r.output
    assert json.loads(r.output)["failed_step"] == "shutdown"


def test_template_build_agent_verify_retries_then_times_out(fake_client, creds, tmp_path, build_edges, monkeypatch):
    _wire_build_client(fake_client)
    fake_client.agent_network_interfaces.side_effect = RuntimeError("not running")
    ticks = iter([float(i) for i in range(0, 2000, 100)])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticks))
    r = inv(["--json", "--dangerous", "template", "build", "ubuntu-24.04", "--node", "pve1",
             "--ip", "192.168.0.198/24,gw=192.168.0.1", "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 1, r.output
    assert json.loads(r.output)["failed_step"] == "verify agent"


def test_template_list(fake_client, creds):
    fake_client.cluster_resources.return_value = [
        dict(TPL_ROW),
        {"vmid": 9002, "name": "plain", "node": "pve1", "type": "qemu", "template": 1, "tags": ""},
        {"vmid": 100, "name": "web", "node": "pve1", "type": "qemu", "template": 0},
    ]
    r = inv(["--json", "template", "list"], creds)
    assert r.exit_code == 0, r.output
    rows = json.loads(r.output)
    assert [row["vmid"] for row in rows] == [9001, 9002]
    assert rows[0]["agent"] is True and rows[1]["agent"] is False

    r = inv(["--json", "template", "list", "--node", "pve9"], creds)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == []


def test_template_list_table(fake_client, creds):
    fake_client.cluster_resources.return_value = [dict(TPL_ROW)]
    r = inv(["--no-json", "template", "list"], creds)
    assert r.exit_code == 0, r.output
    assert "agent-ubuntu-24-04" in r.output


# ------------------------------------------------------ vm up default flow --


def _wire_up_client(fake_client, with_template=True):
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.storage_content.return_value = []
    fake_client.cluster_resources.return_value = [dict(TPL_ROW)] if with_template else []
    fake_client.clone_guest.return_value = "UPID:clone"
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.download_url.return_value = "UPID:dl"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    return fake_client


def test_vm_up_uses_agent_template_when_present(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    _wire_up_client(fake_client)
    r = inv(["--json", "--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04",
             "--node", "pve1", "--size", "medium", "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["ok"] is True and payload["op"] == "qemu.up"
    assert payload["template"] == 9001 and payload["agent"] is True
    assert payload["template_built"] is False
    fake_client.clone_guest.assert_called_once()
    fake_client.create_guest.assert_not_called()
    # the requested size profile still lands on the clone
    update = next(c for c in fake_client.update_config.call_args_list if "cores" in c.kwargs)
    assert update.kwargs["cores"] == 2 and update.kwargs["memory"] == 4096
    assert "guest agent" in payload["hint"]


def test_vm_up_builds_template_when_missing(fake_client, creds, tmp_path, monkeypatch):
    """The one-shot flow: first `vm up --image X` builds the agent template, then clones it."""
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    _wire_up_client(fake_client, with_template=False)
    fake_client.cluster_nextid.side_effect = ["150", "151"]
    built = MagicMock(return_value={"vmid": 9001, "name": "agent-ubuntu-24-04",
                                    "tags": "pmox-agent;img-ubuntu-24.04", "user": "ubuntu"})
    monkeypatch.setattr(cli, "_agent_template_build", built)
    r = inv(["--json", "--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04",
             "--node", "pve1", "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["template"] == 9001 and payload["agent"] is True
    assert payload["template_built"] is True
    assert built.call_args.kwargs["image"] == "ubuntu-24.04"
    assert built.call_args.kwargs["node"] == "pve1"
    # the clone targets a fresh VMID fetched after the build consumed the first
    assert payload["vmid"] == 151
    assert fake_client.clone_guest.call_args.kwargs["newid"] == 151
    fake_client.create_guest.assert_not_called()


def test_vm_up_build_announces_progress_in_human_mode(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    _wire_up_client(fake_client, with_template=False)
    monkeypatch.setattr(cli, "_agent_template_build",
                        lambda *a, **k: {"vmid": 9001, "name": "agent-ubuntu-24-04",
                                         "tags": "pmox-agent;img-ubuntu-24.04", "user": "ubuntu"})
    r = inv(["--no-json", "--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04",
             "--node", "pve1", "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 0, r.output
    assert "building one now" in r.output


def test_vm_up_falls_back_to_plain_image_without_login_user(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    _wire_up_client(fake_client, with_template=False)
    monkeypatch.setattr(cli, "_agent_template_build",
                        lambda *a, **k: pytest.fail("build attempted without a login user"))
    r = inv(["--json", "--dangerous", "vm", "up", "web", "--image", "https://x.test/img.qcow2",
             "--node", "pve1", "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["agent"] is False and payload["template_built"] is False
    assert "template build" in payload["hint"]
    fake_client.create_guest.assert_called_once()
    fake_client.clone_guest.assert_not_called()


def test_vm_up_no_agent_template_flag_forces_plain(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    _wire_up_client(fake_client)
    r = inv(["--json", "--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04",
             "--node", "pve1", "--no-agent-template", "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 0, r.output
    fake_client.create_guest.assert_called_once()
    fake_client.clone_guest.assert_not_called()


def test_vm_up_setting_disables_template_use(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    _wire_up_client(fake_client)
    env = dict(creds, PMOX_AGENT_TEMPLATES="0")
    r = inv(["--json", "--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04",
             "--node", "pve1", "--ssh-key", _keypair(tmp_path)], env)
    assert r.exit_code == 0, r.output
    fake_client.create_guest.assert_called_once()
    fake_client.clone_guest.assert_not_called()


def test_vm_up_template_on_other_node_triggers_local_build(fake_client, creds, tmp_path, monkeypatch):
    """A template on another node doesn't count — clones must be node-local, so
    the one-shot flow builds a fresh template on the target node."""
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    _wire_up_client(fake_client)
    fake_client.cluster_resources.return_value = [dict(TPL_ROW, node="pve2")]
    built = MagicMock(return_value={"vmid": 7777, "name": "agent-ubuntu-24-04",
                                    "tags": "pmox-agent;img-ubuntu-24.04", "user": "ubuntu"})
    monkeypatch.setattr(cli, "_agent_template_build", built)
    r = inv(["--json", "--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04",
             "--node", "pve1", "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["template"] == 7777
    assert built.call_args.kwargs["node"] == "pve1"
    fake_client.clone_guest.assert_called_once()
    fake_client.create_guest.assert_not_called()


def test_vm_up_dry_run_reports_agent_template(fake_client, creds, tmp_path):
    _wire_up_client(fake_client)
    r = inv(["--dry-run", "vm", "up", "web", "--image", "ubuntu-24.04",
             "--node", "pve1", "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["agent_template"] == 9001
    assert payload["plan"][0]["op"] == "clone_guest"
    fake_client.clone_guest.assert_not_called()


def test_vm_up_dry_run_when_template_missing_says_build_plus_clone(fake_client, creds, tmp_path):
    _wire_up_client(fake_client, with_template=False)
    r = inv(["--dry-run", "vm", "up", "web", "--image", "ubuntu-24.04",
             "--node", "pve1", "--ssh-key", _keypair(tmp_path)], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["agent_template"] is None
    assert payload["agent_template_action"] == "build+clone"
    assert "template build" in payload["hint"]
    fake_client.create_guest.assert_not_called()
    fake_client.clone_guest.assert_not_called()
