# pmox Phase C2: Golden Templates + Clone — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the idiomatic Proxmox golden-template workflow: build a reusable cloud-init template once (`pmox image pull <image> --as-template`), then stamp out ready-to-SSH VMs from it fast (`pmox vm new web --from-template <id> --ssh-key … --ip dhcp`).

**Architecture:** Reuses the C1 `provision` plan/execute engine. `client.py` gains one method (`convert_to_template`). `provision.py` gains `build_vm_clone_plan` (clone → set cloud-init → resize → start) and `build_template_plan` (which reuses `build_vm_image_plan` with `start=False` and no user cloud-init keys, then appends a `convert_to_template` step). The existing `vm new` gains a `--from-template` mode (mutually exclusive with `--image`); `image pull` gains an `--as-template` mode. Clones inherit the template's cores/memory/disk; resize/`vm set` adjust afterward.

**Tech Stack:** Python 3.11+, Typer, proxmoxer (mocked), Rich, pytest + pytest-cov.

**Source of truth:** spec §6.2 (`--from-template`), §6.4 (`image pull --as-template`), §3.1 (`convert_to_template`); research-confirmed: `POST /qemu/{vmid}/template` (sync), `POST /qemu/{vmid}/clone` (async UPID, `full=1` for independent disks), cloud-init keys set on the clone via config (metadata → PUT sync is fine).

---

## Conventions for every task

- **Focused runs:** `.venv\Scripts\python.exe -m pytest tests/test_x.py::test_name -v --no-cov`
- **Before each commit:** `.venv\Scripts\python.exe -m pytest` (must reach 100%).
- Work in place in `C:\Users\Luke\Workspace\pmox` on `feat/agent-native-commands`. No worktree.

---

## Task 1: `client.convert_to_template`

**Files:** Modify `pmox/client.py`; Test `tests/test_client.py`.

- [ ] **Step 1: Write the failing test**

```python
def test_convert_to_template(client, api):
    client.convert_to_template("pve1", "qemu", 9000)
    api.nodes.return_value.qemu.return_value.template.post.assert_called_once_with()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_client.py::test_convert_to_template -v --no-cov`
Expected: FAIL with `AttributeError: 'ProxmoxClient' object has no attribute 'convert_to_template'`

- [ ] **Step 3: Implement** — in `pmox/client.py`, add to the guest section (after `clone_guest`):

```python
    def convert_to_template(self, node: str, kind: str, vmid) -> Any:
        """Convert a stopped guest into a template (one-way)."""
        return self._guest(node, kind, vmid).template.post()
```

- [ ] **Step 4: Run focused test** → PASS.
- [ ] **Step 5: Full suite + commit**

```bash
git add pmox/client.py tests/test_client.py
git commit -m "Add client.convert_to_template"
```

---

## Task 2: `provision.build_vm_clone_plan`

**Files:** Modify `pmox/provision.py`; Test `tests/test_provision.py`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_provision.py -k build_vm_clone -v --no-cov`
Expected: FAIL with `AttributeError: module 'pmox.provision' has no attribute 'build_vm_clone_plan'`

- [ ] **Step 3: Implement** — add to `pmox/provision.py`:

```python
def build_vm_clone_plan(
    client,
    *,
    node,
    template_id,
    newid,
    name,
    disk,
    sshkeys=None,
    ipconfig=None,
    ciuser=None,
    cipassword=None,
    nameserver=None,
    full=True,
    start=True,
) -> list:
    """Build the plan for cloning a template into a ready-to-SSH VM."""
    clone_args = {"node": node, "kind": "qemu", "vmid": template_id, "newid": newid}
    if name:
        clone_args["name"] = name
    if full:
        clone_args["full"] = 1
    plan = [step("clone_guest", clone_args, await_task=True, describe=f"clone {template_id} -> {newid}")]

    ci = {}
    if sshkeys:
        ci["sshkeys"] = encode_sshkeys(sshkeys)
    if ipconfig:
        ci["ipconfig0"] = ipconfig
    if ciuser:
        ci["ciuser"] = ciuser
    if cipassword:
        ci["cipassword"] = cipassword
    if nameserver:
        ci["nameserver"] = nameserver
    if ci:
        plan.append(step("update_config", {"node": node, "kind": "qemu", "vmid": newid, **ci}, await_task=False, describe="set cloud-init"))

    if disk:
        plan.append(step("resize_disk", {"node": node, "kind": "qemu", "vmid": newid, "disk": "scsi0", "size": f"{disk}G"}, await_task=False, describe=f"grow scsi0 to {disk}G"))
    if start:
        plan.append(step("guest_power", {"node": node, "kind": "qemu", "vmid": newid, "action": "start"}, await_task=True, describe="start"))
    return plan
