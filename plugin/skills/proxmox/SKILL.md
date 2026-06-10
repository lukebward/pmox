---
name: proxmox
description: Use when the user wants to inspect, manage, or provision their Proxmox VE cluster - VMs, LXC containers, nodes, storage, snapshots, tasks, cluster health, or creating new servers from cloud images and templates. Drives the `pmox` command-line tool (read-only by default; explicit gates before any change).
allowed-tools: Bash(pmox:*), Bash(python -m pmox:*)
---

# Managing and Provisioning Proxmox with the `pmox` CLI

`pmox` explores **and** provisions a Proxmox VE cluster from the command line.
Use it whenever the user asks about their cluster — inspection, guest lifecycle,
configuration edits, or spinning up new VMs and containers from cloud images.
It is **read-only by default**, so exploration is always safe.

## Invoking

Run `pmox <args>`. If `pmox` is not on PATH, fall back to `python -m pmox <args>`.

pmox **auto-detects** its output format: because an agent captures the output,
it emits JSON automatically — you normally **don't need `--json`**. Parse that
JSON to answer the user. Use `--no-json` only when you want the human-readable
table (e.g. to show it verbatim).

```
pmox vm list          # JSON automatically, because output is captured
```

Global flags are **position-independent** — they work before or after the
subcommand:

```
pmox --dangerous vm start 100
pmox vm start 100 --dangerous   # identical
```

**`--yes`/`-y` is *not* a global flag** — it's a per-subcommand confirmation that
must come **after** the subcommand (e.g. `pmox --dangerous vm delete 100 --yes`,
never `pmox --dangerous --yes vm delete 100`, which fails with `No such option:
--yes`). Other per-subcommand options (`-n/--node`, `--purge`, `--target`,
`-o/--option`) likewise go after the subcommand.

**Exit 2 means config error OR usage error** — check the JSON envelope's
`error` field. `"config"` → the user hasn't configured credentials: tell them to
set `PROXMOX_HOST`, `PROXMOX_TOKEN_ID`, and `PROXMOX_TOKEN_SECRET` (env vars or
a `.env` file). `"usage"` → your command line was wrong (typo'd flag or
subcommand): fix it using the envelope's `hint`.

`pmox guide` prints the full agent guide (safety model, envelopes, recipes,
recovery) in one call — useful as a refresher without crawling `--help`.

List commands accept `--fields vmid,name,status` to keep output small on big
clusters (missing keys come back as `null`).

## Safety model

pmox has two independent gates. **Respect them — never try to bypass them.**

### Gate 1: `--dangerous`
Any state change (start, stop, create, delete, clone, migrate, snapshot,
configure) requires the global `--dangerous` flag. Without it, the command
is blocked (exit 4).

