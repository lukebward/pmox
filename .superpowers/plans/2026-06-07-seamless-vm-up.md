# Seamless VM creation (`vm up`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a token-only `pmox vm up` command that creates a ready-to-SSH VM with an auto-allocated static IP, and fix `vm new --image` so it stops failing on LVM-thin storage.

**Architecture:** Three composable, separately-tested capabilities — import-storage routing (`provision.resolve_import_storage`), a cluster-ledger IPAM (`pmox/ipam.py`), and SSH-key resolution (`provision.ensure_ssh_key`) — wired into a new `vm up` CLI command. `vm new` shares the import-storage fix. Everything uses the existing API-token client; no SSH to nodes or guests.

**Tech Stack:** Python 3.11+, Typer (CLI), `ipaddress` (stdlib), pytest + pytest-cov (100% line-coverage gate). Tests run via `.venv/Scripts/python -m pytest`.

---

## File Structure

- `pmox/ipam.py` *(new)* — IPv4 allocation: `static_ips_from_ipconfig`, `parse_pool_range`, `allocate_ip`. Pure functions over the injected client.
- `pmox/provision.py` *(modify)* — add `resolve_import_storage` and `ensure_ssh_key`; thread an `import_storage` argument through `_resolve_image_volid` / `build_vm_image_plan`; reject installer-ISO volids.
- `pmox/config.py` *(modify)* — add `[network]` + `[defaults]` settings (file + env).
- `pmox/cli.py` *(modify)* — resolve import storage in `vm new`; add the `vm up` command.
- `README.md`, `plugin/skills/proxmox/SKILL.md` *(modify)* — document `vm up` and the import behavior.
- Tests: `tests/test_provision.py`, `tests/test_config.py`, `tests/test_ipam.py` *(new)*, `tests/test_cli.py`.

**Test command conventions:**
- Single test (fast, no gate): `.venv/Scripts/python -m pytest <path>::<name> --no-cov -q`
- Full suite + 100% coverage gate: `.venv/Scripts/python -m pytest`

---

# PHASE 1 — Import-storage routing (fixes `vm new` on lvmthin)

## Task 1: `resolve_import_storage`

**Files:**
- Modify: `pmox/provision.py`
- Test: `tests/test_provision.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_provision.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_provision.py -k resolve_import_storage --no-cov -q`
Expected: FAIL with `AttributeError: module 'pmox.provision' has no attribute 'resolve_import_storage'`.

- [ ] **Step 3: Implement `resolve_import_storage`**

Add to `pmox/provision.py` (after the `build_ipconfig` function, near the top):

```python
def resolve_import_storage(client, node: str, explicit=None) -> str:
    """Pick a file-based storage that can hold imported cloud-image disks.

    A token can't enable storage content types, so when none is available we
    raise an actionable error instead of guessing.
    """
    storages = client.list_storage(node)
    if explicit:
        match = next((s for s in storages if s.get("storage") == explicit), None)
        if match is None:
            raise LookupError(f"Storage {explicit!r} not found on {node}.")
        if "import" not in str(match.get("content", "")).split(","):
            raise RuntimeError(
                f"Storage {explicit!r} on {node} lacks the 'import' content type. "
                f"Enable it: Datacenter -> Storage -> {explicit} -> Edit -> check 'Import', "
                f"or `pvesm set {explicit} --content <existing>,import`."
            )
        return explicit
    candidates = [s for s in storages if "import" in str(s.get("content", "")).split(",")]
    if not candidates:
        raise RuntimeError(
            f"No storage on {node} has the 'import' content type, required to import a "
            f"cloud image. Enable it on a directory storage: Datacenter -> Storage -> "
            f"<storage> -> Edit -> check 'Import', or `pvesm set <storage> --content <existing>,import`."
        )
    candidates.sort(key=lambda s: (s.get("plugintype") != "dir", s.get("storage", "")))
    return candidates[0]["storage"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_provision.py -k resolve_import_storage --no-cov -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add pmox/provision.py tests/test_provision.py
git commit -m "feat(provision): resolve_import_storage — pick/validate an import-capable storage"
```

---

## Task 2: Thread `import_storage` through the image plan + reject ISO volids

**Files:**
- Modify: `pmox/provision.py:47-93` (`_resolve_image_volid`, `build_vm_image_plan`)
- Test: `tests/test_provision.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_provision.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_provision.py -k "routes_import or rejects_iso" --no-cov -q`
Expected: FAIL — `routes_import` fails with a `TypeError: ... unexpected keyword argument 'import_storage'`; `rejects_iso` fails because no exception is raised.