```

- [ ] **Step 4: Run focused tests** → PASS.
- [ ] **Step 5: Full suite + commit**

```bash
git add pmox/provision.py tests/test_provision.py
git commit -m "Add provision.build_vm_clone_plan (template clone + cloud-init)"
```

---

## Task 3: `provision.build_template_plan`

**Files:** Modify `pmox/provision.py`; Test `tests/test_provision.py`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_provision.py -k build_template -v --no-cov`
Expected: FAIL with `AttributeError: module 'pmox.provision' has no attribute 'build_template_plan'`

- [ ] **Step 3: Implement** — add to `pmox/provision.py`. It reuses `build_vm_image_plan` (no user cloud-init keys, `start=False`) then appends the convert step:

```python
def build_template_plan(client, *, node, vmid, name, storage, image, cores=1, memory=1024) -> list:
    """Build a golden cloud-init template: import the image + cloud-init drive, then convert."""
    plan = build_vm_image_plan(
        client,
        node=node,
        vmid=vmid,
        name=name,
        cores=cores,
        memory=memory,
        disk=None,
        storage=storage,
        image=image,
        start=False,
    )
    plan.append(step("convert_to_template", {"node": node, "kind": "qemu", "vmid": vmid}, await_task=False, describe="convert to template"))
    return plan
```

- [ ] **Step 4: Run focused tests** → PASS.
- [ ] **Step 5: Full suite + commit**

```bash
git add pmox/provision.py tests/test_provision.py
git commit -m "Add provision.build_template_plan (golden cloud-init template)"
```

---

## Task 4: `vm new --from-template` mode

**Files:** Modify `pmox/cli.py` (extend `_new`); Test `tests/test_cli.py`.

- [ ] **Step 1: Write the failing tests**

```python
def test_vm_new_from_template_clones_and_sets_ci(fake_client, creds, tmp_path, monkeypatch):
    import pmox.cli as cli
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAA user@host")
    fake_client.resolve_node.return_value = "pve1"
    fake_client.clone_guest.return_value = "UPID:clone"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "vm", "new", "web", "--from-template", "9000", "--vmid", "120",
             "--ssh-key", str(key), "--ip", "dhcp", "--ciuser", "ubuntu"], creds)
    assert r.exit_code == 0, r.output
    fake_client.clone_guest.assert_called_once_with(node="pve1", kind="qemu", vmid=9000, newid=120, name="web", full=1)
    ci = fake_client.update_config.call_args.kwargs
    assert ci["ciuser"] == "ubuntu" and ci["ipconfig0"] == "ip=dhcp"


def test_vm_new_from_template_dry_run(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["--dry-run", "vm", "new", "web", "--from-template", "9000", "--vmid", "120"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["op"] == "qemu.new.from_template"
    assert payload["plan"][0]["op"] == "clone_guest"
    fake_client.clone_guest.assert_not_called()


def test_vm_new_image_and_template_mutually_exclusive(fake_client, creds):
    r = inv(["--dangerous", "vm", "new", "web", "--image", "ubuntu-24.04", "--from-template", "9000", "--node", "pve1"], creds)
    assert r.exit_code == 1, r.output


def test_vm_new_from_template_needs_dangerous(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    r = inv(["vm", "new", "web", "--from-template", "9000", "--vmid", "120"], creds)
    assert r.exit_code == 4, r.output
    fake_client.clone_guest.assert_not_called()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -k "from_template or mutually_exclusive" -v --no-cov`
