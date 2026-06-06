# pmox: agent-native commands, cloud-init provisioning, and skill upgrade

**Status:** approved design — ready for implementation planning
**Date:** 2026-06-06
**Scope:** layers A (skill), B (intent/edit commands), C (provisioning). The MCP server (layer D) is explicitly **out of scope**.

## 1. Motivation

An autopsy of a real session — "create one small Ubuntu VM and edit it" — exposed where `pmox` makes an agent work too hard:

- Creating one VM took ~9 CLI calls plus source reads, a failed call, and a question round. The agent re-derived the Proxmox disk/boot/net schema by copying a sibling VM.
- **Editing a guest is impossible**: there is no `set`/update command. You can create, clone, migrate, delete, power, snapshot — but not change cores/RAM, add a disk/NIC, set tags, or change boot order on an existing guest.
- There is no `--wait` (verification is manual UPID polling), no `--dry-run` (no way to preview a call before spending a `--dangerous` action), no sizing profiles, and no way to produce a *working* server (pmox can build a VM shell but cannot install an OS).
- Global flags (`--dangerous`/`--json`) must precede the subcommand; nothing warns when they don't. This was the single most common invocation error.

The root cause is a design where the **tool is a faithful thin API mirror and all intelligence is the caller's job**. The fix is to relocate that intelligence into the tool and its context. This design does that in three layers, implemented in order **B → C → A**:

- **B** — intent/edit commands and the cross-cutting ergonomics (`vm set`, `--wait`, `--dry-run`, `vm new` with profiles, `describe`, `health`, `resize`, `rename`, `tag`).
- **C** — provisioning that yields a ready-to-use server (cloud-init for VMs, ostemplate for containers), via both an all-in-one command and a template+clone workflow, for both guest kinds.
- **A** — a skill (and docs) rewrite that documents the final, fluent surface so any agent is instantly capable.

## 2. Principles & constraints

These are inherited from the existing codebase and **must be preserved**:

- **Clean module boundaries.** `config.py` (settings/precedence), `client.py` (thin wrapper — *one API endpoint per method*), `output.py` (Rich tables + auto-JSON), `safety.py` (the two gates), `cli.py` (wiring). New cross-cutting logic goes in small new modules rather than bloating `cli.py`.
- **100% test coverage is enforced** (`pyproject.toml`: `--cov-fail-under=100`). Every new line ships with a test. The suite is fully mocked — **no live cluster required**.
- **Two-tier safety preserved and extended.** Read-only by default; `--dangerous` for any state change; `--yes` additionally for destructive ops. Exit codes `0` ok · `1` error · `2` config · `3` confirm-required · `4` read-only.
- **The faithful thin layer remains.** The existing raw `vm create` (`-o key=value` passthrough) stays; ergonomics are added *on top*, never by removing the low-level escape hatch.

## 3. Architecture & module layout

```
pmox/
  config.py     (unchanged)
  client.py     + thin single-endpoint methods (see §3.1)
  output.py     + a small number of formatters/columns for describe & health
  safety.py     + delete-guard helper for `set`
  cli.py        + new commands, the global flags, and the lifecycle helpers (§4)
  catalog.py    NEW — pure data + resolvers: size profiles, VM image catalog, CT template resolution
  views.py      NEW — read-side compositions: describe, health (compose existing client calls)
  provision.py  NEW — provisioning orchestration: plan builders + plan executor (§6)
```

Rationale: `client.py` stays strictly one-endpoint-per-method (keeps it trivially testable against the proxmoxer mock). Multi-call *read* compositions live in `views.py`; multi-call *write* orchestration lives in `provision.py`; static data lives in `catalog.py`. `cli.py` only wires these together.

### 3.1 New `client.py` methods (thin, one endpoint each)