- [ ] **Step 3: Implement the changes**

In `pmox/provision.py`, replace `_resolve_image_volid` (currently lines 47-70):

```python
def _resolve_image_volid(client, node: str, import_storage: str, image: str):
    """Return (volid, download_step_or_None) for a --image argument.

    ``import_storage`` is the file-based storage that holds the downloaded image;
    the VM's disk lives on a separate (possibly lvmthin) storage.
    """
    spec = catalog.resolve_image(image)
    if spec["kind"] == "volid":
        if ":iso/" in spec["volid"]:
            raise ValueError(
                f"{spec['volid']!r} is an installer ISO, not a cloud image. Use a cloud image: "
                f"a catalog name (e.g. 'ubuntu-24.04'), an https URL, or an import volid."
            )
        return spec["volid"], None
    volid = f"{import_storage}:import/{spec['filename']}"
    present = any(c.get("volid") == volid for c in client.storage_content(node, import_storage))
    if present:
        return volid, None
    download = step(
        "download_url",
        {
            "node": node,
            "storage": import_storage,
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
```

Then in `build_vm_image_plan`, add the `import_storage` parameter and use it. Change the signature line `disk,` / `storage,` block to include `import_storage=None,` (add it right after `storage,`), and change the first body line from:

```python
    volid, download = _resolve_image_volid(client, node, storage, image)
```

to:

```python
    volid, download = _resolve_image_volid(client, node, import_storage or storage, image)
```