### Gate 2: `--yes`
Destructive operations additionally require `--yes`, a **per-subcommand** flag
that must be placed **after** the subcommand (unlike the global `--dangerous`).
The destructive set is:
`vm delete`, `vm stop`, `vm reset`, `vm migrate`, `vm snapshot rollback`,
`vm snapshot delete`, `ct delete`, `ct stop`, `ct reset`, `ct migrate`,
`ct snapshot rollback`, `ct snapshot delete`, and **`vm set`/`ct set` with a
`delete=` key**.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error (see the envelope's `error` field: `error` or `network`) |
| 2 | Config missing **or** CLI usage error (envelope: `config` vs `usage`) |
| 3 | Operation needs `--yes` |
| 4 | Operation needs `--dangerous` |

### JSON error envelope

Under `--json` (or when the output is captured), errors are a structured
envelope you can branch on:

```json
{"ok": false, "error": "read_only", "need": ["--dangerous"], "message": "..."}
```

`error` is one of six fixed codes:

| Code | Exit | Meaning |
|------|------|---------|
| `read_only` | 4 | Operation needs `--dangerous` |
| `confirm_required` | 3 | Operation needs `--yes` |
| `config` | 2 | Credentials not configured / config file invalid |
| `usage` | 2 | Bad command line (typo'd flag or subcommand) |
| `network` | 1 | Can't reach the Proxmox API (DNS/TLS/timeout — often transient) |
| `error` | 1 | General error |

Check `ok` first. If `ok` is false and a `need` array is present (only for
`read_only` and `confirm_required`), it lists the flag to add (`--dangerous`
or `--yes`). Envelopes may carry extra machine-actionable fields:

- task failures/timeouts: `upid`, `node`, and a `hint` (e.g. resume with
  `pmox task wait <upid>`).
- partial provisioning failures: `vmid`, `completed_steps`, `failed_step`, and
  a `hint` saying whether a guest was already created and how to recover.
  **A guest that was created is not cleaned up; a blind retry would create a
  second one** — follow the hint (describe, then finish manually or delete).

### Success envelope

Mutations return structured results — read the fields, never parse the prose:

```json
{"ok": true, "message": "...", "op": "qemu.start", "vmid": 100, "node": "pve1",
 "upid": "UPID:...", "hint": "..."}
```

With `--wait` the `upid` is replaced by a `task` object (final task status).
`vm new`/`ct new`/`vm up`/`image pull --as-template` all return the created
`vmid`; `vm up` adds `name`, `ip` (`null` when DHCP) and `ssh`.

### `--dry-run`

Add `--dry-run` to any change command to print the exact API call as JSON and
exit with zero mutations. Useful for previewing before committing. Note:
`--dry-run` still makes **read** API calls to resolve node/VMID — cluster
connectivity is required.

### `--wait` / `--timeout` / `task wait`

Add `--wait` to block until the resulting task finishes and report its outcome.
`--timeout <s>` controls how long each wait may take (default 600 s).
**Provisioning commands (`vm up`, `vm new --image`, `ct new`, `image pull`)
always wait on their internal steps** — `--wait` matters for one-shot ops like
start/stop/delete.

If your shell times out (or `--timeout` expires) mid-operation, the Proxmox
task keeps running server-side. Resume with:

```
pmox task wait <upid>          # node parsed from the UPID; honors --timeout
pmox task status <upid>        # one-shot check
pmox task log <upid>           # failure details
```

Timeout/failure envelopes include the `upid` so you can do this without
re-parsing any text. Image downloads are cached: re-running a provisioning
command skips an already-downloaded image. The create step is NOT idempotent —
on a partial failure, follow the envelope's `hint` instead of blindly retrying.

## Discovery — start here

```
pmox guide                               # the full agent guide, in one call
pmox health                              # one-shot cluster health triage
pmox nodes list                          # all nodes + resource usage
pmox vm list [--fields vmid,name,status] # all VMs (status, resources)
pmox ct list                             # all containers
pmox cluster resources                   # cluster-wide resource view
pmox cluster resources --type vm         # filter: vm | node | storage | sdn | pool
pmox vm describe <vmid>                  # consolidated: status + config + snapshots + tasks
pmox ct describe <vmid>                  # same for containers
pmox vm ip <vmid> [--wait]               # live IP(s): agent, static config, or same-LAN ARP scan
pmox ct ip <vmid>                        # (--all adds loopback, link-local, MACs)
pmox image list                          # VM cloud image catalog
pmox image list --ct                     # LXC container templates (node auto-picked)
```

> `vm ip` tries three sources in order: the QEMU guest agent, the static
> cloud-init config, and — for agent-less DHCP VMs — a same-LAN ARP scan by the
> guest's MAC (`source: "arp"`, IPv4 only). The scan needs pmox to run on the
> same network as the guest (works from the LAN, not over a VPN) and degrades
> to an actionable error elsewhere. `ct ip` needs no agent. After creating a
> DHCP guest, `pmox vm ip <vmid> --wait` polls until an address appears.

> Using `vm ...` on a container VMID (or vice versa) returns a clear error with
> the corrective command. Unknown VMIDs error with "not found" — check
> `pmox vm list` / `pmox ct list`. `--node` is optional almost everywhere:
> resolved from the VMID, parsed from the UPID, or auto-picked on single-node
> clusters (multi-node errors list the candidate names).

## Recipes

### One-call cloud-init VM (ready to SSH)

```
pmox --dangerous vm new web \
  --image ubuntu-24.04 \
  --size small \
  --disk 50 \
  --ssh-key ~/.ssh/id_ed25519.pub \
  --ip dhcp \
  --wait
```

pmox auto-picks a VMID and node, imports the image, wires cloud-init, and
waits for the task to complete.

### Seamless one-shot VM (`vm up`)

```
pmox --dangerous vm up web --image ubuntu-24.04 --wait
```

Zero-config: auto-routes the image import to an `import`-capable storage, ensures
an SSH key (generating one if absent), and creates the VM with **DHCP** by
default. For a known static IP, pass `--ip <cidr>,gw=<ip>`, or set a `[network]`
pool (`cidr`/`gateway`/`pool`, outside your DHCP scope) so pmox auto-allocates a
free address — then `pmox vm ip <vmid>` returns it immediately from cloud-init
config, no guest agent needed. For DHCP guests, `pmox vm ip <vmid> --wait`
polls until an address appears — via the guest agent, or a same-LAN ARP scan
when pmox runs on the same network as the guest.

To get a **DHCP** VM's IP via the guest agent instead, clone a template that has
`qemu-guest-agent` baked in: `pmox --dangerous vm up web --from-template <vmid>`
(mutually exclusive with `--image`; the clone inherits the template's hardware
and pmox warns if --size/--storage are passed alongside).
pmox can't install the agent (token-only), so build that template once yourself
(boot a base VM, `apt install qemu-guest-agent`, convert it to a template).

### Ready container (ready to SSH)

```
pmox --dangerous ct new box \
  --template ubuntu-24.04 \
  --ssh-key ~/.ssh/id_ed25519.pub \
  --ip dhcp \
  --wait
```

`--template` takes a catalog name or a vztmpl volid. Discover available
templates with `pmox image list --ct [--node <node>]`.

### Edit a guest

```
pmox --dangerous vm set 100 -o cores=4 -o memory=4096
pmox --dangerous vm resize 100 --disk scsi0 --size +10G
pmox --dangerous vm rename 100 web01
pmox --dangerous vm tag 100 --add prod,k3s
```

`vm set` / `ct set` accept any Proxmox config key as `-o key=value` (repeatable).
To remove a key use `-o delete=<key>` — that triggers the `--yes` gate too.

### Blank VM shell

```
pmox --dangerous vm new web --size small --disk 50 --node pve1
```

No `--image` or `--from-template` → blank QEMU VM with a disk attached.

### Golden template + clone

Pull a cloud image and convert it to a reusable template (one-way):

```
pmox --dangerous image pull ubuntu-24.04 \
  --storage local \
  --node pve1 \
  --as-template
```

Then clone it into a new cloud-init VM (read the printed VMID from the pull):

```
pmox --dangerous vm new web \
  --from-template <vmid> \
  --ssh-key ~/.ssh/id_ed25519.pub \
  --ip dhcp \
  --wait
```

### Preview before acting

Add `--dry-run` to any change to print the full plan (zero mutations):

```
pmox --dangerous --dry-run vm new web --image ubuntu-24.04 --size medium
```

Confirm completion afterward with `--wait` or `pmox vm describe <vmid>`.

### Snapshot lifecycle

```
pmox --dangerous vm snapshot create 100 before-upgrade
pmox vm snapshot list 100
pmox --dangerous vm snapshot rollback 100 before-upgrade --yes
pmox --dangerous vm snapshot delete 100 before-upgrade --yes
```

### Guest lifecycle

```
pmox --dangerous vm start 100
pmox --dangerous vm shutdown 100      # graceful
pmox --dangerous vm stop 100 --yes   # hard stop
pmox --dangerous vm reboot 100
pmox --dangerous vm reset 100 --yes  # hard reset
pmox --dangerous vm suspend 100
pmox --dangerous vm resume 100
pmox --dangerous vm delete 100 [--purge] --yes
pmox --dangerous vm migrate 100 --target pve2 [--online] --yes
```

Use `ct` in place of `vm` for containers — identical subcommand surface.

## Sizing profiles

| Profile | Cores | Memory |
|---------|-------|--------|
| `small` | 1 | 1024 MiB |
| `medium` | 2 | 4096 MiB |
| `large` | 4 | 8192 MiB |

Pass as `--size small` to `vm new` or `ct new`. Disk size (`--disk <GiB>`) is
set separately.

## Command cheat-sheet

### Inspect (always safe — no flags needed)

```
pmox version
pmox health
pmox nodes list
pmox nodes status <node>
pmox cluster status
pmox cluster resources [--type vm|node|storage|sdn|pool]
pmox vm list [--node <node>]
pmox ct list [--node <node>]
pmox vm status <vmid>
pmox ct status <vmid>
pmox vm config <vmid>
pmox ct config <vmid>
pmox vm describe <vmid>
pmox ct describe <vmid>
pmox vm ip <vmid> [--all] [--wait] [--node <node>]
pmox ct ip <vmid> [--all] [--node <node>]
pmox vm snapshot list <vmid>
pmox ct snapshot list <vmid>
pmox storage list
pmox storage content <storage> [--node <node>]
pmox task list [--node <node>]
pmox task status <upid> [--node <node>]
pmox task log <upid> [--node <node>]
pmox task wait <upid> [--node <node>]
pmox image list
pmox image list --ct [--node <node>]
```

### Change (need `--dangerous`)

```
pmox --dangerous vm new <name> [--image <name|url|volid>] [--from-template <vmid>]
                                [--size small|medium|large] [--disk <GiB>]
                                [--storage <storage>] [--node <node>] [--vmid <id>]
                                [--ssh-key <path>] [--ip dhcp|<cidr>,gw=<ip>]
                                [--ciuser <u>] [--cipassword <p>] [--nameserver <dns>]
                                [-o key=value] [--wait] [--dry-run]
pmox --dangerous vm up <name> (--image <name|url|volid> | --from-template <vmid>)
                               [--size ...] [--disk <GiB>] [--node <node>]
                               [--storage <disk>] [--import-storage <storage>]
                               [--ip <cidr>,gw=<ip>] [--ssh-key <path>] [--no-ssh-key]
                               [--ciuser <u>] [--wait] [--dry-run]
pmox --dangerous ct new <name> --template <name|volid>
                                [--size small|medium|large] [--disk <GiB>]
                                [--storage <storage>] [--template-storage <storage>]
                                [--node <node>] [--vmid <id>]
                                [--ssh-key <path>] [--ip dhcp|<cidr>,gw=<ip>]
                                [--password <pw>] [--wait] [--dry-run]
pmox --dangerous image pull <name|url> --storage <storage> --node <node>
                                [--as-template [--vmid <id>] [--name <name>]]
                                [--ct] [--checksum <algo>:<hexdigest>]
pmox --dangerous vm set <vmid> -o key=value [-o key=value ...]
pmox --dangerous ct set <vmid> -o key=value [-o key=value ...]
pmox --dangerous vm resize <vmid> --disk <disk> --size +10G|50G
pmox --dangerous ct resize <vmid> --disk <disk> --size +10G|50G
pmox --dangerous vm rename <vmid> <newname>
pmox --dangerous ct rename <vmid> <newname>
pmox --dangerous vm tag <vmid> [--add <tags>] [--remove <tags>] [--set <tags>]
pmox --dangerous ct tag <vmid> [--add <tags>] [--remove <tags>] [--set <tags>]
pmox --dangerous vm start <vmid>
pmox --dangerous ct start <vmid>
pmox --dangerous vm shutdown <vmid>
pmox --dangerous ct shutdown <vmid>
pmox --dangerous vm reboot <vmid>
pmox --dangerous ct reboot <vmid>
pmox --dangerous vm suspend <vmid>
pmox --dangerous vm resume <vmid>
pmox --dangerous vm clone <vmid> --newid <id> [--name <n>] [--full] [--target <node>]
pmox --dangerous ct clone <vmid> --newid <id> [--name <n>] [--full] [--target <node>]
pmox --dangerous vm snapshot create <vmid> <name>
pmox --dangerous ct snapshot create <vmid> <name>
```

### Destroy (need `--dangerous --yes`)

```
pmox --dangerous vm stop <vmid> --yes
pmox --dangerous ct stop <vmid> --yes
pmox --dangerous vm reset <vmid> --yes
pmox --dangerous ct reset <vmid> --yes
pmox --dangerous vm migrate <vmid> --target <node> [--online] --yes
pmox --dangerous ct migrate <vmid> --target <node> --yes
pmox --dangerous vm delete <vmid> [--purge] --yes
pmox --dangerous ct delete <vmid> [--purge] --yes
pmox --dangerous vm snapshot rollback <vmid> <name> --yes
pmox --dangerous ct snapshot rollback <vmid> <name> --yes
pmox --dangerous vm snapshot delete <vmid> <name> --yes
pmox --dangerous ct snapshot delete <vmid> <name> --yes
pmox --dangerous vm set <vmid> -o delete=<key> --yes
pmox --dangerous ct set <vmid> -o delete=<key> --yes
```

## Requirements and caveats

- **PVE 8.2+ required for `vm new --image` and `image pull`** (PVE 8.4+
  recommended). The target storage must have the **`import` content type**
  enabled. pmox uses an API token, so it imports by volume-ID; absolute paths
  would require `root@pam`, which is not supported.

- **Checksum verification is opt-in.** Catalog images download over HTTPS
  without integrity verification by default. For security-sensitive workflows,
  pre-pull with `pmox --dangerous image pull <name> --checksum sha256:<hex> ...`
  (the verified, cached image is then reused by `vm new`/`vm up`/`--as-template`),
  or pass a trusted URL / pre-verified volid as `--image`.

- **`ct new` has two storages.** `--template-storage` (default `local`) holds
  the downloaded LXC template (vztmpl). `--storage` is the root filesystem
  (auto-detected when omitted; local-lvm preferred). Discover available templates with
  `pmox image list --ct [--node <node>]`.

- **`--dry-run` still reads.** Zero mutations are made, but pmox still calls the
  Proxmox API to resolve node/VMID lookups. Cluster connectivity is required.

- **Clones inherit the template's disk size.** When using
  `vm new --from-template`, pass `--disk <GiB>` to override, or resize after
  with `vm resize`.

- **Template conversion is one-way.** `image pull --as-template` converts the
  downloaded image into a PVE template; this cannot be undone without deleting
  and re-pulling.

## Rules for the AI

1. **Default to read-only.** Never add `--dangerous` unless the user explicitly
   asks to change or create something.

2. **Add `--dangerous` only when requested.** When the user asks to start,
   stop, configure, create, or delete a guest — add `--dangerous`. Nothing else.

3. **Add `--yes` only for destructive actions, and confirm first.** Before
   running delete, stop, reset, migrate, snapshot rollback/delete, or
   `set delete=` — confirm the exact target (VMID, name, snapshot) with the user,
   then run with both `--dangerous --yes`.

4. **Never try to bypass the gates.** If the CLI exits with code 3 or 4, that
   is the safety model working as intended. Surface the `need` field (present
   for exit 3 and 4) from the JSON error envelope and ask the user for explicit
   confirmation before retrying with the appropriate flags.

5. **Use `--wait` for create/start operations** when you need to confirm
   completion before reporting success or taking the next step.

6. **Parse JSON output.** pmox emits JSON when output is captured. Read the
   structured data to answer questions precisely rather than scraping text.