| Method | Endpoint (proxmoxer) | Used by |
|---|---|---|
| `update_config(node, kind, vmid, **params)` | `nodes(node).{kind}(vmid).config.put(**params)` (synchronous update) | `set`, `rename`, `tag`, provisioning |
| `resize_disk(node, kind, vmid, disk, size)` | `nodes(node).{kind}(vmid).resize.put(disk=, size=)` | `resize`, provisioning |
| `cluster_nextid()` | `cluster.nextid.get()` | `new` auto-VMID |
| `download_url(node, storage, *, url, content, filename, checksum=None, checksum_algorithm=None)` | `nodes(node).storage(storage)("download-url").post(...)` | VM cloud image fetch |
| `list_appliances(node)` | `nodes(node).aplinfo.get()` | CT template resolution / `image list --ct` |
| `download_appliance(node, storage, template)` | `nodes(node).aplinfo.post(storage=, template=)` | CT template fetch |
| `convert_to_template(node, kind, vmid)` | `nodes(node).{kind}(vmid).template.post()` | `image pull --as-template` |

(Existing `create_guest`, `clone_guest`, `guest_power`, `guest_config`, `guest_status`, `list_snapshots`, `list_tasks`, `task_status`, `storage_content`, `resolve_node`, `cluster_resources`, `list_nodes` are reused as-is.)

> **Note on the config verb:** Proxmox exposes both PUT (synchronous) and POST (async, returns UPID) for `…/config`. We use **PUT** for `update_config` so simple edits complete without a task to wait on. To be confirmed against proxmoxer's mapping during implementation.

## 4. Cross-cutting machinery

### 4.1 Global, position-independent flags

Add `--wait` / `--no-wait`, `--timeout <secs>` (default **600**), and `--dry-run` as **global** options on the root callback, alongside the existing `--dangerous`/`--json`. They are stored on `State`. `--wait` defaults **off** (opt-in), preserving today's behavior.

Flag ordering is fixed with a pure shim `hoist_global_flags(argv) -> argv` called at the top of `main()`. It lifts any known global flag — and its value, for value-taking flags — to the front before Typer parses. Result: `pmox vm set 100 -o cores=4 --dangerous --dry-run` works regardless of flag position.

- The set of hoisted flags and which take a value is a small known table inside the shim.
- The shim is a pure function → directly unit-tested for 100% coverage.
- *Alternative rejected:* document-the-rule-only in the skill (zero code, but fragile — the error keeps recurring).

### 4.2 One mutation-lifecycle helper

Every **single-endpoint** state-changing command funnels through:

```
_execute(ctx, op_label, node, call, *, params=None, destructive=False, yes=False, confirm_msg=None)
```

Order of operations:

1. **`--dry-run`** → print `{"dry_run": true, "op": op_label, "node": node, "params": params}` and exit 0. Crucially this does **not** require `--dangerous` — previewing is safe and is the whole point (the agent self-verifies before committing).
2. Enforce `--dangerous` (`require_dangerous`).
3. If `destructive`, enforce `--yes` (`confirm`).
4. Run `call()` (the client invocation).
5. If `--wait` and the result is a UPID string, poll `task_status(node, upid)` to completion or `--timeout`, then report final `exitstatus`.
6. Emit the `_ok` envelope (existing helper).

The **existing** mutating commands (power, create, clone, migrate, delete, snapshot create/delete/rollback) are **retrofitted** onto `_execute`, so `--dry-run` and `--wait` work uniformly everywhere and the safety/JSON behavior is consistent. This refactors working code, but removes duplication and unifies behavior. Their existing tests are updated; new tests cover the dry-run/wait paths.

Multi-step **provisioning** (§6) does not use `_execute`; it uses the parallel `build_plan`/`execute_plan` path (§6.5), which applies the same `--dangerous` gate, `--dry-run` plan preview, and `--wait`/`--timeout` semantics through the shared `safety.py` primitives.

### 4.3 `--wait` semantics

A helper `_maybe_wait(ctx, node, result)` is used inside `_execute` step 5. Polling is a sleep loop; in tests `time.sleep` is monkeypatched so the suite never actually sleeps. A timeout yields exit 1 with a clear message. If the result is not a UPID (e.g. a sync PUT returning `None`/a dict), `--wait` is a no-op.