(The disk and cloud-init drive still use `storage`; only the import volid uses `import_storage`. When `import_storage` is None — e.g. `build_template_plan` — it falls back to `storage`, preserving today's behavior.)

- [ ] **Step 4: Run the full suite (existing image/template tests must stay green)**

Run: `.venv/Scripts/python -m pytest tests/test_provision.py --no-cov -q`
Expected: PASS (all provision tests, including the unchanged `test_build_vm_image_plan_downloads_when_absent` which passes `storage="local"` and no `import_storage`).

- [ ] **Step 5: Commit**

```bash
git add pmox/provision.py tests/test_provision.py
git commit -m "feat(provision): route image import to a separate storage; reject ISO volids"
```

---

## Task 3: Wire `vm new` to resolve import storage (+ `--import-storage`)

**Files:**
- Modify: `pmox/cli.py:808-888` (the `_new` command, image branch)
- Test: `tests/test_cli.py` (update 4 existing tests; add 3 new)

- [ ] **Step 1: Update existing `--image` tests + write new failing tests**

In `tests/test_cli.py`, add this constant near the top (after `runner = CliRunner()`):

```python
_IMPORT_STORAGES = [
    {"storage": "local", "content": "import,iso,vztmpl,backup", "plugintype": "dir"},
    {"storage": "local-lvm", "content": "images,rootdir", "plugintype": "lvmthin"},
]
```

Update the four existing image tests to mock `list_storage` and expect the import on `local`:

In `test_vm_new_image_creates_cloudinit_vm`, after `fake_client.storage_content.return_value = []` add:
```python
    fake_client.list_storage.return_value = _IMPORT_STORAGES
```
and change the assertion line:
```python
    assert "import-from=local-lvm:import/noble-server-cloudimg-amd64.qcow2" in create_kwargs["scsi0"]
```
to:
```python
    assert "import-from=local:import/noble-server-cloudimg-amd64.qcow2" in create_kwargs["scsi0"]
```
(`ide2 == "local-lvm:cloudinit"` stays — the disk/cloud-init drive remain on `--storage`.)

In `test_vm_new_image_dry_run_prints_plan`, `test_vm_new_image_needs_dangerous`, and `test_vm_new_image_passes_o_options`, add after their `fake_client.storage_content.return_value = []` line:
```python
    fake_client.list_storage.return_value = _IMPORT_STORAGES
```

Add three new tests at the end of the `Task 5 (C1): vm new --image` section:

```python
def test_vm_new_image_auto_routes_off_lvmthin(fake_client, creds, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.storage_content.return_value = []
    fake_client.download_url.return_value = "UPID:dl"
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "vm", "new", "web", "--image", "ubuntu-24.04",
             "--node", "pve1", "--vmid", "150", "--storage", "local-lvm"], creds)
    assert r.exit_code == 0, r.output
    # download went to the file storage, not the lvmthin disk storage
    assert fake_client.download_url.call_args.kwargs["storage"] == "local"


def test_vm_new_image_explicit_import_storage(fake_client, creds, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.storage_content.return_value = []
    fake_client.download_url.return_value = "UPID:dl"
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "vm", "new", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--vmid", "150", "--import-storage", "local"], creds)
    assert r.exit_code == 0, r.output
    assert fake_client.download_url.call_args.kwargs["storage"] == "local"


def test_vm_new_image_no_import_storage_errors(fake_client, creds):
    fake_client.list_storage.return_value = [
        {"storage": "local-lvm", "content": "images,rootdir", "plugintype": "lvmthin"},
    ]
    r = inv(["--dangerous", "vm", "new", "web", "--image", "ubuntu-24.04", "--node", "pve1", "--vmid", "150"], creds)
    assert r.exit_code == 1, r.output
    fake_client.create_guest.assert_not_called()
```

- [ ] **Step 2: Run the image tests to verify the new ones fail**

Run: `.venv/Scripts/python -m pytest tests/test_cli.py -k "vm_new_image" --no-cov -q`
Expected: FAIL — the `--import-storage` option doesn't exist yet (`no such option`), and auto-routing still uses `local-lvm`.

- [ ] **Step 3: Implement the `vm new` wiring**

In `pmox/cli.py`, add the `import_storage` option to the `_new` signature (right after the `storage` option, line 814):

```python
            import_storage: Optional[str] = typer.Option(None, "--import-storage", help="Storage to hold the imported image (default: auto-detect one with 'import' content)."),
```

In the `if image:` branch (around line 862), insert the resolver before `plan = provision.build_vm_image_plan(`:

```python
                if image:
                    sshkeys = "\n".join(Path(p).read_text().strip() for p in (ssh_key or [])) or None
                    needs_import = catalog.resolve_image(image)["kind"] != "volid"
                    resolved_import = (
                        provision.resolve_import_storage(client, target_node, import_storage)
                        if needs_import else None
                    )
                    plan = provision.build_vm_image_plan(
                        client,
                        node=target_node,
                        vmid=target_vmid,
                        name=name,
                        cores=profile["cores"],
                        memory=profile["memory"],
                        disk=disk,
                        storage=storage,
                        import_storage=resolved_import,
                        image=image,
                        sshkeys=sshkeys,
                        ipconfig=provision.build_ipconfig(ip) if ip else None,
                        ciuser=ciuser,
                        cipassword=cipassword,
                        nameserver=nameserver,
                        start=True,
                        extra=parse_options(option),
                    )
```

(Leave the rest of the branch — `dry_run`, `require_dangerous`, `execute_plan`, `_ok` — unchanged.)

- [ ] **Step 4: Run the full suite + coverage gate**

Run: `.venv/Scripts/python -m pytest`
Expected: PASS, `Required test coverage of 100% reached.`

- [ ] **Step 5: Commit**

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "feat(cli): vm new auto-routes image import to a file storage (--import-storage)"
```

---

# PHASE 2 — Config + IPAM

## Task 4: `[network]` / `[defaults]` config

**Files:**
- Modify: `pmox/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py` (it already imports `textwrap`, `load_settings`):

```python
def test_network_and_defaults_from_file(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(textwrap.dedent(
        """
        [proxmox]
        host = "h"
        [network]
        cidr = "192.168.0.0/24"
        gateway = "192.168.0.1"
        pool = "192.168.0.200-192.168.0.250"
        nameserver = "1.1.1.1"
        [defaults]
        import_storage = "local"
        ssh_key = "~/.ssh/id_ed25519.pub"
        ciuser = "ubuntu"
        """
    ))
    s = load_settings(env={}, config_path=cfg)
    assert s.host == "h"
    assert s.net_cidr == "192.168.0.0/24"
    assert s.net_gateway == "192.168.0.1"
    assert s.net_pool == "192.168.0.200-192.168.0.250"
    assert s.net_nameserver == "1.1.1.1"
    assert s.default_import_storage == "local"
    assert s.default_ssh_key == "~/.ssh/id_ed25519.pub"
    assert s.default_ciuser == "ubuntu"


def test_network_env_overrides_file(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[network]\ncidr = "10.0.0.0/24"\n')
    s = load_settings(env={"PROXMOX_NET_CIDR": "192.168.5.0/24"}, config_path=cfg)
    assert s.net_cidr == "192.168.5.0/24"


def test_network_unset_defaults_none(tmp_path):
    s = load_settings(env={}, config_path=tmp_path / "none.toml")
    assert s.net_cidr is None
    assert s.default_ciuser is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_config.py -k "network" --no-cov -q`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'net_cidr'`.

- [ ] **Step 3: Implement the config changes**

In `pmox/config.py`:

(a) Add env constants after `ENV_TIMEOUT` (line 27):

```python
ENV_NET_CIDR = "PROXMOX_NET_CIDR"
ENV_NET_GATEWAY = "PROXMOX_NET_GATEWAY"
ENV_NET_POOL = "PROXMOX_NET_POOL"
ENV_NET_NAMESERVER = "PROXMOX_NET_NAMESERVER"
ENV_DEFAULT_IMPORT_STORAGE = "PROXMOX_DEFAULT_IMPORT_STORAGE"
ENV_DEFAULT_SSH_KEY = "PROXMOX_DEFAULT_SSH_KEY"
ENV_DEFAULT_CIUSER = "PROXMOX_DEFAULT_CIUSER"
```

(b) Add fields to the `Settings` dataclass (after `timeout: int = DEFAULT_TIMEOUT`, line 49):

```python
    net_cidr: Optional[str] = None
    net_gateway: Optional[str] = None
    net_pool: Optional[str] = None
    net_nameserver: Optional[str] = None
    default_import_storage: Optional[str] = None
    default_ssh_key: Optional[str] = None
    default_ciuser: Optional[str] = None
```

(c) Change `_load_config_file` to return the full document — replace its final lines:

```python
    # Accept either top-level keys or a [proxmox] table.
    if isinstance(data.get("proxmox"), dict):
        return data["proxmox"]
    return data
```

with:

```python
    return data
```

(d) Replace `_from_file` (lines 117-128) with:

```python
def _from_file(data: dict) -> dict:
    conn = data["proxmox"] if isinstance(data.get("proxmox"), dict) else data
    out = _coerce(
        conn,
        keys={
            "host": ("host", str),
            "port": ("port", int),
            "token_id": ("token_id", str),
            "token_secret": ("token_secret", str),
            "verify_ssl": ("verify_ssl", _parse_bool),
            "timeout": ("timeout", int),
        },
    )
    out.update(_coerce(
        data.get("network") or {},
        keys={
            "net_cidr": ("cidr", str),
            "net_gateway": ("gateway", str),
            "net_pool": ("pool", str),
            "net_nameserver": ("nameserver", str),
        },
    ))
    out.update(_coerce(
        data.get("defaults") or {},
        keys={
            "default_import_storage": ("import_storage", str),
            "default_ssh_key": ("ssh_key", str),
            "default_ciuser": ("ciuser", str),
        },
    ))
    return out
```

(e) In `_from_env`, add the new env keys to the `_coerce` `keys=` dict (alongside `host`/`port`/...):

```python
            "net_cidr": (ENV_NET_CIDR, str),
            "net_gateway": (ENV_NET_GATEWAY, str),
            "net_pool": (ENV_NET_POOL, str),
            "net_nameserver": (ENV_NET_NAMESERVER, str),
            "default_import_storage": (ENV_DEFAULT_IMPORT_STORAGE, str),
            "default_ssh_key": (ENV_DEFAULT_SSH_KEY, str),
            "default_ciuser": (ENV_DEFAULT_CIUSER, str),
```

- [ ] **Step 4: Run the full suite + coverage gate**

Run: `.venv/Scripts/python -m pytest`
Expected: PASS (existing config tests still pass because they all go through `load_settings`), 100% coverage.

- [ ] **Step 5: Commit**

```bash
git add pmox/config.py tests/test_config.py
git commit -m "feat(config): add [network] pool and [defaults] settings (file + env)"
```

---

## Task 5: `pmox/ipam.py` — cluster-ledger IP allocation

**Files:**
- Create: `pmox/ipam.py`
- Test: `tests/test_ipam.py` *(new)*

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ipam.py`:

```python
from unittest.mock import MagicMock

import pytest

from pmox import ipam


def test_static_ips_from_ipconfig_collects_statics():
    cfg = {
        "cores": 2,
        "ipconfig0": "ip=192.168.0.50/24,gw=192.168.0.1,ip6=2001:db8::5/64",
        "ipconfig1": "ip=dhcp,ip6=auto",
    }
    assert ipam.static_ips_from_ipconfig(cfg) == ["192.168.0.50", "2001:db8::5"]


def test_parse_pool_range_ok():
    assert ipam.parse_pool_range("192.168.0.200-192.168.0.250") == ("192.168.0.200", "192.168.0.250")


def test_parse_pool_range_bad():
    with pytest.raises(ValueError):
        ipam.parse_pool_range("192.168.0.200")


def _ipam_client(guests):
    c = MagicMock()
    c.cluster_resources.return_value = [{"node": "n1", "type": "qemu", "vmid": vmid} for vmid, _ in guests]
    cfgs = {vmid: cfg for vmid, cfg in guests}
    c.guest_config.side_effect = lambda node, kind, vmid: cfgs[vmid]
    return c


def test_allocate_ip_returns_lowest_free():
    c = _ipam_client([
        (100, {"ipconfig0": "ip=192.168.0.200/24,gw=192.168.0.1"}),
        (101, {"ipconfig0": "ip=192.168.0.201/24,gw=192.168.0.1"}),
    ])
    out = ipam.allocate_ip(c, cidr="192.168.0.0/24", gateway="192.168.0.1", pool="192.168.0.200-192.168.0.250")
    assert out == "192.168.0.202/24"


def test_allocate_ip_reserves_gateway():
    c = _ipam_client([])
    out = ipam.allocate_ip(c, cidr="192.168.0.0/24", gateway="192.168.0.200", pool="192.168.0.200-192.168.0.250")
    assert out == "192.168.0.201/24"


def test_allocate_ip_exhausted_raises():
    c = _ipam_client([(100, {"ipconfig0": "ip=192.168.0.200/24"})])
    with pytest.raises(RuntimeError, match="exhausted"):
        ipam.allocate_ip(c, cidr="192.168.0.0/24", gateway="192.168.0.1", pool="192.168.0.200-192.168.0.200")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_ipam.py --no-cov -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'pmox.ipam'`.

- [ ] **Step 3: Implement `pmox/ipam.py`**

```python
"""Token-only IPv4 allocation: the cluster's own static ipconfig is the ledger.

No local state file — to find a free address we read every guest's static
``ipconfigN`` across the cluster. The pool MUST sit outside the DHCP scope, since
we cannot see DHCP leases or non-Proxmox hosts.
"""

from __future__ import annotations

import ipaddress


def static_ips_from_ipconfig(config) -> list:
    """Static addresses declared in a guest's cloud-init ``ipconfigN`` keys."""
    ips: list = []
    for key, value in config.items():
        if not str(key).startswith("ipconfig"):
            continue
        fields = dict(p.split("=", 1) for p in str(value).split(",") if "=" in p)
        for fkey in ("ip", "ip6"):
            cidr = fields.get(fkey)
            if not cidr or cidr in ("dhcp", "auto", "manual"):
                continue
            ips.append(cidr.split("/", 1)[0])
    return ips


def parse_pool_range(pool: str):
    """Split ``'START-END'`` into (start, end) address strings."""
    start, sep, end = pool.partition("-")
    if not sep:
        raise ValueError(f"pool must be 'START-END' (got {pool!r}).")
    return start.strip(), end.strip()


def allocate_ip(client, *, cidr: str, gateway: str, pool: str) -> str:
    """Return the lowest free address in ``pool`` as ``'<ip>/<prefixlen>'``.

    Used addresses = every guest's static ipconfigN across the cluster + the
    gateway. Raises ``RuntimeError`` if the pool is exhausted.
    """
    network = ipaddress.ip_network(cidr, strict=False)
    start_s, end_s = parse_pool_range(pool)
    start = ipaddress.ip_address(start_s)
    end = ipaddress.ip_address(end_s)

    used = {gateway}
    for row in client.cluster_resources(type="vm"):
        cfg = client.guest_config(row.get("node"), row.get("type"), row.get("vmid"))
        used.update(static_ips_from_ipconfig(cfg))

    candidate = start
    while candidate <= end:
        if str(candidate) not in used:
            return f"{candidate}/{network.prefixlen}"
        candidate += 1
    raise RuntimeError(f"IP pool {pool} is exhausted; every address is in use.")
```

- [ ] **Step 4: Run the full suite + coverage gate**

Run: `.venv/Scripts/python -m pytest`
Expected: PASS, 100% coverage (new `pmox/ipam.py` fully covered).

- [ ] **Step 5: Commit**

```bash
git add pmox/ipam.py tests/test_ipam.py
git commit -m "feat(ipam): cluster-ledger static IPv4 allocation"
```

---

# PHASE 3 — SSH key + `vm up`

## Task 6: `ensure_ssh_key`

**Files:**
- Modify: `pmox/provision.py` (add `import subprocess`, `from pathlib import Path`, and the function)
- Test: `tests/test_provision.py`

- [ ] **Step 1: Write the failing tests**

Add to the top imports of `tests/test_provision.py`:

```python
from pathlib import Path
from types import SimpleNamespace
```

Add the tests:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_provision.py -k ensure_ssh_key --no-cov -q`
Expected: FAIL with `AttributeError: module 'pmox.provision' has no attribute 'ensure_ssh_key'`.

- [ ] **Step 3: Implement `ensure_ssh_key`**

At the top of `pmox/provision.py`, add imports below `from urllib.parse import quote`:

```python
import subprocess
from pathlib import Path
```

Add the function (near `encode_sshkeys`):

```python
def ensure_ssh_key(path: str) -> str:
    """Return the public-key text at ``path``; generate an ed25519 keypair if absent."""
    pub = Path(path).expanduser()
    if not pub.exists():
        if pub.suffix != ".pub":
            raise ValueError(f"ssh key path must end in .pub to generate (got {path!r}).")
        priv = pub.with_suffix("")
        priv.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(priv), "-N", "", "-C", "pmox"],
            check=True, capture_output=True, text=True,
        )
    return pub.read_text().strip()
```

- [ ] **Step 4: Run the full suite + coverage gate**

Run: `.venv/Scripts/python -m pytest`
Expected: PASS, 100% coverage.

- [ ] **Step 5: Commit**

```bash
git add pmox/provision.py tests/test_provision.py
git commit -m "feat(provision): ensure_ssh_key — read or generate an ed25519 key"
```

---

## Task 7: `pmox vm up` command

**Files:**
- Modify: `pmox/cli.py` (import `ipam`; add the `_up` command under `if kind == "qemu":`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py` (near `_IMPORT_STORAGES`):

```python
def _net_creds(creds):
    return {**creds, "PROXMOX_NET_CIDR": "192.168.0.0/24",
            "PROXMOX_NET_GATEWAY": "192.168.0.1",
            "PROXMOX_NET_POOL": "192.168.0.200-192.168.0.250"}
```

Add the tests (new section `vm up command`):

```python
def test_vm_up_allocates_ip_and_creates(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.cluster_resources.return_value = []
    fake_client.storage_content.return_value = []
    fake_client.download_url.return_value = "UPID:dl"
    fake_client.create_guest.return_value = "UPID:create"
    fake_client.guest_power.return_value = "UPID:start"
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", str(key), "--ciuser", "ubuntu"], _net_creds(creds))
    assert r.exit_code == 0, r.output
    create = fake_client.create_guest.call_args.kwargs
    assert create["ipconfig0"] == "ip=192.168.0.200/24,gw=192.168.0.1"
    assert "import-from=local:import/noble-server-cloudimg-amd64.qcow2" in create["scsi0"]
    assert create["ide2"] == "local-lvm:cloudinit"
    assert "192.168.0.200" in r.output and "ssh ubuntu@192.168.0.200" in r.output


def test_vm_up_json_output(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.cluster_resources.return_value = []
    fake_client.storage_content.return_value = []
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--json", "--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", str(key), "--ciuser", "ubuntu"], _net_creds(creds))
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["ip"] == "192.168.0.200"
    assert out["ssh"] == "ssh ubuntu@192.168.0.200"


def test_vm_up_explicit_ip_skips_allocation(fake_client, creds, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.storage_content.return_value = []
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    # no [network] creds and no --ciuser: exercises the explicit-IP + default-user-hint paths
    r = inv(["--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--ip", "192.168.0.77/24,gw=192.168.0.1", "--ssh-key", str(key)], creds)
    assert r.exit_code == 0, r.output
    fake_client.cluster_resources.assert_not_called()
    assert fake_client.create_guest.call_args.kwargs["ipconfig0"] == "ip=192.168.0.77/24,gw=192.168.0.1"
    assert "192.168.0.77" in r.output and "image's default user" in r.output


def test_vm_up_no_pool_configured_errors(fake_client, creds, tmp_path):
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.cluster_nextid.return_value = "150"
    r = inv(["--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", str(key)], creds)  # creds has no [network]
    assert r.exit_code == 1, r.output
    fake_client.create_guest.assert_not_called()


def test_vm_up_dry_run(fake_client, creds, tmp_path):
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.cluster_resources.return_value = []
    fake_client.storage_content.return_value = []
    r = inv(["--dry-run", "vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", str(key)], _net_creds(creds))
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["op"] == "qemu.up"
    fake_client.create_guest.assert_not_called()


def test_vm_up_needs_dangerous(fake_client, creds, tmp_path):
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA u@h")
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.cluster_resources.return_value = []
    fake_client.storage_content.return_value = []
    r = inv(["vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--ssh-key", str(key)], _net_creds(creds))
    assert r.exit_code == 4, r.output
    fake_client.create_guest.assert_not_called()


def test_vm_up_no_ssh_key(fake_client, creds, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    fake_client.cluster_nextid.return_value = "150"
    fake_client.list_storage.return_value = _IMPORT_STORAGES
    fake_client.cluster_resources.return_value = []
    fake_client.storage_content.return_value = []
    fake_client.task_status.return_value = {"status": "stopped", "exitstatus": "OK"}
    r = inv(["--dangerous", "vm", "up", "web", "--image", "ubuntu-24.04", "--node", "pve1",
             "--no-ssh-key", "--ciuser", "ubuntu"], _net_creds(creds))
    assert r.exit_code == 0, r.output
    assert "sshkeys" not in fake_client.create_guest.call_args.kwargs
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_cli.py -k vm_up --no-cov -q`
Expected: FAIL — `vm up` is not a command yet (`No such command 'up'`).

- [ ] **Step 3: Implement the `vm up` command**

In `pmox/cli.py`, add `ipam` to the package import (line 33):

```python
from . import __version__, catalog, ipam, provision, views
```

Inside the `if kind == "qemu":` block, immediately after the `_new` command definition (after line 906), add:

```python
        @group.command("up", help="Create a ready-to-SSH VM with an auto-allocated static IP (seamless, token-only).")
        def _up(
            ctx: typer.Context,
            name: str = typer.Argument(..., help="VM name."),
            image: str = typer.Option(..., "--image", help="Cloud image: catalog name, https URL, or import volid."),
            size: str = typer.Option("small", "--size", help="Sizing profile: small | medium | large."),
            disk: Optional[int] = typer.Option(None, "--disk", help="Disk size in GiB."),
            node: Optional[str] = typer.Option(None, "--node", "-n", help="Node (auto-picked if one node)."),
            storage: str = typer.Option("local-lvm", "--storage", help="Storage for the disk/cloud-init."),
            import_storage: Optional[str] = typer.Option(None, "--import-storage", help="Storage to hold the imported image (default: auto-detect)."),
            ip: Optional[str] = typer.Option(None, "--ip", help="Static <cidr>,gw=<ip> to use instead of auto-allocating."),
            ssh_key: Optional[str] = typer.Option(None, "--ssh-key", help="SSH public key path (default ~/.ssh/id_ed25519.pub; generated if missing)."),
            no_ssh_key: bool = typer.Option(False, "--no-ssh-key", help="Don't attach or generate an SSH key."),
            ciuser: Optional[str] = typer.Option(None, "--ciuser", help="Cloud-init user (default from config)."),
            vmid: Optional[int] = typer.Option(None, "--vmid", help="VMID (auto-assigned if omitted)."),
        ):
            with error_boundary(ctx.obj.json):
                client = _get_client(ctx)
                settings = ctx.obj.settings
                target_node = node or _single_node_or_die(client)
                target_vmid = vmid if vmid is not None else int(client.cluster_nextid())
                profile = catalog.size_params(size)

                if ip:
                    ipconfig = provision.build_ipconfig(ip)
                    chosen_ip = ip.split(",", 1)[0].split("/", 1)[0]
                else:
                    if not (settings.net_cidr and settings.net_gateway and settings.net_pool):
                        raise ValueError(
                            "vm up needs a static-IP pool. Set [network] cidr/gateway/pool in your "
                            "pmox config (or PROXMOX_NET_CIDR/GATEWAY/POOL), or pass --ip explicitly."
                        )
                    allocated = ipam.allocate_ip(
                        client, cidr=settings.net_cidr, gateway=settings.net_gateway, pool=settings.net_pool
                    )
                    ipconfig = f"ip={allocated},gw={settings.net_gateway}"
                    chosen_ip = allocated.split("/", 1)[0]

                needs_import = catalog.resolve_image(image)["kind"] != "volid"
                resolved_import = (
                    provision.resolve_import_storage(client, target_node, import_storage or settings.default_import_storage)
                    if needs_import else None
                )

                sshkeys = None
                if not no_ssh_key:
                    key_path = ssh_key or settings.default_ssh_key or str(Path.home() / ".ssh" / "id_ed25519.pub")
                    sshkeys = provision.ensure_ssh_key(key_path)

                chosen_ciuser = ciuser or settings.default_ciuser
                plan = provision.build_vm_image_plan(
                    client, node=target_node, vmid=target_vmid, name=name,
                    cores=profile["cores"], memory=profile["memory"], disk=disk,
                    storage=storage, import_storage=resolved_import, image=image,
                    sshkeys=sshkeys, ipconfig=ipconfig, ciuser=chosen_ciuser,
                    cipassword=None, nameserver=settings.net_nameserver, start=True,
                )
                if ctx.obj.dry_run:
                    print(json.dumps({"dry_run": True, "op": "qemu.up", "node": target_node, "plan": plan}, default=str, indent=2))
                    return
                require_dangerous(ctx.obj.dangerous)
                provision.execute_plan(client, target_node, plan, waiter=lambda n, upid: _maybe_wait(ctx, n, upid))

                ssh_hint = f"ssh {chosen_ciuser}@{chosen_ip}" if chosen_ciuser else None
                if ctx.obj.json:
                    emit({"vmid": target_vmid, "name": name, "node": target_node, "ip": chosen_ip, "ssh": ssh_hint}, json_output=True)
                else:
                    console.print(f"VM {target_vmid}  {name}  ip {chosen_ip}")
                    if chosen_ciuser:
                        console.print(f"ssh {chosen_ciuser}@{chosen_ip}")
                    else:
                        console.print(f"ssh <image's default user>@{chosen_ip}")
```

- [ ] **Step 4: Run the full suite + coverage gate**

Run: `.venv/Scripts/python -m pytest`
Expected: PASS, `Required test coverage of 100% reached.`

- [ ] **Step 5: Commit**

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "feat(cli): vm up — seamless token-only ready-to-SSH VM with auto static IP"
```

---

## Task 8: Document `vm up`

**Files:**
- Modify: `README.md`, `plugin/skills/proxmox/SKILL.md`

- [ ] **Step 1: Add `vm up` to the README**

In `README.md`, in the cheat-sheet "Change (need `--dangerous`)" area (near the `vm new` line), add:

```
pmox vm up <name> --image <name>     ready-to-SSH VM, auto static IP (needs [network] config)
```

And add a short subsection after the provisioning examples:

```markdown
### Seamless one-shot VM (`vm up`)

With a static-IP pool configured once:

​```toml
[network]
cidr = "192.168.0.0/24"
gateway = "192.168.0.1"
pool = "192.168.0.200-192.168.0.250"   # MUST be outside your DHCP scope
​```

​```
pmox --dangerous vm up web --image ubuntu-24.04 --wait
​```

pmox allocates the lowest free address in the pool (by scanning existing static
`ipconfigN` across the cluster), routes the image import to a file-based storage,
ensures an SSH key (generating `~/.ssh/id_ed25519.pub` if absent), and prints the
IP + `ssh` command. No guest agent required.
```

- [ ] **Step 2: Add `vm up` to the skill doc**

In `plugin/skills/proxmox/SKILL.md`, add a recipe after "One-call cloud-init VM":

```markdown
### Seamless VM with an auto-allocated static IP

```
pmox --dangerous vm up web --image ubuntu-24.04 --wait
```

Requires a `[network]` pool in config (`cidr`/`gateway`/`pool`, the pool outside
your DHCP scope). pmox allocates a free static IP from the pool, auto-routes the
image import to a storage with the `import` content type, and ensures an SSH key.
`pmox vm ip <vmid>` returns the address immediately (read from cloud-init config —
no guest agent needed).
```

And in the cheat-sheet "Change" block, add:

```
pmox --dangerous vm up <name> --image <name|url|volid>
                               [--size ...] [--disk <GiB>] [--node <node>]
                               [--storage <disk>] [--import-storage <storage>]
                               [--ip <cidr>,gw=<ip>] [--ssh-key <path>] [--no-ssh-key]
                               [--ciuser <u>] [--wait] [--dry-run]
```

- [ ] **Step 3: Commit**

```bash
git add README.md plugin/skills/proxmox/SKILL.md
git commit -m "docs: document vm up and the import-storage behavior"
```

---

## Self-Review (completed during planning)

- **Spec coverage:** Component A (IPAM) → Task 5; Component B (import routing) → Tasks 1–3; Component C (ssh key) → Task 6; Component D (config) → Task 4; Component E (`vm up`) → Task 7; `vm ip` fallback → already done (Phase 0); errors (no pool / no import storage / exhausted / ISO) → Tasks 1, 2, 5, 7; docs → Task 8. All covered.
- **Type/signature consistency:** `resolve_import_storage(client, node, explicit=None)`, `build_vm_image_plan(..., import_storage=None, ...)`, `ensure_ssh_key(path)`, `ipam.allocate_ip(client, *, cidr, gateway, pool)`, `Settings.net_cidr/net_gateway/net_pool/net_nameserver/default_*` — used identically across tasks.
- **Coverage gate:** each task adds code together with tests that exercise every new line; the full-suite step at each task end enforces `--cov-fail-under=100`.

---

## Execution Handoff

Two execution options:

1. **Subagent-Driven (recommended)** — a fresh subagent per task, reviewed between tasks.
2. **Inline Execution** — execute tasks in this session with checkpoints.
