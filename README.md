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
pmox --dangerous --yes vm delete 100
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

When running non-interactively (e.g. an AI calling the CLI), a destructive op
*without* `--yes` is refused rather than silently prompted. Exit codes:
`0` ok · `1` error · `2` config · `3` confirmation required · `4` read-only.

Under `--json`, errors are a JSON envelope `{"ok": false, "error": "...", "need": [...], "message": "..."}`.

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
pmox health                          One-shot cluster health triage (read-only)
pmox nodes list                      nodes + CPU/mem/uptime
pmox nodes status <node>             detailed node status
pmox cluster status                  cluster membership/quorum
pmox cluster resources [--type]      everything the cluster sees (vm|node|storage|...)

pmox vm list [--node N]              QEMU VMs (cluster-wide)
pmox vm status <vmid>                live status (node auto-resolved)
pmox vm config <vmid>                raw configuration
pmox vm describe <vmid>              consolidated view: status + config + snapshots + tasks

pmox vm set <vmid> -o key=val        update config (needs --dangerous; delete=key needs --yes)
pmox vm resize <vmid> --disk D --size [+]G    grow a disk (needs --dangerous)
pmox vm rename <vmid> <newname>      rename (needs --dangerous)
pmox vm tag <vmid> --add t1,t2       add tags; --remove / --set also available (needs --dangerous)

pmox vm start|shutdown|reboot|suspend|resume <vmid>      (needs --dangerous)
pmox vm stop|reset <vmid>                                (needs --dangerous --yes)
pmox vm new [name] [--image img | --from-template id]    create VM (needs --dangerous; see Provisioning)
pmox vm create <vmid> --node N [-o key=val ...]          low-level create
pmox vm clone <vmid> --newid <id> [--name X] [--full] [--target N]
pmox vm migrate <vmid> --target N [--online]             (needs --dangerous --yes)
pmox vm delete <vmid> [--purge]                          (needs --dangerous --yes)
pmox vm snapshot list|create|delete|rollback <vmid> ...

pmox ct ...                          same as `vm`, for LXC containers
pmox ct describe <ctid>              consolidated view of a container
pmox ct new [name] --template <t>    create container from template (needs --dangerous; see Provisioning)

pmox storage list [--node N]         storage usage
pmox storage content <id> --node N
pmox task list --node N              recent tasks
pmox task status|log <upid> --node N

pmox image list                      list VM cloud-image catalog
pmox image list --ct --node N        list LXC container templates available on a node
pmox image pull <name|url> --storage S --node N [--as-template]   download image (needs --dangerous)
```

`--node` is optional for guest commands — `pmox` finds which node a VMID lives on
via the cluster resources endpoint.

Global flags are **position-independent** — they work before or after the subcommand:
`--json/--no-json`, `--dangerous`, `--wait/--no-wait`, `--timeout <s>` (default 600),
`--dry-run`, `--host`, `--port`, `--token-id`, `--token-secret`, `--verify-ssl/--no-verify-ssl`,
`--config`.

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

**No-checksum caveat:** catalog images are downloaded over HTTPS without checksum
verification (no warning in v1). For integrity, supply `--image <url>` from a
trusted source or a pre-verified image.

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

## Using with an AI (e.g. Claude)

Point the AI at the CLI and let it run commands via the shell. Because your shell
captures pmox's output, it **emits JSON automatically** — the model gets
structured output to parse with no flag, while you still see tables at your own
terminal. (Force it either way with `--json` / `--no-json`, or globally with
`PMOX_JSON=1` / `PMOX_JSON=0`.)

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
  views.py       composite read queries (describe, health)
  provision.py   VM / container creation workflows (cloud-init, clones, ct new)
  output.py      Rich tables + plain JSON; byte/uptime/percent formatters
  safety.py      the two gates: require_dangerous() and confirm()
  cli.py         Typer app wiring config + client + output + safety together
```

Each module has one job and a clear interface, which is what makes the whole
thing straightforward to test with mocks.

## License

[MIT](LICENSE) © 2026 Luke Ward
