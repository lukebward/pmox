# pmox Phase C1: VM Cloud-Init All-in-One — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `pmox` produce a ready-to-SSH Ubuntu/Debian VM in one command — `pmox --dangerous vm new web --image ubuntu-24.04 --size small --disk 50 --ssh-key ~/.ssh/id_ed25519.pub --ip dhcp` — by fetching a cloud image to storage and creating a cloud-init VM entirely through the Proxmox REST API (no node shell).

**Architecture:** A new `provision.py` holds a small plan/execute engine plus cloud-init helpers; provisioning flows are built as an ordered list of serializable **steps** (so `--dry-run` can print the whole plan and tests can assert it) and executed by dispatching each step to the existing `ProxmoxClient`, awaiting task UPIDs between dependent steps. `catalog.py` gains a VM image catalog + resolver. `client.py` gains one thin method (`download_url`). The existing B2 `vm new` (blank shell) is extended with an `--image` mode (+ cloud-init options) that delegates to `provision`. The image is attached via `scsi0=<storage>:0,import-from=<volid>` **inside the async create call**, so no 30-second PUT timeout applies.

**Tech Stack:** Python 3.11+, Typer, proxmoxer (mocked in tests), Rich, pytest + pytest-cov.

**Source of truth:** `.superpowers/specs/2026-06-06-pmox-agent-native-design.md` (§6.1, §6.2, §6.5, §6.6) — refined by the API research below.

## Confirmed API facts this plan relies on (from research)

- `POST /nodes/{node}/storage/{storage}/download-url` with `content=import`, `filename` ending `.qcow2`, optional `checksum`/`checksum-algorithm`; returns a UPID. The `import` content type must be enabled on the storage (PVE **8.2+**, **8.4+** recommended for `.qcow2`/`.raw`).
- Disk import via **volume ID**: `scsi0=<storage>:0,import-from=<storage>:import/<file>.qcow2`, passed to the **async create** (`POST /nodes/{node}/qemu`, returns UPID). Volume-ID notation is API-token-safe; absolute paths would require `root@pam` and are therefore NOT used.
- Cloud-init keys on the create call: `ciuser`, `cipassword`, `sshkeys` (**URL-encoded**, `quote(..., safe="")`), `ipconfig0` (`ip=dhcp` or `ip=<cidr>,gw=<ip>`), `nameserver`, and the drive `ide2=<storage>:cloudinit`. Cloud images need `serial0=socket`, `vga=serial0`, `agent=1`.
- Resize after create: `PUT /nodes/{node}/qemu/{vmid}/resize` (fast, sync) — B1's `resize_disk`.