Expected: FAIL (`No such option: --from-template`)

- [ ] **Step 3: Implement** — in `pmox/cli.py`, modify `_new`. Add the option and a branch. Add to the signature (after `image`):

```python
            from_template: Optional[int] = typer.Option(None, "--from-template", help="Clone an existing template VMID into a cloud-init VM. Cloud-init mode."),
```

Restructure the body so VMID is resolved first, then the three modes branch (the existing `--image` and blank paths are unchanged except for where `target_node` is computed). Replace the body from `client = _get_client(ctx)` down to the image branch with:

```python
            with error_boundary(ctx.obj.json):
                client = _get_client(ctx)
                if image and from_template:
                    raise ValueError("--image and --from-template are mutually exclusive.")
                target_vmid = vmid if vmid is not None else int(client.cluster_nextid())

                if from_template:
                    target_node = node or client.resolve_node(from_template)
                    if not target_node:
                        raise LookupError(f"Could not locate template {from_template} in the cluster.")
                    sshkeys = "\n".join(Path(p).read_text().strip() for p in (ssh_key or [])) or None
                    plan = provision.build_vm_clone_plan(
                        client,
                        node=target_node,
                        template_id=from_template,
                        newid=target_vmid,
                        name=name,
                        disk=disk,
                        sshkeys=sshkeys,
                        ipconfig=provision.build_ipconfig(ip) if ip else None,
                        ciuser=ciuser,
                        cipassword=cipassword,
                        nameserver=nameserver,
                        start=True,
                    )
                    if ctx.obj.dry_run:
                        print(json.dumps({"dry_run": True, "op": "qemu.new.from_template", "node": target_node, "plan": plan}, default=str, indent=2))
                        return
                    require_dangerous(ctx.obj.dangerous)
                    provision.execute_plan(client, target_node, plan, waiter=lambda n, upid: _maybe_wait(ctx, n, upid))
                    _ok(ctx, f"Cloned template {from_template} -> VM {target_vmid} on {target_node}")
                    return

                target_node = node or _single_node_or_die(client)

                if image:
                    # ... existing C1 image branch, unchanged, EXCEPT it now uses the
                    # target_vmid/target_node computed above (it already did) ...
```

> Important: the existing `--image` branch and blank-shell branch stay exactly as they are; only their preceding `target_node`/`target_vmid` resolution moves above. Make sure `target_vmid` (computed once at the top) and `target_node = node or _single_node_or_die(client)` (for image/blank) are still in scope for those branches. Re-run the full C1 + B2 `vm new` tests to confirm no regression.

- [ ] **Step 4: Run focused tests + the existing vm new tests** → PASS:

`.venv\Scripts\python.exe -m pytest tests/test_cli.py -k "vm_new or from_template or ct_has_no_new" -v --no-cov`

- [ ] **Step 5: Full suite + commit**

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Add vm new --from-template clone mode"
```

---

## Task 5: `image pull --as-template` mode

**Files:** Modify `pmox/cli.py` (extend `image_pull`); Test `tests/test_cli.py`.

- [ ] **Step 1: Write the failing tests**

```python
def test_image_pull_as_template_builds_template(fake_client, creds, monkeypatch):
    import pmox.cli as cli
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.storage_content.return_value = []
    fake_client.download_url.return_value = "UPID:dl"
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "image", "pull", "ubuntu-24.04", "--storage", "local", "--node", "pve1",
             "--as-template", "--vmid", "9000"], creds)
    assert r.exit_code == 0, r.output
    fake_client.create_guest.assert_called_once()
    fake_client.convert_to_template.assert_called_once_with(node="pve1", kind="qemu", vmid=9000)


