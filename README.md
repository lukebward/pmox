# pmox

A friendly, **AI-friendly** command-line tool to explore and manage a [Proxmox VE](https://www.proxmox.com/) cluster.

`pmox` wraps the Proxmox API with clean commands, pretty tables for humans, and a
`--json` mode for machines. It is **read-only by default** so you (or an AI) can
explore safely, with two layers of protection before anything can change.

```
pmox nodes list
pmox vm list
pmox vm status 100
pmox --dangerous vm start 100
pmox --dangerous vm delete 100 --yes
```

## Safety model

Two independent gates protect your cluster:

| Gate | Flag | Applies to | Default |
|------|------|-----------|---------|
| **Dangerous mode** | `--dangerous` (or `PMOX_DANGEROUS=1`) | *any* state change (power, create, delete, clone, migrate, snapshot) | **off — read-only** |
| **Confirmation** | `--yes` | *destructive* ops: `delete`, `stop`, `reset`, `migrate`, `rollback` | required when non-interactive |

So:

- **Explore** with no flags — nothing can be modified.
- **Change** something benign (e.g. `start`) — add `--dangerous`.
- **Destroy** something (e.g. `delete`) — add `--dangerous` **and** `--yes`.

When running non-interactively (e.g. an AI calling the CLI), a destructive op
*without* `--yes` is refused rather than silently prompted. Exit codes:
`0` ok · `1` error · `2` config · `3` confirmation required · `4` read-only.

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
pmox version                      Proxmox version of the connected node
pmox nodes list                   nodes + CPU/mem/uptime
pmox nodes status <node>          detailed node status
pmox cluster status               cluster membership/quorum
pmox cluster resources [--type]   everything the cluster sees (vm|node|storage|...)

pmox vm list [--node N]           QEMU VMs (cluster-wide)
pmox vm status <vmid>             live status (node auto-resolved)
pmox vm config <vmid>             configuration
pmox vm start|shutdown|reboot|suspend|resume <vmid>      (needs --dangerous)
pmox vm stop|reset <vmid>                                (needs --dangerous --yes)
pmox vm create <vmid> --node N [--name X] [-o key=value ...]
pmox vm clone <vmid> --newid <id> [--name X] [--full] [--target N]
pmox vm migrate <vmid> --target N [--online]            (needs --dangerous --yes)
pmox vm delete <vmid> [--purge]                          (needs --dangerous --yes)
pmox vm snapshot list|create|delete|rollback <vmid> ...

pmox ct ...                       same as `vm`, for LXC containers
pmox storage list [--node N]      storage usage
pmox storage content <id> --node N
pmox task list --node N           recent tasks
pmox task status|log <upid> --node N
```

`--node` is optional for guest commands — `pmox` finds which node a VMID lives on
via the cluster resources endpoint.

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
pmox cluster resources           # AI explores freely, read-only (JSON auto)
pmox vm status 100
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
  config.py    Settings + precedence merge (file < env < flags)
  client.py    thin, injectable wrapper over proxmoxer (the only API surface)
  output.py    Rich tables + plain JSON; byte/uptime/percent formatters
  safety.py    the two gates: require_dangerous() and confirm()
  cli.py       Typer app wiring config + client + output + safety together
```

Each module has one job and a clear interface, which is what makes the whole
thing straightforward to test with mocks.

## License

[MIT](LICENSE) © 2026 Luke Ward
