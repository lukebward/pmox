# pmox Phase C3: LXC Container Provisioning — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ready-to-SSH **container** provisioning: `pmox --dangerous ct new box --template ubuntu-24.04 --ssh-key ~/.ssh/id.pub --ip dhcp` resolves/downloads the LXC template and creates the container with the SSH key injected — plus `image list --ct` / `image pull --ct` for browsing/fetching container templates.

**Architecture:** Reuses the C1 `provision` plan/execute engine. `client.py` gains `list_appliances` (GET `aplinfo`) and `download_appliance` (POST `aplinfo`). `provision.py` gains `build_ct_plan`, which resolves the template from the **live** appliance list by substring match (filenames version over time), downloads it to a `vztmpl` storage if absent, then creates the LXC with `ostemplate`/`rootfs`/`ssh-public-keys`/`net0`. `ct new` is added to the guest factory under a `kind == "lxc"` guard (mirroring the QEMU-only `vm new`). `image list`/`image pull` gain a `--ct` mode.

**Tech Stack:** Python 3.11+, Typer, proxmoxer (mocked), Rich, pytest + pytest-cov.

**Source of truth:** spec §6.3 (`ct new`), §6.4 (`image --ct`), §3.1 (`list_appliances`/`download_appliance`); research-confirmed: `GET/POST /nodes/{node}/aplinfo`; LXC create via `POST /nodes/{node}/lxc` with `ostemplate=<storage>:vztmpl/<file>`, `rootfs=<storage>:<GB>`, `ssh-public-keys` (hyphenated; proxmoxer form-encodes the raw value), `net0=name=eth0,bridge=vmbr0,ip=dhcp`. No cloud-init drive needed for LXC.

---

## Conventions for every task

- **Focused runs:** `.venv\Scripts\python.exe -m pytest tests/test_x.py::test_name -v --no-cov`
- **Before each commit:** `.venv\Scripts\python.exe -m pytest` (must reach 100%).
- Work in place in `C:\Users\Luke\Workspace\pmox` on `feat/agent-native-commands`. No worktree.
- Help text that varies by guest kind uses `help=f"..."` in the decorator (not a `{label}` docstring). `ct new` is LXC-only with a static help string.

---

## Task 1: `client.list_appliances` + `download_appliance`

**Files:** Modify `pmox/client.py`; Test `tests/test_client.py`.

- [ ] **Step 1: Write the failing tests**

```python
def test_list_appliances(client, api):
    api.nodes.return_value.aplinfo.get.return_value = [{"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    out = client.list_appliances("pve1")
    assert out == [{"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    api.nodes.assert_called_with("pve1")
    api.nodes.return_value.aplinfo.get.assert_called_once_with()


def test_download_appliance(client, api):
    api.nodes.return_value.aplinfo.post.return_value = "UPID:apl"
    out = client.download_appliance("pve1", "local", "ubuntu-24.04-standard_24.04-2_amd64.tar.zst")
    assert out == "UPID:apl"
    api.nodes.return_value.aplinfo.post.assert_called_once_with(
        storage="local", template="ubuntu-24.04-standard_24.04-2_amd64.tar.zst"
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_client.py -k "appliance" -v --no-cov`
Expected: FAIL with `AttributeError: 'ProxmoxClient' object has no attribute 'list_appliances'`

- [ ] **Step 3: Implement** — in `pmox/client.py`, add to the storage section (after `download_url`):

```python
    def list_appliances(self, node: str) -> list:
        """List downloadable appliance/container templates available to a node."""
        return self._api.nodes(node).aplinfo.get()

    def download_appliance(self, node: str, storage: str, template: str) -> Any:
        """Download a container template to a vztmpl storage (async; returns a UPID)."""
        return self._api.nodes(node).aplinfo.post(storage=storage, template=template)
```

- [ ] **Step 4: Run focused tests** → PASS.
- [ ] **Step 5: Full suite + commit**

```bash
git add pmox/client.py tests/test_client.py
git commit -m "Add client.list_appliances and download_appliance (LXC templates)"
```

---

## Task 2: `provision.build_ct_plan`

**Files:** Modify `pmox/provision.py`; Test `tests/test_provision.py`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