def test_image_pull_as_template_dry_run(fake_client, creds):
    fake_client.storage_content.return_value = []
    r = inv(["--dry-run", "image", "pull", "ubuntu-24.04", "--storage", "local", "--node", "pve1",
             "--as-template", "--vmid", "9000"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["op"] == "image.pull.template"
    assert payload["plan"][-1]["op"] == "convert_to_template"
    fake_client.create_guest.assert_not_called()


def test_image_pull_as_template_needs_dangerous(fake_client, creds):
    fake_client.storage_content.return_value = []
    r = inv(["image", "pull", "ubuntu-24.04", "--storage", "local", "--node", "pve1", "--as-template", "--vmid", "9000"], creds)
    assert r.exit_code == 4, r.output
    fake_client.create_guest.assert_not_called()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -k "as_template" -v --no-cov`
Expected: FAIL (`No such option: --as-template`)

- [ ] **Step 3: Implement** — in `pmox/cli.py`, extend `image_pull`. Add options and an `--as-template` branch before the plain-download logic:

```python
    as_template: bool = typer.Option(False, "--as-template", help="Build a reusable golden VM template instead of just downloading."),
    vmid: Optional[int] = typer.Option(None, "--vmid", help="VMID for the template (auto-assigned if omitted). Used with --as-template."),
    name: Optional[str] = typer.Option(None, "--name", help="Name for the template. Used with --as-template."),
```

Inside the command, after `spec = catalog.resolve_image(image)` and the volid-rejection check, add:

```python
        if as_template:
            target_vmid = vmid if vmid is not None else int(client.cluster_nextid())
            plan = provision.build_template_plan(client, node=node, vmid=target_vmid, name=name, storage=storage, image=image)
            if ctx.obj.dry_run:
                print(json.dumps({"dry_run": True, "op": "image.pull.template", "node": node, "plan": plan}, default=str, indent=2))
                return
            require_dangerous(ctx.obj.dangerous)
            provision.execute_plan(client, node, plan, waiter=lambda n, upid: _maybe_wait(ctx, n, upid))
            _ok(ctx, f"Built template {target_vmid} on {node} from {image}")
            return
```

(The existing plain-download path below is unchanged.)

- [ ] **Step 4: Run focused tests + existing image tests** → PASS:

`.venv\Scripts\python.exe -m pytest tests/test_cli.py -k image -v --no-cov`

- [ ] **Step 5: Full suite + commit**

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Add image pull --as-template (build golden template)"
```

---

## Self-review (completed during planning)

- **Spec coverage:** §3.1 `convert_to_template` (T1) ✓; §6.2 `--from-template` clone + cloud-init (T2, T4) ✓; §6.4 `image pull --as-template` golden template (T3, T5) ✓. Clones inherit template size; documented for the A skill.
- **Reuse:** `build_template_plan` reuses `build_vm_image_plan(start=False)` (no user ci keys) + a convert step — no duplicated import/cloud-init-drive logic. Both new CLI modes reuse the C1 `provision.execute_plan` + `_maybe_wait` + the shared `--ssh-key`/`--ip`/`--ciuser`/`--cipassword`/`--nameserver` options.
- **Name/type consistency:** `client.convert_to_template(node, kind, vmid)`; `provision.build_vm_clone_plan(...)`/`build_template_plan(...)`; CLI ops `qemu.new.from_template` / `image.pull.template`. `clone_guest`/`update_config`/`resize_disk`/`guest_power` dispatched via the engine with all-kwargs args (matching how `execute_plan` calls them).
- **Mode exclusivity:** `--image` + `--from-template` together → exit 1 (tested). `_new` now has three modes (blank / image / from-template) with early returns; if the reviewer finds it too large, factoring the image/template branches into helpers is a reasonable follow-up (not required for C2).
- **Coverage discipline:** clone full/minimal (ci present/absent, disk, start) (T2); template build/cached (T3); from-template clone/dry-run/exclusive/needs-dangerous (T4); as-template build/dry-run/needs-dangerous (T5). update_config (PUT, sync) is `await_task=False`, so `_maybe_wait` no-ops on its `None` result.
- **Safety:** clone/template builds are `--dangerous`-gated; `--dry-run` prints the plan with zero mutations (tests assert clone_guest/create_guest not called).
```
