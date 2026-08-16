# Seamless VM creation: `pmox vm up`

- **Date:** 2026-06-07
- **Status:** Design approved; ready for implementation planning
- **Author:** Luke Ward (with Claude)

## Summary

Add an opinionated `pmox vm up <name> --image <name>` command that, in a single
token-only call, produces a **ready-to-SSH VM with a known static IP**. It
composes the existing image-based provisioning with three new capabilities:

1. **Auto static-IP allocation** (a mini-IPAM that uses the cluster as its ledger),
2. **Automatic import-storage routing** (so cloud-image import stops failing on
   LVM-thin disk storage), and
3. **Automatic SSH-key resolution** (use the default key, generate one if absent).

`vm new` remains the low-level primitive and is **not** redesigned, but it shares
the new import-storage resolver so `vm new --image <catalog-name>` stops being
broken on the common lvmthin-only setup.

## Motivation

Creating a usable VM today takes many manual steps, every one of which we hit in
practice:

- `vm new --image ubuntu-24.04` fails with `can't upload to storage type
  'lvmthin', not a file based storage!` because the cloud image download is
  routed to the **disk** storage (`local-lvm`), which can't hold a file.
- Working around it requires enabling the `import` content type on a file-based
  storage by hand, then a two-step `image pull` → `vm new --image <volid>`.
- Cloud images ship no `qemu-guest-agent`, so `vm ip` can't report a DHCP
  address — you must ARP-scan, install the agent, or use a static IP.
- The user must generate and pass an SSH key, or the VM has no login.

The goal: collapse all of that into one command, while keeping pmox's
**API-token-only** model (no SSH to nodes or guests).

## Decisions (resolved during brainstorming)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Scope | Full usable-VM flow (reachable + known IP on return) |
| 2 | Guest agent | **Stay token-only** — do not require the agent; achieve "usable" without it |
| 3 | Addressing | **Auto-allocate a static IP** (a known IP without the agent requires static) |
| 4 | IPAM source of truth | **Cluster config is the ledger** — scan guests' `ipconfigN`; no local state file |
| 5 | CLI surface | **Separate `vm up` command**; `vm new` stays the primitive |
| 6 | Import fix scope | **Shared** — `vm new` also gets the import-storage resolver (stops being broken) |
| 7 | Missing SSH key | **Auto-generate** an ed25519 key via `ssh-keygen` |

Consequence of #2 + #3: the agent is **optional**. `vm ip` returns the static
address via the cloud-init-config fallback (already implemented in `views.py`).
Graceful-shutdown-via-agent and snapshot fsfreeze remain out of scope (a future
golden-template path can add the agent).

## Design

### Component A — Mini-IPAM (`pmox/ipam.py`, new module)

```
allocate_ip(client, *, cidr, gateway, pool_start, pool_end) -> "<ip>/<prefix>"
```

- Uses the stdlib `ipaddress` module for all address math.
- **Used-IP discovery (the ledger):** iterate `client.cluster_resources(type="vm")`;
  for each guest call `client.guest_config(node, kind, vmid)` and collect every
  static address from `ipconfigN` keys (`ip=<cidr>`, skipping `dhcp`/`auto`/`manual`).
- **Reserved:** the configured `gateway`.
- **Result:** the lowest address in `[pool_start, pool_end]` that is neither used
  nor reserved, returned as `"<ip>/<prefix>"` using the prefix from `cidr`.
- **Exhaustion:** raise a clear error when no address is free.
- Pure function over the injected client → unit-testable with a `MagicMock`.

**Known limitations (documented, by design):**
- Sees only Proxmox-declared **static** IPs — not DHCP leases or non-Proxmox
  hosts. Therefore the pool **must live outside the DHCP scope**. This is a hard
  requirement called out in `--help` and docs.
- Cost is one `guest_config` call per guest (N+1 reads). Acceptable for homelab
  scale; can be revisited if it becomes slow.

A small shared parser `static_ips_from_ipconfig(config) -> list[str]` is extracted
so IPAM and the `views.py` fallback parse `ipconfigN` the same way.

### Component B — Import-storage routing (`pmox/provision.py`)

New helper:

```
resolve_import_storage(client, node, explicit=None) -> str
```

- If `explicit` is given, validate it exists and has the `import` content type;
  error otherwise.
- Else auto-pick a storage on `node` whose `content` includes `import`
  (deterministic tie-break: prefer a directory/file-based storage, then first by
  name).
- If none exists, raise the actionable error (a token cannot enable content types):
  > `No storage on <node> has the 'import' content type. Enable it: Datacenter →
  > Storage → <storage> → Edit → check "Import", or `pvesm set <storage>
  > --content <existing>,import`.`

`_resolve_image_volid` / `build_vm_image_plan` are updated to take an
`import_storage` distinct from the disk `storage`: the download targets
`import_storage`, the import-file volid is `{import_storage}:import/{filename}`,
and the disk is still created on the disk `storage` via `scsi0
import-from=<volid>`. Both `vm up` and `vm new` resolve `import_storage` (with an
optional `--import-storage` override) before building the plan. This is exactly
the working pattern proven by hand (`image pull --storage local` → `vm new
--image local:import/...qcow2 --storage local-lvm`).

### Component C — SSH key resolution (`pmox/provision.py` or small helper)

```
ensure_ssh_key(path) -> str   # returns the public-key text
```

- Resolve `path` from `--ssh-key`, else config `default_ssh_key`, else
  `~/.ssh/id_ed25519.pub`.
