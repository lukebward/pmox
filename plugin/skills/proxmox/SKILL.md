---
name: proxmox
description: Use when the user wants to inspect, manage, or provision their Proxmox VE cluster - VMs, LXC containers, nodes, storage, snapshots, tasks, cluster health, or creating new servers from cloud images and templates. Drives the `pmox` command-line tool (read-only by default; explicit gates before any change).
allowed-tools: Bash(pmox:*), Bash(python:*)
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

If a command fails with a **config error (exit 2)**, the user hasn't configured
credentials. Tell them to set `PROXMOX_HOST`, `PROXMOX_TOKEN_ID`, and
`PROXMOX_TOKEN_SECRET` as environment variables (or in a `.env` file). pmox
reads these automatically.

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
| 1 | Error |
| 2 | Config missing (`PROXMOX_HOST` etc. not set) |
| 3 | Operation needs `--yes` |
| 4 | Operation needs `--dangerous` |

### JSON error envelope

Under `--json` (or when the output is captured), errors are a structured
envelope you can branch on:

```json
{"ok": false, "error": "read_only", "need": ["--dangerous"], "message": "..."}
```

`error` is one of four fixed codes:

| Code | Exit | Meaning |
|------|------|---------|
| `read_only` | 4 | Operation needs `--dangerous` |
| `confirm_required` | 3 | Operation needs `--yes` |
| `config` | 2 | Credentials not configured |
| `error` | 1 | General error |

Check `ok` first. If `ok` is false and a `need` array is present (only for
`read_only` and `confirm_required`), it lists the flag to add (`--dangerous`
or `--yes`). For `config` and `error` there is no `need` key.

### `--dry-run`

Add `--dry-run` to any change command to print the exact API call as JSON and
exit with zero mutations. Useful for previewing before committing. Note:
`--dry-run` still makes **read** API calls to resolve node/VMID — cluster
connectivity is required.

### `--wait` / `--timeout`

Add `--wait` to block until the resulting task finishes and report its outcome.
`--timeout <s>` controls how long to wait (default 600 s). Use these after
create/start/clone operations to confirm completion before proceeding.

## Discovery — start here

```
pmox health                              # one-shot cluster health triage
pmox nodes list                          # all nodes + resource usage
pmox vm list                             # all VMs (status, resources)
pmox ct list                             # all containers
pmox cluster resources                   # cluster-wide resource view
pmox cluster resources --type vm         # filter: vm | node | storage | sdn | pool
pmox vm describe <vmid>                  # consolidated: status + config + snapshots + tasks
pmox ct describe <vmid>                  # same for containers
pmox vm ip <vmid>                        # live IP(s): VM via guest agent, CT via interfaces
pmox ct ip <vmid>                        # (--all adds loopback, link-local, MACs)
pmox image list                          # VM cloud image catalog
pmox image list --ct --node <node>       # LXC container templates on a node
```

> `vm ip` reads the live address from the QEMU guest agent, so the guest needs
> `qemu-guest-agent` running and `agent: 1` set (cloud-init VMs from `vm new`
> already enable `agent: 1`). `ct ip` needs no agent.

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

### Seamless VM with an auto-allocated static IP

```
pmox --dangerous vm up web --image ubuntu-24.04 --wait
```

Requires a `[network]` pool in config (`cidr`/`gateway`/`pool`, the pool outside
your DHCP scope). pmox allocates a free static IP from the pool, auto-routes the
image import to a storage with the `import` content type, and ensures an SSH key.
`pmox vm ip <vmid>` returns the address immediately (read from cloud-init config —
no guest agent needed).

### Ready container (ready to SSH)

```
pmox --dangerous ct new box \
  --template ubuntu-24.04 \
  --ssh-key ~/.ssh/id_ed25519.pub \
  --ip dhcp \
  --wait
```

`--template` takes a catalog name or a vztmpl volid. Discover available
templates with `pmox image list --ct --node <node>`.

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
pmox vm ip <vmid> [--all] [--node <node>]
pmox ct ip <vmid> [--all] [--node <node>]
pmox vm snapshot list <vmid>
pmox ct snapshot list <vmid>
pmox storage list
pmox storage content <storage> --node <node>
pmox task list --node <node>
pmox task status <upid> --node <node>
pmox task log <upid> --node <node>
pmox image list
pmox image list --ct --node <node>
```

### Change (need `--dangerous`)

```
pmox --dangerous vm new <name> [--image <name|url|volid>] [--from-template <vmid>]
                                [--size small|medium|large] [--disk <GiB>]
                                [--storage <storage>] [--node <node>] [--vmid <id>]
                                [--ssh-key <path>] [--ip dhcp|<cidr>,gw=<ip>]
                                [--ciuser <u>] [--cipassword <p>] [--nameserver <dns>]
                                [-o key=value] [--wait] [--dry-run]
pmox --dangerous vm up <name> --image <name|url|volid>
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
                                [--ct]
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

- **No checksum verification.** Catalog images are downloaded over HTTPS without
  integrity verification in v1. For security-sensitive workflows, pass a URL
  from a trusted source or a pre-verified volid as `--image`.

- **`ct new` has two storages.** `--template-storage` (default `local`) holds
  the downloaded LXC template (vztmpl). `--storage` (default `local-lvm`) is
  the root filesystem. Discover available templates with
  `pmox image list --ct --node <node>`.

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