(Ensure `import pytest` is present in `tests/test_provision.py`; add it if missing.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_provision.py -k build_ct -v --no-cov`
Expected: FAIL with `AttributeError: module 'pmox.provision' has no attribute 'build_ct_plan'`

- [ ] **Step 3: Implement** — add to `pmox/provision.py`:

```python
def _resolve_appliance(client, node, template_storage, template):
    """Return (ostemplate_volid, download_step_or_None) for a CT template name/volid."""
    if ":vztmpl/" in template:
        return template, None
    matches = [a for a in client.list_appliances(node) if template in a.get("template", "")]
    if not matches:
        raise LookupError(f"No container template matching {template!r} available on {node}.")
    filename = matches[0]["template"]
    volid = f"{template_storage}:vztmpl/{filename}"
    present = any(c.get("volid") == volid for c in client.storage_content(node, template_storage))
    if present:
        return volid, None
    download = step(
        "download_appliance",
        {"node": node, "storage": template_storage, "template": filename},
        await_task=True,
        describe=f"download template {filename}",
    )
    return volid, download


def build_ct_plan(
    client,
    *,
    node,
    vmid,
    hostname,
    template,
    storage,
    template_storage,
    disk,
    cores,
    memory,
    sshkeys=None,
    ip="dhcp",
    password=None,
    start=True,
) -> list:
    """Build the plan for creating a ready-to-use LXC container."""
    ostemplate, download = _resolve_appliance(client, node, template_storage, template)
    plan = []
    if download:
        plan.append(download)

    create_args = {
        "node": node,
        "kind": "lxc",
        "vmid": vmid,
        "ostemplate": ostemplate,
        "rootfs": f"{storage}:{disk}",
        "cores": cores,
        "memory": memory,
        "net0": f"name=eth0,bridge=vmbr0,ip={ip}",
        "unprivileged": 1,
    }
    if hostname:
        create_args["hostname"] = hostname
    if sshkeys:
        create_args["ssh-public-keys"] = sshkeys
    if password:
        create_args["password"] = password
    plan.append(step("create_guest", create_args, await_task=True, describe=f"create CT {vmid}"))

    if start:
        plan.append(step("guest_power", {"node": node, "kind": "lxc", "vmid": vmid, "action": "start"}, await_task=True, describe="start"))
    return plan
```

- [ ] **Step 4: Run focused tests** → PASS.
- [ ] **Step 5: Full suite + commit**

```bash
git add pmox/provision.py tests/test_provision.py
git commit -m "Add provision.build_ct_plan (LXC provisioning with template resolution)"
```

---

## Task 3: `ct new` command

**Files:** Modify `pmox/cli.py` (add `new` to the factory under `kind == "lxc"`); Test `tests/test_cli.py`.

- [ ] **Step 1: Write the failing tests**

```python
def test_ct_new_creates_container(fake_client, creds, tmp_path, monkeypatch):
    import pmox.cli as cli
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAA user@host")
    fake_client.list_appliances.return_value = [{"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    fake_client.storage_content.return_value = []
    fake_client.download_appliance.return_value = "UPID:apl"
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "ct", "new", "box", "--template", "ubuntu-24.04", "--node", "pve1",
             "--vmid", "300", "--disk", "8", "--ssh-key", str(key), "--ip", "dhcp"], creds)
    assert r.exit_code == 0, r.output
    create_kwargs = fake_client.create_guest.call_args.kwargs
    assert create_kwargs["ostemplate"] == "local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst"
    assert create_kwargs["rootfs"] == "local-lvm:8"
    assert create_kwargs["ssh-public-keys"] == "ssh-ed25519 AAAA user@host"
    assert create_kwargs["hostname"] == "box"


def test_ct_new_dry_run(fake_client, creds):
    fake_client.list_appliances.return_value = [{"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    fake_client.storage_content.return_value = []
    r = inv(["--dry-run", "ct", "new", "box", "--template", "ubuntu-24.04", "--node", "pve1", "--vmid", "300"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["op"] == "lxc.new"
    assert payload["plan"][-1]["op"] in {"create_guest", "guest_power"}
    fake_client.create_guest.assert_not_called()


def test_ct_new_needs_dangerous(fake_client, creds):
    fake_client.list_appliances.return_value = [{"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    fake_client.storage_content.return_value = []
    r = inv(["ct", "new", "box", "--template", "ubuntu-24.04", "--node", "pve1", "--vmid", "300"], creds)
    assert r.exit_code == 4, r.output
    fake_client.create_guest.assert_not_called()


def test_vm_has_no_ct_template_option(fake_client, creds):
    # `vm new` must NOT accept --template (that's ct-only)
    r = inv(["--dangerous", "vm", "new", "web", "--template", "ubuntu-24.04", "--node", "pve1", "--vmid", "300"], creds)
    assert r.exit_code != 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -k "ct_new or vm_has_no_ct" -v --no-cov`
Expected: FAIL (`No such command 'new'` for ct, or `No such option: --template`)

- [ ] **Step 3: Implement** — in `pmox/cli.py`'s `build_guest_app`, add a `kind == "lxc"` block (mirroring the `kind == "qemu"` `new` block). Place it near the `qemu` `new` block:

```python
    if kind == "lxc":

        @group.command("new", help="Create an LXC container from a template, ready to SSH.")
        def _ct_new(
            ctx: typer.Context,
            name: Optional[str] = typer.Argument(None, help="Hostname (optional)."),
            template: str = typer.Option(..., "--template", help="Template: catalog/aplinfo name or a vztmpl volid."),
            size: str = typer.Option("small", "--size", help="Sizing profile: small | medium | large."),
            disk: int = typer.Option(8, "--disk", help="Root filesystem size in GiB."),
            storage: str = typer.Option("local-lvm", "--storage", help="Storage for the rootfs."),
            template_storage: str = typer.Option("local", "--template-storage", help="Storage to download the template into (vztmpl)."),
            node: Optional[str] = typer.Option(None, "--node", "-n", help="Node (auto-picked if one node)."),
            vmid: Optional[int] = typer.Option(None, "--vmid", help="VMID (auto-assigned if omitted)."),
            ssh_key: Optional[List[str]] = typer.Option(None, "--ssh-key", help="Path to an SSH public key file (repeatable)."),
            ip: str = typer.Option("dhcp", "--ip", help="dhcp or <cidr>,gw=<ip>."),
            password: Optional[str] = typer.Option(None, "--password", help="Root password."),
        ):
            with error_boundary(ctx.obj.json):
                client = _get_client(ctx)
                target_node = node or _single_node_or_die(client)
                target_vmid = vmid if vmid is not None else int(client.cluster_nextid())
                profile = catalog.size_params(size)
                sshkeys = "\n".join(Path(p).read_text().strip() for p in (ssh_key or [])) or None
                plan = provision.build_ct_plan(
                    client,
                    node=target_node,
                    vmid=target_vmid,
                    hostname=name,
                    template=template,
                    storage=storage,
                    template_storage=template_storage,
                    disk=disk,
                    cores=profile["cores"],
                    memory=profile["memory"],
                    sshkeys=sshkeys,
                    ip=ip,
                    password=password,
                    start=True,
                )
                if ctx.obj.dry_run:
                    print(json.dumps({"dry_run": True, "op": "lxc.new", "node": target_node, "plan": plan}, default=str, indent=2))
                    return
                require_dangerous(ctx.obj.dangerous)
                provision.execute_plan(client, target_node, plan, waiter=lambda n, upid: _maybe_wait(ctx, n, upid))
                _ok(ctx, f"Created container {target_vmid} on {target_node} from {template}")
```

- [ ] **Step 4: Run focused tests** → PASS. Re-run the existing vm new tests (`-k vm_new`) to confirm `--template` is still rejected by `vm new` (it should be — the qemu block has no `--template`).
- [ ] **Step 5: Full suite + commit**

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Add ct new (LXC container provisioning)"
```

---

## Task 4: `image list --ct` and `image pull --ct`

**Files:** Modify `pmox/cli.py` (extend `image_list` + `image_pull` with `--ct`); Test `tests/test_cli.py`.

- [ ] **Step 1: Write the failing tests**

```python
def test_image_list_ct(fake_client, creds):
    fake_client.list_appliances.return_value = [
        {"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst", "type": "lxc"},
    ]
    r = inv(["--json", "image", "list", "--ct", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    assert any("ubuntu-24.04" in row["template"] for row in json.loads(r.output))
    fake_client.list_appliances.assert_called_once_with("pve1")


def test_image_pull_ct_downloads(fake_client, creds, monkeypatch):
    import pmox.cli as cli
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.list_appliances.return_value = [{"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    fake_client.storage_content.return_value = []
    fake_client.download_appliance.return_value = "UPID:apl"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "image", "pull", "ubuntu-24.04", "--ct", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    fake_client.download_appliance.assert_called_once_with("pve1", "local", "ubuntu-24.04-standard_24.04-2_amd64.tar.zst")


def test_image_pull_ct_needs_dangerous(fake_client, creds):
    fake_client.list_appliances.return_value = [{"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    fake_client.storage_content.return_value = []
    r = inv(["image", "pull", "ubuntu-24.04", "--ct", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 4, r.output
    fake_client.download_appliance.assert_not_called()


def test_image_pull_ct_cached_skips(fake_client, creds):
    fake_client.list_appliances.return_value = [{"template": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    fake_client.storage_content.return_value = [{"volid": "local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst"}]
    r = inv(["--dangerous", "image", "pull", "ubuntu-24.04", "--ct", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    fake_client.download_appliance.assert_not_called()


def test_image_pull_ct_unknown_template(fake_client, creds):
    fake_client.list_appliances.return_value = [{"template": "debian-12-standard_12.7-1_amd64.tar.zst"}]
    r = inv(["--dangerous", "image", "pull", "ubuntu-24.04", "--ct", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 1, r.output
    fake_client.download_appliance.assert_not_called()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -k "image_list_ct or image_pull_ct" -v --no-cov`
Expected: FAIL (`No such option: --ct`)

- [ ] **Step 3: Implement** — in `pmox/cli.py`:

For `image_list`, add a `--ct` option and branch:

```python
@image_app.command("list")
def image_list(
    ctx: typer.Context,
    ct: bool = typer.Option(False, "--ct", help="List LXC container templates (live, from the node) instead of the VM catalog."),
    node: Optional[str] = typer.Option(None, "--node", "-n", help="Node (required with --ct)."),
):
    """List VM cloud images (catalog) or, with --ct, container templates from a node."""
    with error_boundary(ctx.obj.json):
        if ct:
            if not node:
                raise ValueError("--ct requires --node.")
            client = _get_client(ctx)
            emit(client.list_appliances(node), json_output=ctx.obj.json, title="Container templates")
            return
        rows = [{"name": name, "url": e["url"], "filename": e["filename"]} for name, e in catalog.IMAGE_CATALOG.items()]
        emit(rows, json_output=ctx.obj.json, title="Image catalog")
```

For `image_pull`, add a `--ct` option and a branch that resolves + downloads a container template (it shares the `as_template`/plain-download structure; place the `--ct` branch first, after the volid-reject check which only applies to VM URLs). Add `ct: bool = typer.Option(False, "--ct", ...)` to the signature and:

```python
        if ct:
            matches = [a for a in client.list_appliances(node) if image in a.get("template", "")]
            if not matches:
                raise ValueError(f"No container template matching {image!r} on {node}.")
            filename = matches[0]["template"]
            volid = f"{storage}:vztmpl/{filename}"
            if ctx.obj.dry_run:
                print(json.dumps({"dry_run": True, "op": "image.pull.ct", "node": node, "params": {"volid": volid, "template": filename}}, default=str, indent=2))
                return
            require_dangerous(ctx.obj.dangerous)
            if any(c.get("volid") == volid for c in client.storage_content(node, storage)):
                _ok(ctx, f"Template already present: {volid}")
                return
            upid = client.download_appliance(node, storage, filename)
            _maybe_wait(ctx, node, upid)
            _ok(ctx, f"Pulled container template {image} -> {volid}", upid)
            return
```

> Place the `--ct` branch BEFORE the `spec = catalog.resolve_image(image)` / volid-reject logic, because `--ct` resolves via `aplinfo`, not the VM image catalog. The `as_template` and plain-download branches that follow are unchanged.

- [ ] **Step 4: Run focused tests + existing image tests** → PASS:

`.venv\Scripts\python.exe -m pytest tests/test_cli.py -k image -v --no-cov`

- [ ] **Step 5: Full suite + commit**

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Add image list --ct and image pull --ct (container templates)"
```

---

## Self-review (completed during planning)

- **Spec coverage:** §3.1 `list_appliances`/`download_appliance` (T1) ✓; §6.3 `ct new` (template resolution via live aplinfo + substring match, download-if-absent, LXC create with ostemplate/rootfs/ssh-public-keys/net0, profiles/auto-vmid/node-auto-pick) (T2, T3) ✓; §6.4 `image list --ct`/`image pull --ct` (T4) ✓.
- **Research baked in:** live `aplinfo` substring match (filenames version over time); LXC `ssh-public-keys` passed raw (proxmoxer form-encodes); no cloud-init drive for LXC; `unprivileged=1` default.
- **Name/type consistency:** `client.list_appliances(node)` / `download_appliance(node, storage, template)`; `provision._resolve_appliance` / `build_ct_plan(...)`; CLI ops `lxc.new` / `image.pull.ct`. LXC create args via the engine's `**`-dispatch include the hyphenated `ssh-public-keys` key (valid as a dict key).
- **Kind separation:** `ct new` is added under `kind == "lxc"` in the factory; `vm new` (qemu block) has no `--template`, so `vm new --template` errors (tested). `ct` already had all generic commands (set/resize/describe/rename/tag) from B1/B2 via the factory.
- **Coverage discipline:** appliance download/cached/explicit-volid/unknown (T2); ct new create/dry-run/needs-dangerous + vm-rejects-template (T3); image list --ct (+ --ct-requires-node), pull --ct download/needs-dangerous/cached/unknown (T4).
- **Safety:** `ct new` and `image pull --ct` are `--dangerous`-gated; `image list --ct` is an ungated read; `--dry-run` prints the plan with zero mutations (tests assert create_guest/download_appliance not called).
```