- If the file exists, read and return it.
- If missing, generate an ed25519 keypair with
  `ssh-keygen -t ed25519 -f <priv> -N "" -C "pmox"` (invoked via `subprocess`
  with an argument **list**, avoiding shell-quoting pitfalls cross-platform),
  then return the `.pub` contents.
- `--no-ssh-key` skips key handling entirely.

### Component D — Config additions (`pmox/config.py`)

Extend `Settings` + `load_settings` (file + env, same precedence as today) with a
`[network]` table and matching `PROXMOX_NET_*` env vars:

```toml
[network]
cidr       = "192.168.0.0/24"
gateway    = "192.168.0.1"
pool       = "192.168.0.200-192.168.0.250"   # inclusive IP range
nameserver = "192.168.0.1"                    # optional

[defaults]                                    # all optional
import_storage = "local"
ssh_key        = "~/.ssh/id_ed25519.pub"
ciuser         = "ubuntu"
```

`vm up` **requires** `[network]` `cidr`/`gateway`/`pool` **only when
auto-allocating** — i.e. unless `--ip` is passed explicitly. If allocation is
needed and they're missing, it errors with exactly what to set. `vm new` never
requires `[network]`.

### Component E — `pmox vm up` command (`pmox/cli.py`)

```
pmox --dangerous vm up <name> --image <name|volid>
     [--size small|medium|large] [--disk <GiB>] [--node <node>]
     [--storage <disk-storage>] [--import-storage <storage>]
     [--ip <cidr>,gw=<ip>]            # override auto-allocation
     [--ssh-key <path>] [--no-ssh-key]
     [--ciuser <u>] [--wait] [--dry-run]
```

Flow:
1. Resolve node (auto when single-node, as `vm new` does).
2. `ip = --ip` if given, else `ipam.allocate_ip(...)` from `[network]`.
3. `import_storage = resolve_import_storage(client, node, --import-storage)`.
4. `pubkey = ensure_ssh_key(...)` unless `--no-ssh-key`.
5. Build the image plan (import + create + cloud-init `ipconfig0=ip=<ip>,gw=<gw>`
   + `sshkeys` + `nameserver` + resize + start) via the existing
   `build_vm_image_plan`, now passing `import_storage`.
6. Execute (honoring `--dangerous`, `--wait`, `--dry-run`).
7. Print a result block. The `ssh` hint uses the resolved cloud-init user
   (`--ciuser`/`[defaults].ciuser`); if none is set it prints the IP and notes
   "log in as the image's default user":
   ```
   VM 112  web   ip 192.168.0.200
   ssh ubuntu@192.168.0.200
   ```

`vm ip <vmid>` returns the same address immediately via the existing config
fallback — no agent needed.

### Data flow

```
[network] config + cluster scan (used static IPs)
        └─> ipam.allocate_ip ─> 192.168.0.200/24
node storages ─> resolve_import_storage ─> "local"
--ssh-key / default ─> ensure_ssh_key ─> "ssh-ed25519 AAAA..."
        └─> build_vm_image_plan(storage=local-lvm, import_storage=local,
                                ipconfig0=ip=..., sshkeys=..., nameserver=...)
        └─> execute_plan ─> create+import+resize+start
        └─> report ip + ssh command
```

## Error handling

| Condition | Behavior |
|-----------|----------|
| `[network]` pool not configured (vm up) | Error listing the exact keys to set |
| No `import`-capable storage on node | Actionable "enable import content" error |
| Pool exhausted / no free IP | Clear "pool exhausted (range X–Y)" error |
| `--image` resolves to an installer ISO, not a cloud image | Explain ISO vs cloud image; reject |
| SSH key missing and `ssh-keygen` unavailable | Error with the manual command to run |
| Standard safety gates | `vm up` is a create → requires `--dangerous` |

## Testing

TDD throughout; the repo's **100% line-coverage** gate is maintained
(`--cov-fail-under=100`, run via `.venv/Scripts/python -m pytest`).

- `ipam.py`: lowest-free selection, gateway reserved, used-IP scan across guests,
  pool exhaustion, prefix handling, range parsing.
- `resolve_import_storage`: explicit (valid/invalid), auto-detect (one/multiple/
  none), tie-break determinism.
- `build_vm_image_plan` with `import_storage`: import routed to the file storage,
  disk on the disk storage, `import-from` volid correct.
- `ensure_ssh_key`: existing file read; missing → generate (mock `subprocess`).
- `vm up` CLI: composition + `--dry-run` plan + result output + error paths.

## Phased rollout

Designed as a whole; shipped in independent, separately-reviewable PRs:

- **Phase 0 (done):** `vm ip` cloud-init-config fallback (`views.py`).
- **Phase 1:** `resolve_import_storage` + plan routing; wire into `vm new`
  (fixes the lvmthin bug for the existing command).
- **Phase 2:** `[network]`/`[defaults]` config + `ipam.py`.
- **Phase 3:** `ensure_ssh_key` + `vm up` command composing it all + output.

## Out of scope (possible future work)

- Installing/ensuring `qemu-guest-agent` (would need golden templates or an SSH
  path) — and the agent-only features (graceful shutdown, snapshot fsfreeze).
- SSH-to-node for cloud-init snippets, or SSH-to-guest for post-create setup.
- Network probing / DHCP-lease reading for IP discovery.
- IPv6 auto-allocation (IPAM is IPv4 for v1; static IPv6 can still be passed via
  `--ip`/`vm new`).
- Proxmox SDN/IPAM integration as an alternative ledger.