### 4.4 `--dry-run` for multi-step provisioning

Provisioning commands build a **plan** (an ordered list of steps; §6) and, under `--dry-run`, print the entire plan and exit 0 without executing — the agent previews every call in the sequence before spending a `--dangerous` action.

### 4.5 Machine-readable errors

When `--json` is active, `error_boundary` emits a JSON envelope instead of Rich prose, keyed to the same exit codes:

```json
{"ok": false, "error": "read_only", "need": ["--dangerous"], "message": "…"}
```

`error` ∈ `read_only` (4), `confirm_required` (3, `need: ["--yes"]`), `config` (2), `error` (1). Exit codes are unchanged. The agent branches on `error`/`need` instead of scraping text. `error_boundary` is given the JSON flag (e.g. `error_boundary(json_output=ctx.obj.json)`); the callback-level config-error path is handled equivalently.

### 4.6 Safety refinement: `set` delete-guard

`vm/ct set` is `--dangerous`-gated like `create`. Additionally, if any `-o` pair has key `delete` (Proxmox's device-removal syntax, e.g. `-o delete=net1,scsi2`), it **also** requires `--yes` — removing a disk/NIC is destructive. A small helper in `safety.py` detects this.

## 5. Phase B — command surface

All writes route through `_execute` (or, for provisioning, the plan path). The **edit and read** commands below exist for **both** `vm` and `ct` via the existing `build_guest_app` factory. Ergonomic *create* differs by kind: `vm new` (blank shell) is Phase B; `ct new` inherently needs an ostemplate at creation and is therefore Phase C (§6.3).

### 5.1 Edit (needs `--dangerous`)

```
vm set <vmid> -o key=value …                       # PUT config (sync). --yes also required if any -o is delete=…
vm resize <vmid> --disk scsi0 --size +10G          # grow-only; +10G (grow by) or 50G (grow to)
vm rename <vmid> <newname>                          # sugar: sets name= (VM) / hostname= (CT)
vm tag <vmid> [--add a,b] [--remove c] [--set a,b]  # read-modify-write of the tags string
```

- The `-o key=value` parser (currently inline in `create`) is extracted into a small pure helper and reused by `set`/`create`/`new`.
- `tag` reads the current config, computes the new tag set via a pure `merge_tags(current, add, remove, set_)` helper, and writes back via `update_config`. `--set` replaces wholesale; `--add`/`--remove` mutate the existing set.
- `rename` and `tag` are thin wrappers over `update_config`.
- `resize` is grow-only (Proxmox does not shrink); it maps to `resize_disk`. It is a write but not destructive → `--dangerous`, no `--yes`.

### 5.2 Ergonomic create (needs `--dangerous`)

```
vm new [name] [--size small|medium|large] [--disk 50] [--storage local-lvm] [--node N] [--vmid N] [-o k=v …]
```

- `--size` resolves cores/memory from `SIZE_PROFILES` (overridable by `-o cores=`/`-o memory=`). Default size: **small**.
- `--vmid` omitted → `cluster_nextid()`.
- `--node` omitted → auto-pick when the cluster has exactly one node; otherwise error asking for `--node`.
- `--storage` default **`local-lvm`** (the PVE default), override per-invocation.
- Static sane defaults for a bootable shell, all overridable via `-o`: `scsihw=virtio-scsi-single`, `net0=virtio,bridge=vmbr0`, `scsi0=<storage>:<disk>,iothread=1`, `boot=order=scsi0`, `ostype=l26`.
- The raw `vm create` stays as the faithful thin passthrough; `new` is the recommended layer on top.
- `--image` / `--from-template` (the cloud-init modes) are added in Phase C; in Phase B, `new` produces a blank shell.
- Containers: `ct new` is introduced in Phase C (§6.3), since an LXC requires an ostemplate at creation — there is no meaningful blank-shell container.

**`SIZE_PROFILES`:**

| Profile | cores | memory (MiB) |
|---|---|---|
| small | 1 | 1024 |
| medium | 2 | 4096 |
| large | 4 | 8192 |

(Tunable constants in `catalog.py`.)

**Deferred:** `--like <vmid>` (mirror an existing guest's `scsihw`/`cpu`/`bridge`). Static defaults cover the common case; add later only if they prove too rigid.

### 5.3 Read (no gate; compositions in `views.py`)

```
vm describe <vmid>   # one blob: status + config + owning node + snapshots + this guest's recent tasks
health               # quorum/online; per-node CPU+mem pressure; storage near-full; running/stopped counts (with flags)
```

- `describe_guest(client, kind, vmid, node=None)` composes `guest_status` + `guest_config` + resolved node + `list_snapshots` + `list_tasks` filtered to this vmid. JSON = one object; human = a few Rich panels.
- `summarize_health(client, thresholds)` composes `cluster_status` (quorum/online), `list_nodes` (per-node CPU/mem; flag > threshold), `cluster_resources(type="storage")` (flag near-full), and `cluster_resources(type="vm")` (running vs stopped counts; list stopped). Thresholds (default 85%) are constants in `catalog.py`. Best-effort triage; precise "should be up but down" detection (onboot/HA) is not attempted in v1.

## 6. Phase C — provisioning

### 6.1 Image catalog & appliance resolution (`catalog.py`)

- `IMAGE_CATALOG` — VM cloud images by short name → `{url, checksum, checksum_algorithm}`. Initial set: `ubuntu-24.04` (noble), `ubuntu-22.04` (jammy), `debian-12` (bookworm genericcloud). Checksums are **recommended but optional** in v1 (a missing checksum downloads with a warning); the catalog notes that URLs/checksums need periodic refresh.
- CT templates are resolved from the **live** appliance list (`list_appliances`) by **prefix match** (e.g. short name `ubuntu-24.04` → the newest `ubuntu-24.04-standard_*` entry), because appliance filenames version over time. Hardcoding is avoided.
- Escape hatches: `--image <url>` or `--image <volid>` for VMs; `--template <volid|name>` for CTs.

### 6.2 `vm new` unifies three mutually-exclusive modes

```
vm new web                          # blank shell (§5.2)
vm new web --image ubuntu-24.04 …   # all-in-one cloud-init
vm new web --from-template 9000 …   # clone a golden template + cloud-init
```

Shared cloud-init options for the two provisioning modes: `--ssh-key <path>` (repeatable), `--ip dhcp | <cidr>,gw=<ip>`, `--ciuser`, `--cipassword`, `--nameserver`.

**`--image` all-in-one plan** (ordered steps):

1. Resolve image (catalog name → url+checksum; or url; or volid).
2. Ensure image on `--storage`: check `storage_content` for an existing volume (**cache** — skip if present); else `download_url(content="import", filename=…, checksum=…)` and await the download task.
3. `create_guest(qemu, vmid, name=, cores=, memory=, scsihw="virtio-scsi-single", net0=…, ostype="l26", serial0="socket", vga="serial0", agent=1)` — cloud-image-friendly console.
4. `update_config(scsi0="<storage>:0,import-from=<volid|path>,iothread=1")` — PVE 8 disk import.
5. `update_config(ide2="<storage>:cloudinit", boot="order=scsi0", ciuser=, cipassword=, sshkeys=<url-encoded>, ipconfig0=<built>, nameserver=)`.
6. `resize_disk(qemu, vmid, "scsi0", "<disk>G")` — grow from the small cloud image to `--disk`.
7. Start (and, with `--wait`, poll to running).

**`--from-template` plan:** `clone_guest(template_id → newid, name=, full=?, target=?)` (await) → `update_config(sshkeys=, ciuser=, ipconfig0=, …)` → optional `resize_disk` → start/wait.

### 6.3 `ct new` (simpler — native LXC, no cloud-init drive)

```
ct new box --template ubuntu-24.04 --size small --disk 8 --storage local-lvm --ssh-key <path> [--ip dhcp] [--password …]
```

Plan: resolve template (catalog/aplinfo → download to a `vztmpl` storage if missing, await) → single `create_guest(lxc, vmid, hostname=, ostemplate=<volid>, cores=, memory=, rootfs="<storage>:<disk>", net0="name=eth0,bridge=vmbr0,ip=dhcp", **{"ssh-public-keys": <keys>}, password=?)` → start/wait. LXC accepts SSH keys and network config directly at create, so no cloud-init drive is needed.

### 6.4 `image` group (discovery + golden templates)

```
image list [--ct]                                          # catalog + (for --ct) live aplinfo; read-only
image pull <name|url> --storage S [--ct] [--as-template]   # download (cached); --as-template builds a golden VM template
```

- `image list` is read-only (no gate).
- `image pull` writes to storage (and, with `--as-template`, creates+templatizes a VM) → `--dangerous`. With `--as-template` (VMs only) it runs: download → `create_guest` → import disk → attach cloud-init drive → `convert_to_template`, producing a template for fast `vm new --from-template` clones.

### 6.5 Orchestration model (`provision.py`)

- `build_<flow>_plan(client, params) -> list[Step]` performs the **read-only** resolution (`cluster_nextid`, `storage_content` cache check, `list_appliances` match) and returns a concrete, serializable ordered list of steps. A `Step` names a client method + concrete kwargs (e.g. `{"op": "create_guest", "args": {…}}`), so later steps' args (imported volid, new vmid) are computed deterministically up front.
- `execute_plan(client, plan, *, wait, timeout)` dispatches each step via `getattr(client, step.op)(**step.args)`, awaiting any step that returns a task UPID (e.g. download, clone, create, start) before a dependent step runs; synchronous steps (e.g. the `config` PUTs) need no await. Exactly which endpoints are async is confirmed during implementation.
- `--dry-run` prints the plan from `build_*_plan` and never calls `execute_plan` → zero mutations.
- This split makes the orchestration testable: assert the built plan (pure) separately from execution order (against the existing `fake_client`).

### 6.6 Cloud-init helpers (pure, unit-tested)

- `encode_sshkeys(text) -> str` — reads/normalizes one or more public keys and URL-encodes them for the `sshkeys` config value.
- `build_ipconfig(spec) -> str` — `dhcp` → `ip=dhcp`; `192.168.1.50/24,gw=192.168.1.1` → `ip=192.168.1.50/24,gw=192.168.1.1`.

## 7. Phase A — skill & docs

`plugin/skills/proxmox/SKILL.md` is rewritten to document the final surface:

- Flag ordering is now a non-issue (global flags work anywhere) — note it, drop the gotcha framing.
- **Discovery:** `pmox health`, `pmox vm describe <id>`, `pmox image list`.
- **Recipes**, headlined by one-call server creation:
  ```
  pmox --dangerous vm new web --image ubuntu-24.04 --size small --disk 50 \
       --ssh-key ~/.ssh/id_ed25519.pub --ip dhcp --wait
  ```
  plus: edit (`vm set -o cores=4 -o memory=4096`), `resize`, `rename`, `tag`; the template workflow (`image pull … --as-template` then `vm new --from-template`); `ct new`.
- The **sizing-profile table**.
- "**Use `--dry-run` to self-verify the plan before spending a `--dangerous` action.**"
- "**Branch on the JSON `error`/`need` fields**" for programmatic error handling.
- The updated safety model (incl. `set --delete=` requiring `--yes`).

Matching updates: `plugin/commands/{run,cluster-status,list-guests}.md` (where relevant), `README.md`, `plugin/README.md`, and `CHANGELOG.md` (move items under `[Unreleased]`).

## 8. Safety model (final)

| Operation class | Gate |
|---|---|
| Reads: `version`, `nodes`, `cluster`, `*list`, `*status`, `*config`, `describe`, `health`, `storage`, `task`, `image list` | none |
| Non-destructive writes: `set` (no `delete=`), `resize`, `rename`, `tag`, `new` (all modes), `create`, `clone`, `start`/`shutdown`/`reboot`/`suspend`/`resume`, `snapshot create`, `image pull` | `--dangerous` |
| Destructive writes: `set` with `delete=`, `stop`, `reset`, `migrate`, `delete`, `snapshot delete`/`rollback` | `--dangerous` + `--yes` |
| Any write with `--dry-run` | none — prints the plan, exits 0 |

## 9. Testing strategy

- **Coverage held at 100%** (`--cov-fail-under=100`); fully mocked, no live cluster.
- **Client:** endpoint-chain test per new method (mirrors `test_client.py`).
- **Pure helpers** (`hoist_global_flags`, `merge_tags`, the `-o` parser, `encode_sshkeys`, `build_ipconfig`, `SIZE_PROFILES`/catalog resolvers, threshold formatting): direct unit tests.
- **`views.py`** (`describe_guest`, `summarize_health`): tested against the `api` mock / `fake_client` for composition correctness.
- **`provision.py`:** plan builders tested for exact ordered steps & resolved params; `execute_plan` tested for call order and await behavior against `fake_client` (`time.sleep` patched).
- **CLI** (`test_cli.py`): every new command and mode across read-only / `--dangerous` / `--yes` / `--dry-run` / `--wait`. `--dry-run` tests assert the plan is printed and **zero** client mutations occur. Retrofitted existing commands keep their tests, plus new dry-run/wait coverage.

## 10. Risks & assumptions

- **PVE 8.1+** is assumed for the `import` storage content type and `import-from` in the config endpoint (the all-in-one VM path). Fallback: download the cloud image to a directory/NFS storage and `import-from` a filesystem path. Confirm against the target cluster during planning.
- Cloud images require a serial console; the `--image` defaults set `serial0`/`vga` accordingly.
- Reporting the booted VM's **IP** needs the guest agent and a post-boot poll → **out of scope** for v1. Provisioning reports the vmid and "ready, cloud-init applied"; a future `vm ip <id>` can add it.
- Appliance (LXC template) filenames drift → resolve by prefix from the live `aplinfo` list.
- Catalog image URLs/checksums require periodic maintenance; the `--image <url|volid>` escape hatch avoids hard dependence on the catalog.

## 11. Out of scope

- **MCP server** (layer D) — explicitly excluded.
- Declarative `apply -f` reconciliation; bulk tag/selector operations.
- `vm ip` (post-boot IP discovery); `vm new --like` (config inheritance by scanning).

## 12. Implementation sequencing (for the plan)

1. **Phase B — foundation.**
   1. Cross-cutting: `hoist_global_flags`, global flags on the callback, `_execute` + `_maybe_wait`, machine-readable `error_boundary`, retrofit existing mutations. (§4)
   2. Edit commands: `set` (+ delete-guard), `resize`, `rename`, `tag`; extract the `-o` parser and `merge_tags`. (§5.1)
   3. `vm new` (blank shell) with profiles, auto-VMID, node auto-pick, defaults; `catalog.SIZE_PROFILES`. (§5.2)
   4. `views.py`: `describe`, `health`. (§5.3)
2. **Phase C — provisioning** (builds on B's `update_config`, `resize`, `_execute`/`--wait`, `--dry-run`).
   1. Client methods for download/appliance/template; `catalog` image entries + appliance resolver; `provision.py` plan/execute model + cloud-init helpers. (§3.1, §6.1, §6.5, §6.6)
   2. `vm new --image` and `--from-template`. (§6.2)
   3. `ct new`. (§6.3)
   4. `image list` / `image pull [--as-template]`. (§6.4)
3. **Phase A — skill & docs** (documents the final surface). (§7)

Each phase is independently shippable and leaves the suite green at 100% coverage.