**Documented requirement (surface in Phase A's skill):** `vm new --image` needs PVE 8.2+ (8.4+ recommended) and a storage with the `import` content type enabled; pmox uses an API token, so it relies on volume-ID `import-from`.

---

## Conventions for every task

- **Focused test runs** (red/green) disable coverage: `.venv\Scripts\python.exe -m pytest tests/test_x.py::test_name -v --no-cov`
- **Before every commit**, run the full suite (enforces 100%): `.venv\Scripts\python.exe -m pytest` — must end with `Required test coverage of 100% reached`.
- Work in place in `C:\Users\Luke\Workspace\pmox` on branch `feat/agent-native-commands`. No worktree.
- Commands whose help varies by guest kind use `help=f"..."` in the decorator (not a `{label}` docstring). `vm new` is QEMU-only and uses a static help string.

---

## Task 1: VM image catalog + resolver in `catalog.py`

**Files:**
- Modify: `pmox/catalog.py` (add `IMAGE_CATALOG` + `resolve_image`)
- Test: `tests/test_catalog.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_catalog.py`:

```python
def test_resolve_image_catalog_name():
    spec = catalog.resolve_image("ubuntu-24.04")
    assert spec["kind"] == "url"
    assert spec["url"].startswith("https://")
    assert spec["filename"].endswith(".qcow2")


def test_resolve_image_url():
    spec = catalog.resolve_image("https://example.com/img/my-cloud.qcow2")
    assert spec == {
        "kind": "url",
        "url": "https://example.com/img/my-cloud.qcow2",
        "filename": "my-cloud.qcow2",
        "checksum": None,
        "algo": None,
    }


def test_resolve_image_volid():
    spec = catalog.resolve_image("local:import/foo.qcow2")
    assert spec == {"kind": "volid", "volid": "local:import/foo.qcow2"}


def test_image_catalog_filenames_are_qcow2():
    for entry in catalog.IMAGE_CATALOG.values():
        assert entry["filename"].endswith(".qcow2")
        assert entry["url"].startswith("https://")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_catalog.py -k "resolve_image or image_catalog" -v --no-cov`
Expected: FAIL with `AttributeError: module 'pmox.catalog' has no attribute 'resolve_image'`

- [ ] **Step 3: Implement**

Add to `pmox/catalog.py`:

```python
# VM cloud images by short name. `filename` ends in .qcow2 because Proxmox's
# `import` content type recognises disk images by extension (an Ubuntu .img is a
# qcow2). Checksums are optional in v1 (a warning is printed when absent) and
# need periodic refresh; the --image <url|volid> escape hatch avoids hard
# dependence on this table.
IMAGE_CATALOG = {
    "ubuntu-24.04": {
        "url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
        "filename": "noble-server-cloudimg-amd64.qcow2",
        "checksum": None,
        "algo": None,
    },
    "ubuntu-22.04": {
        "url": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
        "filename": "jammy-server-cloudimg-amd64.qcow2",
        "checksum": None,
        "algo": None,
    },
    "debian-12": {
        "url": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
        "filename": "debian-12-genericcloud-amd64.qcow2",
        "checksum": None,
        "algo": None,
    },
}


def resolve_image(image: str) -> dict:
    """Resolve a ``--image`` argument to a fetch spec.

    Returns either ``{"kind": "url", "url", "filename", "checksum", "algo"}``
    (a catalog short-name or an explicit https URL) or ``{"kind": "volid",
    "volid"}`` (an image already present on a storage).
    """
    if image in IMAGE_CATALOG:
        e = IMAGE_CATALOG[image]
        return {"kind": "url", "url": e["url"], "filename": e["filename"], "checksum": e["checksum"], "algo": e["algo"]}
    if image.startswith("http://") or image.startswith("https://"):
        return {"kind": "url", "url": image, "filename": image.rsplit("/", 1)[-1], "checksum": None, "algo": None}
    return {"kind": "volid", "volid": image}
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_catalog.py -k "resolve_image or image_catalog" -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest`

```bash
git add pmox/catalog.py tests/test_catalog.py
git commit -m "Add VM image catalog and resolve_image to catalog.py"
```

---

## Task 2: `client.download_url`

**Files:**
- Modify: `pmox/client.py` (add `download_url`)
- Test: `tests/test_client.py`

- [ ] **Step 1: Write the failing test**

```python
def test_download_url(client, api):
    seg = api.nodes.return_value.storage.return_value
    getattr(seg, "download-url").post.return_value = "UPID:dl"
    out = client.download_url(
        "pve1", "local", url="https://x/y.qcow2", content="import", filename="y.qcow2",
        checksum="abc", checksum_algorithm="sha256",
    )
    assert out == "UPID:dl"
    api.nodes.assert_called_with("pve1")
    api.nodes.return_value.storage.assert_called_with("local")
    getattr(seg, "download-url").post.assert_called_once_with(
        url="https://x/y.qcow2", content="import", filename="y.qcow2",
        checksum="abc", **{"checksum-algorithm": "sha256"},
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_client.py::test_download_url -v --no-cov`
Expected: FAIL with `AttributeError: 'ProxmoxClient' object has no attribute 'download_url'`

- [ ] **Step 3: Implement**

In `pmox/client.py`, add to the storage section (after `storage_content`):

```python
    def download_url(
        self,
        node: str,
        storage: str,
        *,
        url: str,
        content: str,
        filename: str,
        checksum: Optional[str] = None,
        checksum_algorithm: Optional[str] = None,
    ) -> Any:
        """Fetch a URL into a storage (async; returns a UPID).

        ``content`` is ``import`` for VM disk images. The hyphenated REST path
        segment ``download-url`` and parameter ``checksum-algorithm`` are
        addressed via ``getattr``/dict expansion because they aren't valid
        Python identifiers.
        """
        endpoint = getattr(self._api.nodes(node).storage(storage), "download-url")
        params: dict = {"url": url, "content": content, "filename": filename}
        if checksum is not None:
            params["checksum"] = checksum
        if checksum_algorithm is not None:
            params["checksum-algorithm"] = checksum_algorithm
        return endpoint.post(**params)
```

> Adjust the test in Step 1 if your implementation omits `checksum`/`checksum-algorithm` when `None` — the provided test passes both, so it exercises the present-path. Add a second test for the omitted path if needed for 100% coverage (see Step 4).

- [ ] **Step 4: Run focused test; add omitted-arg coverage if needed**

Run: `.venv\Scripts\python.exe -m pytest tests/test_client.py::test_download_url -v --no-cov`
Expected: PASS

The `if checksum is not None` / `if checksum_algorithm is not None` branches need both arms covered. Add:

```python
def test_download_url_without_checksum(client, api):
    seg = api.nodes.return_value.storage.return_value
    getattr(seg, "download-url").post.return_value = "UPID:dl"
    client.download_url("pve1", "local", url="https://x/y.qcow2", content="import", filename="y.qcow2")
    getattr(seg, "download-url").post.assert_called_once_with(
        url="https://x/y.qcow2", content="import", filename="y.qcow2",
    )
```

Run both: `.venv\Scripts\python.exe -m pytest tests/test_client.py -k download_url -v --no-cov` → PASS.

- [ ] **Step 5: Run full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest`

```bash
git add pmox/client.py tests/test_client.py
git commit -m "Add client.download_url for fetching cloud images to storage"
```

---

## Task 3: `provision.py` engine + cloud-init helpers

**Files:**
- Create: `pmox/provision.py`
- Test: `tests/test_provision.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_provision.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_provision.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'pmox.provision'`

- [ ] **Step 3: Implement**

Create `pmox/provision.py`:

```python
"""Provisioning orchestration: build an ordered list of API steps, then execute.

A *plan* is a list of step dicts (``op``/``args``/``await_task``/``describe``).
Splitting plan-building from execution lets ``--dry-run`` print the whole plan
and lets tests assert the plan without touching a cluster. ``execute_plan``
dispatches each step to the :class:`~pmox.client.ProxmoxClient` and waits on
task UPIDs between dependent steps.
"""

from __future__ import annotations

from urllib.parse import quote


def encode_sshkeys(text: str) -> str:
    """URL-encode SSH public keys for the QEMU ``sshkeys`` config value."""
    return quote(text, safe="")


def build_ipconfig(spec: str) -> str:
    """Turn a friendly --ip value into a Proxmox ipconfig string."""
    return "ip=dhcp" if spec == "dhcp" else f"ip={spec}"


def step(op: str, args: dict, *, await_task: bool = False, describe: str = "") -> dict:
    """Construct one plan step."""
    return {"op": op, "args": args, "await_task": await_task, "describe": describe}


def execute_plan(client, node: str, plan: list, waiter) -> list:
    """Run each step against ``client``; wait on UPID-returning steps via ``waiter``.

    ``waiter`` is a callable ``(node, upid) -> None`` (the CLI supplies one that
    polls task status with the configured timeout).
    """
    results = []
    for s in plan:
        result = getattr(client, s["op"])(**s["args"])
        if s["await_task"]:
            waiter(node, result)
        results.append(result)
    return results
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_provision.py -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest`

```bash
git add pmox/provision.py tests/test_provision.py
git commit -m "Add provision engine (step/execute_plan) and cloud-init helpers"
```

---

## Task 4: `provision.build_vm_image_plan`

**Files:**
- Modify: `pmox/provision.py` (add `build_vm_image_plan` + private `_resolve_image_volid`)
- Test: `tests/test_provision.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_provision.py -k build_vm_image -v --no-cov`
Expected: FAIL with `AttributeError: module 'pmox.provision' has no attribute 'build_vm_image_plan'`

- [ ] **Step 3: Implement**

Add to `pmox/provision.py` (import the catalog at top: `from . import catalog`):

```python
from . import catalog
```

```python
def _resolve_image_volid(client, node: str, storage: str, image: str):
    """Return (volid, download_step_or_None) for a --image argument."""
    spec = catalog.resolve_image(image)
    if spec["kind"] == "volid":
        return spec["volid"], None
    volid = f"{storage}:import/{spec['filename']}"
    present = any(c.get("volid") == volid for c in client.storage_content(node, storage))
    if present:
        return volid, None
    download = step(
        "download_url",
        {
            "node": node,
            "storage": storage,
            "url": spec["url"],
            "content": "import",
            "filename": spec["filename"],
            "checksum": spec["checksum"],
            "checksum_algorithm": spec["algo"],
        },
        await_task=True,
        describe=f"download {spec['filename']}",
    )
    return volid, download


def build_vm_image_plan(
    client,
    *,
    node,
    vmid,
    name,
    cores,
    memory,
    disk,
    storage,
    image,
    sshkeys=None,
    ipconfig=None,
    ciuser=None,
    cipassword=None,
    nameserver=None,
    start=True,
) -> list:
    """Build the ordered plan for an all-in-one cloud-init VM."""
    volid, download = _resolve_image_volid(client, node, storage, image)
    plan = []
    if download:
        plan.append(download)

    create_args = {
        "node": node,
        "kind": "qemu",
        "vmid": vmid,
        "cores": cores,
        "memory": memory,
        "scsihw": "virtio-scsi-single",
        "net0": "virtio,bridge=vmbr0",
        "ostype": "l26",
        "serial0": "socket",
        "vga": "serial0",
        "agent": 1,
        "scsi0": f"{storage}:0,import-from={volid},iothread=1",
        "ide2": f"{storage}:cloudinit",
        "boot": "order=scsi0",
    }
    if name:
        create_args["name"] = name
    if sshkeys:
        create_args["sshkeys"] = encode_sshkeys(sshkeys)
    if ipconfig:
        create_args["ipconfig0"] = ipconfig
    if ciuser:
        create_args["ciuser"] = ciuser
    if cipassword:
        create_args["cipassword"] = cipassword
    if nameserver:
        create_args["nameserver"] = nameserver
    plan.append(step("create_guest", create_args, await_task=True, describe=f"create VM {vmid} (import {volid})"))

    if disk:
        plan.append(
            step(
                "resize_disk",
                {"node": node, "kind": "qemu", "vmid": vmid, "disk": "scsi0", "size": f"{disk}G"},
                await_task=False,
                describe=f"grow scsi0 to {disk}G",
            )
        )
    if start:
        plan.append(
            step(
                "guest_power",
                {"node": node, "kind": "qemu", "vmid": vmid, "action": "start"},
                await_task=True,
                describe="start",
            )
        )
    return plan
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_provision.py -k build_vm_image -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest`

```bash
git add pmox/provision.py tests/test_provision.py
git commit -m "Add provision.build_vm_image_plan (cloud-init all-in-one)"
```

---

## Task 5: `vm new --image` mode in the CLI

**Files:**
- Modify: `pmox/cli.py` (add `provision` import; extend `_new` with `--image` + cloud-init options)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_vm_new_image_creates_cloudinit_vm(fake_client, creds, tmp_path, monkeypatch):
    import pmox.cli as cli
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)  # don't actually sleep while polling
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAA user@host")
    fake_client.storage_content.return_value = []
    fake_client.download_url.return_value = "UPID:dl"
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(
        ["--dangerous", "vm", "new", "web", "--image", "ubuntu-24.04", "--node", "pve1",
         "--vmid", "150", "--disk", "50", "--ssh-key", str(key), "--ip", "dhcp", "--ciuser", "ubuntu"],
        creds,
    )
    assert r.exit_code == 0, r.output
    fake_client.download_url.assert_called_once()
    create_kwargs = fake_client.create_guest.call_args.kwargs
    assert create_kwargs["ide2"] == "local-lvm:cloudinit"
    assert create_kwargs["ciuser"] == "ubuntu"
    assert "import-from=local-lvm:import/noble-server-cloudimg-amd64.qcow2" in create_kwargs["scsi0"]
    fake_client.resize_disk.assert_called_once_with("pve1", "qemu", 150, "scsi0", "50G")


def test_vm_new_image_dry_run_prints_plan(fake_client, creds, tmp_path):
    fake_client.storage_content.return_value = []
    r = inv(["--dry-run", "vm", "new", "web", "--image", "ubuntu-24.04", "--node", "pve1", "--vmid", "150"], creds)
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["op"] == "qemu.new.image"
    assert [s["op"] for s in payload["plan"]] == ["download_url", "create_guest", "guest_power"]
    fake_client.create_guest.assert_not_called()


def test_vm_new_image_needs_dangerous(fake_client, creds):
    fake_client.storage_content.return_value = []
    r = inv(["vm", "new", "web", "--image", "ubuntu-24.04", "--node", "pve1", "--vmid", "150"], creds)
    assert r.exit_code == 4, r.output
    fake_client.create_guest.assert_not_called()


def test_vm_new_blank_still_works(fake_client, creds):
    # no --image → the B2 blank-shell path is unchanged
    r = inv(["--dangerous", "vm", "new", "--node", "pve1", "--vmid", "150"], creds)
    assert r.exit_code == 0, r.output
    fake_client.create_guest.assert_called_once()
    assert "ide2" not in fake_client.create_guest.call_args.kwargs
```

> Sleep-patching pattern: any test that triggers task polling (`--image` create with start, `image pull`) takes `monkeypatch` and does `monkeypatch.setattr(cli.time, "sleep", lambda _s: None)` before calling `inv(...)`, matching the existing B1 wait tests. No shared helper is needed.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -k "vm_new_image or vm_new_blank" -v --no-cov`
Expected: FAIL (`No such option: --image`)

- [ ] **Step 3: Implement**

In `pmox/cli.py`, add `provision` to the package import:

```python
from . import __version__, catalog, provision, views
```

Replace the `_new` command body in `build_guest_app` (the QEMU-only block) with a version that adds the cloud-init options and an `--image` branch. The blank-shell path is preserved exactly:

```python
        @group.command("new", help="Create a VM: blank shell, or a cloud-init server with --image.")
        def _new(
            ctx: typer.Context,
            name: Optional[str] = typer.Argument(None, help="VM name (optional)."),
            size: str = typer.Option("small", "--size", help="Sizing profile: small | medium | large."),
            disk: Optional[int] = typer.Option(None, "--disk", help="Disk size in GiB."),
            storage: str = typer.Option("local-lvm", "--storage", help="Storage for the disk/cloud-init."),
            node: Optional[str] = typer.Option(None, "--node", "-n", help="Node (auto-picked if one node)."),
            vmid: Optional[int] = typer.Option(None, "--vmid", help="VMID (auto-assigned if omitted)."),
            option: Optional[List[str]] = typer.Option(None, "--option", "-o", help="Extra create param key=value."),
            image: Optional[str] = typer.Option(None, "--image", help="Cloud image (catalog name, https URL, or volid). Enables cloud-init mode."),
            ssh_key: Optional[List[str]] = typer.Option(None, "--ssh-key", help="Path to an SSH public key file (repeatable). Cloud-init mode."),
            ip: Optional[str] = typer.Option(None, "--ip", help="dhcp or <cidr>,gw=<ip>. Cloud-init mode."),
            ciuser: Optional[str] = typer.Option(None, "--ciuser", help="Cloud-init user. Cloud-init mode."),
            cipassword: Optional[str] = typer.Option(None, "--cipassword", help="Cloud-init password. Cloud-init mode."),
            nameserver: Optional[str] = typer.Option(None, "--nameserver", help="Cloud-init DNS server(s). Cloud-init mode."),
        ):
            with error_boundary(ctx.obj.json):
                client = _get_client(ctx)
                target_node = node or _single_node_or_die(client)
                target_vmid = vmid if vmid is not None else int(client.cluster_nextid())
                profile = catalog.size_params(size)

                if image:
                    sshkeys = "\n".join(Path(p).read_text().strip() for p in (ssh_key or [])) or None
                    plan = provision.build_vm_image_plan(
                        client,
                        node=target_node,
                        vmid=target_vmid,
                        name=name,
                        cores=profile["cores"],
                        memory=profile["memory"],
                        disk=disk,
                        storage=storage,
                        image=image,
                        sshkeys=sshkeys,
                        ipconfig=provision.build_ipconfig(ip) if ip else None,
                        ciuser=ciuser,
                        cipassword=cipassword,
                        nameserver=nameserver,
                        start=True,
                    )
                    if ctx.obj.dry_run:
                        print(json.dumps({"dry_run": True, "op": "qemu.new.image", "node": target_node, "plan": plan}, default=str, indent=2))
                        return
                    require_dangerous(ctx.obj.dangerous)
                    provision.execute_plan(client, target_node, plan, waiter=lambda n, upid: _maybe_wait(ctx, n, upid))
                    _ok(ctx, f"Created cloud-init VM {target_vmid} on {target_node} from {image}")
                    return

                # blank shell (B2 behavior)
                params = dict(profile)
                params.update({"scsihw": "virtio-scsi-single", "net0": "virtio,bridge=vmbr0", "ostype": "l26"})
                if name:
                    params["name"] = name
                if disk:
                    params["scsi0"] = f"{storage}:{disk},iothread=1"
                    params["boot"] = "order=scsi0"
                params.update(parse_options(option))
                _execute(
                    ctx,
                    op="qemu.new",
                    message=f"Create VM {target_vmid} on {target_node}",
                    node=target_node,
                    call=lambda: client.create_guest(target_node, "qemu", target_vmid, **params),
                    params={"vmid": target_vmid, **params},
                )
```

Ensure `Path` is imported (it already is: `from pathlib import Path`).

> The image path calls `_maybe_wait` (which polls `task_status`); the tests use `_run_with_fast_sleep` to avoid real sleeping. Note `_maybe_wait` ignores results that aren't UPIDs, so non-UPID step results are harmless.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -k "vm_new_image or vm_new_blank" -v --no-cov`
Expected: PASS. Also re-run the existing B2 `vm_new` tests to confirm no regression: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -k vm_new -v --no-cov`.

- [ ] **Step 5: Run full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest`

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Add vm new --image cloud-init mode (delegates to provision)"
```

---

## Task 6: `image` command group (list + pull)

**Files:**
- Modify: `pmox/cli.py` (add an `image` Typer group with `list` and `pull`, registered on `app`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_image_list_json(fake_client, creds):
    r = inv(["--json", "image", "list"], creds)
    assert r.exit_code == 0, r.output
    names = [row["name"] for row in json.loads(r.output)]
    assert "ubuntu-24.04" in names


def test_image_pull_downloads(fake_client, creds, monkeypatch):
    import pmox.cli as cli
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.storage_content.return_value = []
    fake_client.download_url.return_value = "UPID:dl"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "image", "pull", "ubuntu-24.04", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    fake_client.download_url.assert_called_once()
    assert fake_client.download_url.call_args.kwargs["content"] == "import"


def test_image_pull_needs_dangerous(fake_client, creds):
    r = inv(["image", "pull", "ubuntu-24.04", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 4, r.output
    fake_client.download_url.assert_not_called()


def test_image_pull_cached_skips(fake_client, creds):
    fake_client.storage_content.return_value = [{"volid": "local:import/noble-server-cloudimg-amd64.qcow2"}]
    r = inv(["--dangerous", "image", "pull", "ubuntu-24.04", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    fake_client.download_url.assert_not_called()


def test_image_pull_dry_run(fake_client, creds):
    r = inv(["--dry-run", "image", "pull", "ubuntu-24.04", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["op"] == "image.pull"
    fake_client.download_url.assert_not_called()


def test_image_pull_rejects_volid(fake_client, creds):
    r = inv(["--dangerous", "image", "pull", "local:import/x.qcow2", "--storage", "local", "--node", "pve1"], creds)
    assert r.exit_code == 1, r.output
    fake_client.download_url.assert_not_called()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -k "image_list or image_pull" -v --no-cov`
Expected: FAIL (`No such command 'image'`)

- [ ] **Step 3: Implement**

In `pmox/cli.py`, add the group near the other app sections (before `app.add_typer(...)` calls):

```python
image_app = typer.Typer(help="VM cloud images: list the catalog and pull them to storage.", no_args_is_help=True)


@image_app.command("list")
def image_list(ctx: typer.Context):
    """List the built-in VM image catalog."""
    with error_boundary(ctx.obj.json):
        rows = [{"name": name, "url": e["url"], "filename": e["filename"]} for name, e in catalog.IMAGE_CATALOG.items()]
        emit(rows, json_output=ctx.obj.json, title="Image catalog")


@image_app.command("pull")
def image_pull(
    ctx: typer.Context,
    image: str = typer.Argument(..., help="Catalog name or https URL."),
    storage: str = typer.Option(..., "--storage", help="Target storage (needs the 'import' content type)."),
    node: str = typer.Option(..., "--node", "-n", help="Node to download on."),
):
    """Download a VM cloud image to a storage (cached; needs --dangerous)."""
    with error_boundary(ctx.obj.json):
        client = _get_client(ctx)
        spec = catalog.resolve_image(image)
        if spec["kind"] == "volid":
            raise ValueError("image pull expects a catalog name or URL, not an existing volid.")
        volid = f"{storage}:import/{spec['filename']}"
        if ctx.obj.dry_run:
            print(json.dumps({"dry_run": True, "op": "image.pull", "node": node, "params": {"volid": volid, "url": spec["url"]}}, default=str, indent=2))
            return
        require_dangerous(ctx.obj.dangerous)
        if any(c.get("volid") == volid for c in client.storage_content(node, storage)):
            _ok(ctx, f"Image already present: {volid}")
            return
        upid = client.download_url(
            node, storage, url=spec["url"], content="import", filename=spec["filename"],
            checksum=spec["checksum"], checksum_algorithm=spec["algo"],
        )
        _maybe_wait(ctx, node, upid)
        _ok(ctx, f"Pulled {image} → {volid}", upid)
```

Register it with the other `app.add_typer(...)` calls:

```python
app.add_typer(image_app, name="image")
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -k "image_list or image_pull" -v --no-cov`
Expected: PASS

- [ ] **Step 5: Run full suite + commit**

Run: `.venv\Scripts\python.exe -m pytest`

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Add image list and image pull commands"
```

---

## Self-review (completed during planning)

- **Spec coverage (§6.1/§6.2/§6.5/§6.6, research-refined):** image catalog + resolver (T1) ✓; `download_url` client method (T2) ✓; `provision.py` step/execute engine + `encode_sshkeys`/`build_ipconfig` (T3) ✓; `build_vm_image_plan` all-in-one, import-at-create, cached download, serial console, ci keys (T4) ✓; `vm new --image` with `--ssh-key`/`--ip`/`--ciuser`/`--cipassword`/`--nameserver`, dry-run prints plan, dangerous-gated, blank-shell preserved (T5) ✓; `image list`/`image pull` (T6) ✓. **Deferred to C2/C3:** `--from-template`, `image pull --as-template`, `convert_to_template`, `ct new`, `aplinfo`.
- **Research baked in:** `content=import`; volume-ID `import-from` (no root-only absolute paths); import + cloud-init keys set in the **async create** (no PUT timeout); `sshkeys` URL-encoded; serial console defaults; PVE 8.2+/8.4+ + import-content-type requirement documented for Phase A.
- **Name/type consistency:** `catalog.resolve_image`/`IMAGE_CATALOG`; `client.download_url(node, storage, *, url, content, filename, checksum=None, checksum_algorithm=None)`; `provision.step/execute_plan/encode_sshkeys/build_ipconfig/build_vm_image_plan`; CLI reuses `_maybe_wait`, `_single_node_or_die`, `parse_options`, `require_dangerous`, `_execute` (blank path), `_ok`.
- **Coverage discipline:** `download_url`'s checksum present/absent both tested (T2 Step 4); `build_vm_image_plan` download/cached/volid + disk/no-disk + start/no-start + ci-keys-present/absent all exercised (T4); `vm new --image` happy/dry-run/needs-dangerous + blank-shell-unchanged (T5); `image pull` download/cached/needs-dangerous + `image list` (T6).
- **Safety:** provisioning is `--dangerous`-gated; `--dry-run` prints the full plan and performs zero mutations (verified by tests asserting `create_guest`/`download_url` not called).
```
