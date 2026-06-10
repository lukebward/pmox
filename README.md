# pmox

A friendly, **AI-friendly** command-line tool to explore and manage a [Proxmox VE](https://www.proxmox.com/) cluster.

`pmox` wraps the Proxmox API with clean commands, pretty tables for humans, and a
`--json` mode for machines. It is **read-only by default** so you (or an AI) can
explore safely, with two layers of protection before anything can change.

```
pmox health
pmox vm list
pmox vm describe 100

# Spin up a cloud-init VM in one command:
pmox --dangerous vm new web --image ubuntu-24.04 --size small --disk 50 \
     --ssh-key ~/.ssh/id_ed25519.pub --ip dhcp --wait

# Edit a running VM:
pmox --dangerous vm set 100 -o cores=4 -o memory=4096
pmox --dangerous vm resize 100 --disk scsi0 --size +10G
pmox --dangerous vm rename 100 web01
pmox --dangerous vm tag 100 --add prod,k3s

pmox --dangerous vm start 100
pmox --dangerous vm delete 100 --yes
```

## Safety model

Two independent gates protect your cluster:

| Gate | Flag | Applies to | Default |
|------|------|-----------|---------|
| **Dangerous mode** | `--dangerous` (or `PMOX_DANGEROUS=1`) | *any* state change (power, create, delete, clone, migrate, snapshot) | **off — read-only** |
| **Confirmation** | `--yes` | *destructive* ops: `delete`, `stop`, `reset`, `migrate`, `rollback`, snapshot `delete`, and `set` with a `delete=` key | required when non-interactive |

So:

- **Explore** with no flags — nothing can be modified.
- **Change** something benign (e.g. `start`) — add `--dangerous`.
- **Destroy** something (e.g. `delete`) — add `--dangerous` **and** `--yes`.
  Note: `--dangerous` is global, but `--yes` is a **per-subcommand** flag, so it
  goes *after* the subcommand — `pmox --dangerous vm delete 100 --yes`, not
  `pmox --dangerous --yes vm delete 100`.

When running non-interactively (e.g. an AI calling the CLI), a destructive op
*without* `--yes` is refused rather than silently prompted. Exit codes:
`0` ok · `1` error/network · `2` config **or usage** error · `3` confirmation
required · `4` read-only. (Exit 2 covers both missing config and a typo'd
command line; the envelope's `error` field — `config` vs `usage` — tells them apart.)

Under `--json`, errors are a JSON envelope `{"ok": false, "error": "...", "need": [...], "message": "..."}`
with `error` one of `read_only` / `confirm_required` / `config` / `usage` /
`network` / `error`, plus machine-actionable fields where relevant (`upid`,
`node`, `vmid`, `completed_steps`, `failed_step`, `hint`). Successful mutations
return `{"ok": true, "op": ..., "vmid": ..., "node": ..., "upid"|"task": ..., "message": ...}`
— the created VMID is always a field, never just prose. Run **`pmox guide`**
for the full agent guide (safety model, envelopes, recipes, recovery).

## Install

From [PyPI](https://pypi.org/project/pmox/):

```bash
pip install pmox
```

Or install from source (editable), e.g. for development:

**macOS / Linux**

```bash
git clone https://github.com/lukebward/pmox.git
cd pmox
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/lukebward/pmox.git
cd pmox
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

This puts a `pmox` command on your PATH. You can also run it without installing
via `python -m pmox`.

## Configure

Create an **API token** in Proxmox: *Datacenter → Permissions → API Tokens*.
For full management, give the token the privileges it needs (or, for a homelab,
uncheck "Privilege Separation" so it inherits the user's permissions).

Provide configuration via environment variables, a `.env` file, a TOML config
file, or CLI flags (highest priority last):

**`.env`** (copy from `.env.example`):

```ini
PROXMOX_HOST=192.168.1.10
PROXMOX_TOKEN_ID=root@pam!pmox
PROXMOX_TOKEN_SECRET=00000000-0000-0000-0000-000000000000
PROXMOX_VERIFY_SSL=false
```

**TOML** at `~/.config/pmox/config.toml` (or point `--config` / `PMOX_CONFIG` at one):

```toml
host = "192.168.1.10"
token_id = "root@pam!pmox"
token_secret = "..."
verify_ssl = false
```

> TLS verification defaults to **off** because homelab Proxmox uses self-signed
> certificates. Set `PROXMOX_VERIFY_SSL=true` (or `--verify-ssl`) if your node
> has a CA-signed cert.

## Commands

```
pmox version                         Proxmox version of the connected node
pmox guide                           print the built-in agent/automation guide
pmox health                          One-shot cluster health triage (read-only)
pmox nodes list                      nodes + CPU/mem/uptime
pmox nodes status <node>             detailed node status
pmox cluster status                  cluster membership/quorum
pmox cluster resources [--type]      everything the cluster sees (vm|node|storage|...)

pmox vm list [--node N]              QEMU VMs (cluster-wide)
pmox vm status <vmid>                live status (node auto-resolved)
pmox vm config <vmid>                raw configuration
pmox vm describe <vmid>              consolidated view: status + config + snapshots + tasks
pmox vm ip <vmid> [--all] [--wait]   live IP(s): agent, static config, or same-LAN ARP scan

pmox vm set <vmid> -o key=val        update config (needs --dangerous; delete=key needs --yes)
pmox vm resize <vmid> --disk D --size [+]G    grow a disk (needs --dangerous)
pmox vm rename <vmid> <newname>      rename (needs --dangerous)
pmox vm tag <vmid> --add t1,t2       add tags; --remove / --set also available (needs --dangerous)

pmox vm start|shutdown|reboot|suspend|resume <vmid>      (needs --dangerous)
pmox vm stop|reset <vmid>                                (needs --dangerous --yes)
pmox vm new [name] [--image img | --from-template id]    create VM (needs --dangerous; see Provisioning)
pmox vm up <name> --image <name>                         ready-to-SSH VM (DHCP; --ip or [network] pool for a static IP)
pmox vm create <vmid> --node N [-o key=val ...]          low-level create
pmox vm clone <vmid> --newid <id> [--name X] [--full] [--target N]
pmox vm migrate <vmid> --target N [--online]             (needs --dangerous --yes)
pmox vm delete <vmid> [--purge]                          (needs --dangerous --yes)
pmox vm snapshot list|create|delete|rollback <vmid> ...

pmox ct ...                          same as `vm`, for LXC containers
pmox ct describe <ctid>              consolidated view of a container
pmox ct new [name] --template <t>    create container from template (needs --dangerous; see Provisioning)

pmox storage list [--node N]         storage usage
pmox storage content <id> [--node N]
pmox task list [--node N]            recent tasks (node auto-picked on single-node clusters)
pmox task status|log|wait <upid>     node parsed from the UPID; `wait` polls to completion

pmox image list                      list VM cloud-image catalog
pmox image list --ct [--node N]      list LXC container templates available on a node
pmox image pull <name|url> --storage S --node N [--as-template] [--checksum sha256:<hex>]
```

`--node` is optional almost everywhere — resolved from the VMID, parsed from a
UPID, or auto-picked on single-node clusters (multi-node errors list the
candidates). Using `vm ...` on a container VMID (or vice versa) errors with the
corrective command. List commands accept `--fields vmid,name,status` to trim
output to just those keys.

Global flags are **position-independent** — they work before or after the subcommand:
`--json/--no-json`, `--dangerous/--no-dangerous`, `--wait/--no-wait`, `--timeout <s>`
(default 600), `--dry-run`, `--host`, `--port`, `--token-id`, `--token-secret`,
`--verify-ssl/--no-verify-ssl`, `--config`. Provisioning commands always wait on
their internal steps; if a wait times out, resume with `pmox task wait <upid>`.
`PMOX_DANGEROUS=1` is honored from the real environment only — never from a
`.env` file — and `--no-dangerous` forces read-only regardless.

## Provisioning

`pmox` can build cloud-init VMs, containers, and golden templates in a single
command.

### VM from a cloud image (`vm new --image`)

```bash
# One-call cloud-init VM — downloads the image, creates the VM, injects SSH key + network:
pmox --dangerous vm new web \
    --image ubuntu-24.04 \
    --size small \
    --disk 50 \
    --ssh-key ~/.ssh/id_ed25519.pub \
    --ip dhcp \
    --wait

# Sizing profiles: small = 1 core / 1 GiB  ·  medium = 2 / 4 GiB  ·  large = 4 / 8 GiB
```

`--image` accepts a catalog name (e.g. `ubuntu-24.04`), an `https://` URL, or a
Proxmox volume ID. Cloud-init options: `--ssh-key` (repeatable), `--ip dhcp|<cidr>,gw=<ip>`,
`--ciuser`, `--cipassword`, `--nameserver`.

**Requirements:** PVE 8.2+ (8.4+ recommended). The target storage must have the
`import` content type enabled. pmox uses an API token, so it imports by volume ID
(absolute paths would need `root@pam`).

**Checksum verification is opt-in:** catalog images download over HTTPS without
integrity verification by default. Pre-pull with
`pmox --dangerous image pull ubuntu-24.04 --checksum sha256:<hex> ...` to verify;
the cached image is then reused by `vm new` / `vm up` / `--as-template`.

### Seamless one-shot VM (`vm up`)

Zero config — the VM gets its address via DHCP:

```
pmox --dangerous vm up web --image ubuntu-24.04 --wait
```

pmox routes the image import to a file-based storage, picks a disk storage
(local-lvm preferred; override with `--storage`), ensures an SSH key (generating
`~/.ssh/id_ed25519.pub` if absent; `--ssh-key` is repeatable and `~` is expanded),
and creates the VM. With DHCP the address isn't known on return — `pmox vm ip
<vmid> --wait` polls for it once a guest agent is available, or check your
router's leases.

For a **known static IP**, either pass `--ip 192.168.0.50/24,gw=192.168.0.1`, or
configure a pool once so pmox auto-allocates the lowest free address (scanning
existing static `ipconfigN` across the cluster — no guest agent required):

```toml
[network]
cidr = "192.168.0.0/24"
gateway = "192.168.0.1"
pool = "192.168.0.200-192.168.0.250"   # MUST be outside your DHCP scope
```

**Want a known IP on a DHCP VM?** Clone a template that already runs
`qemu-guest-agent` — the agent then reports the address, so no static IP or pool
is needed:

```
pmox --dangerous vm up web --from-template 9000 --wait
pmox vm ip <vmid> --wait   # polls until the agent reports the DHCP address
```

Build that agent template once (pmox stays token-only, so this part is yours):

```
pmox --dangerous vm up base --image ubuntu-24.04 --ip 192.168.0.250/24,gw=192.168.0.1 --wait
ssh ubuntu@192.168.0.250 "sudo apt-get update && sudo apt-get install -y qemu-guest-agent && sudo systemctl enable --now qemu-guest-agent"
pmox --dangerous --yes vm stop <vmid>
#   then convert it to a template: Proxmox UI -> Convert to template, or `qm template <vmid>` on the node
```

### Clone from a template (`vm new --from-template`)

```bash
pmox --dangerous vm new web --from-template 9000 --ssh-key ~/.ssh/id_ed25519.pub --ip dhcp --wait
```

Clones inherit the template's disk size — pass `--disk` or resize afterward.

### Container from a template (`ct new`)

```bash
pmox --dangerous ct new box \
    --template ubuntu-24.04 \
    --ssh-key ~/.ssh/id_ed25519.pub \
    --ip dhcp \
    --wait
```

`--template` accepts a catalog/aplinfo name or a `vztmpl` volume ID. Discover
available templates with `pmox image list --ct --node N`.

`--template-storage` (default `local`, stores the `vztmpl`) differs from
`--storage` (default `local-lvm`, the rootfs). SSH public keys are sent raw — no
manual encoding needed.

### Golden template (`image pull --as-template`)

```bash
pmox --dangerous image pull ubuntu-24.04 --storage local --node pve1 --as-template
# then clone it:
pmox --dangerous vm new web --from-template <id> --ssh-key ~/.ssh/id_ed25519.pub --ip dhcp --wait
```

`--as-template` conversion is **one-way**. Same PVE 8.2+/`import` content-type
requirements apply.

### Finding a guest's IP (`vm ip` / `ct ip`)

After creating a DHCP guest, read the address it actually got:

```bash
pmox vm ip 100              # primary IP + per-interface table
pmox vm ip 100 --all        # also show loopback, IPv6 link-local, and MACs
pmox ct ip 200              # same for containers
```

For VMs this tries three sources in order: the **QEMU guest agent**; the
static cloud-init `ipconfigN` config; and — for agent-less DHCP VMs — a
**same-LAN ARP scan** by the guest's MAC (`source: "arp"`). The scan nudges the
local subnet with empty UDP datagrams and reads the OS neighbor table, so it
needs pmox to run on the same L2 network as the VM's bridge (your desk: yes; over
a VPN: no), and it is IPv4-only. Containers report their interfaces directly,
so `ct ip` needs no agent. JSON output (the default when
piped) carries every interface and address; the table view hides loopback and
link-local unless you pass `--all`.

## Using with an AI (e.g. Claude)

Point the AI at the CLI and let it run commands via the shell. Because your shell
captures pmox's output, it **emits JSON automatically** — the model gets
structured output to parse with no flag, while you still see tables at your own
terminal. (Force it either way with `--json` / `--no-json`, or globally with
`PMOX_JSON=1` / `PMOX_JSON=0`.)

Tell the AI to run **`pmox guide`** first: it prints the complete agent guide —
safety model, exit codes, envelope shapes, provisioning recipes, and recovery
steps — so any agent self-onboards in one call, no plugin required.

- Leave dangerous mode **off** for exploration. The AI literally cannot change
  anything without you adding `--dangerous` (and `--yes` for destructive ops),
  so accidental damage is impossible during read-only investigation.

```bash
pmox health                          # AI explores freely, read-only (JSON auto)
pmox vm describe 100
pmox image list
```

When you *want* the AI to act, tell it to include the flags explicitly:

```bash
pmox --dangerous vm start 100
pmox --dangerous vm snapshot create 100 before-upgrade
```

## Claude Code plugin

This repo is also a Claude Code plugin (in [`plugin/`](plugin/)), so Claude can
drive `pmox` for you with the safety gates intact:

```
/plugin marketplace add lukebward/pmox
/plugin install pmox@pmox-marketplace
```

It adds a `proxmox` skill (auto-activates when you ask about your cluster) plus
`/pmox:cluster-status`, `/pmox:list-guests`, and `/pmox:run`. Install the CLI
first (`pipx install pmox`). See [`plugin/README.md`](plugin/README.md).

## Development & tests

The test-suite mocks the Proxmox API, so **no live cluster is required**.

```bash
pip install -e ".[dev]"
pytest
```

Tests cover config precedence, the safety gates, output formatting, the API
client's endpoint mapping, and the CLI end-to-end with an injected fake client.

## Architecture

```
pmox/
  config.py      Settings + precedence merge (file < env < flags)
  client.py      thin, injectable wrapper over proxmoxer (the only API surface)
  catalog.py     VM cloud-image catalog and container template discovery
  views.py       composite read queries (describe, health, guest lookup)
  provision.py   VM / container creation workflows + fail-fast validation
  ipam.py        token-only static IPv4 allocation (cluster config as ledger)
  output.py      Rich tables + plain JSON; byte/uptime/percent formatters
  safety.py      the two gates: require_dangerous() and confirm()
  errors.py      typed errors carrying structured envelope fields (upid, hint, ...)
  guide.py       the `pmox guide` text (agent onboarding)
  cli.py         Typer app wiring config + client + output + safety together
```

Each module has one job and a clear interface, which is what makes the whole
thing straightforward to test with mocks.

## License

[MIT](LICENSE) © 2026 Luke Ward
