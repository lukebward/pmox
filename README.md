# pmox
(AI tools helped build this project)

[![PyPI](https://img.shields.io/pypi/v/pmox)](https://pypi.org/project/pmox/)
[![Python](https://img.shields.io/pypi/pyversions/pmox)](https://pypi.org/project/pmox/)
[![License](https://img.shields.io/pypi/l/pmox)](LICENSE)

**Status:** alpha. Commands and flags can change before 1.0.

A command-line tool that explores and manages a [Proxmox VE](https://www.proxmox.com/)
cluster, built so an AI agent can drive it safely.

`pmox` wraps the Proxmox API with concise commands, pretty tables for humans, and
JSON for machines. It is **read-only by default**: it changes nothing without
`--dangerous`, and it destroys nothing without `--dangerous` and `--yes`.

Docs: [Commands](docs/commands.md) · [Configuration](docs/configuration.md) ·
[Provisioning](docs/provisioning.md) · or run `pmox guide`.

![pmox demo](https://raw.githubusercontent.com/lukebward/pmox/main/docs/demo.gif)

```
pmox health
pmox vm describe 100

# Cloud-init VM, ready to SSH, in one command:
pmox --dangerous vm up web --image ubuntu-24.04 --wait

pmox --dangerous vm start 100
pmox --dangerous vm delete 100 --yes
```

## Why pmox

- `qm`, `pct`, and `pvesh` need a root shell on a Proxmox node. pmox needs one
  scoped API token and runs from any machine on your network.
- MCP servers add a process to run and configure. pmox is a plain CLI: anything
  with shell access can drive it, and the JSON envelopes make it easy to wrap
  in an MCP server if you prefer.
- An AI agent gets guardrails by default: read-only mode until `--dangerous`,
  confirmation for destructive commands, stable error codes, and a built-in
  guide (`pmox guide`).

pmox is not for everything:

- Use Terraform or Ansible when you want declarative state.
- Use `pvesh` when you need an API endpoint pmox does not cover.

## Quick start

pmox requires Python 3.11+, Proxmox VE 8.x, and an API token.

```bash
pip install pmox
```

Create the token in Proxmox under *Datacenter → Permissions → API Tokens* (for a
homelab, uncheck "Privilege Separation" so it inherits the user's permissions).
Put the connection details in a `.env` file where you run pmox, or use a TOML
file, environment variables, or flags ([docs/configuration.md](docs/configuration.md)):

```ini
PROXMOX_HOST=192.168.1.10
PROXMOX_TOKEN_ID=root@pam!pmox
PROXMOX_TOKEN_SECRET=00000000-0000-0000-0000-000000000000
PROXMOX_VERIFY_SSL=false    # self-signed homelab cert
```

Confirm it connects, then create your first VM:

```bash
pmox health                 # quorum, node load, storage, guest counts
pmox --dangerous vm up web --image ubuntu-24.04 --wait    # prints the new VMID
pmox vm ip <vmid> --wait    # agent-reported DHCP address, in seconds
ssh ubuntu@<ip>
```

The first `vm up` per image takes a few minutes: it builds a golden template
that includes `qemu-guest-agent`, then clones it. Every later `vm up` clones
in seconds, and `vm ip` gets its answer from the guest agent: no scans, no
guessing. On a multi-node cluster add `--node <name>`; cloud-image
provisioning needs PVE 8.2+ and a storage with the `import` content type.
Delete the VM again with `pmox --dangerous vm delete <vmid> --yes`.

## Safety model

Two independent gates protect the cluster:

| Gate | Flag | Applies to | Default |
|------|------|-----------|---------|
| Dangerous mode | `--dangerous` (or `PMOX_DANGEROUS=1`) | any state change: power, create, edit, clone, migrate, snapshot | off (read-only) |
| Confirmation | `--yes` | destructive ops: `delete`, `stop`, `reset`, `migrate`, `rollback`, snapshot `delete`, `set` with a `delete=` key | required when non-interactive |

`--dangerous` is global; `--yes` belongs to the subcommand and goes after it:
`pmox --dangerous vm delete 100 --yes`.

When pmox runs non-interactively (e.g. under an agent), it refuses a destructive
command without `--yes` instead of waiting at a prompt. Failures come back as
JSON envelopes with stable error codes, and successes carry machine-readable
fields (`vmid`, `upid`, ...). The exit code distinguishes "needs `--dangerous`"
(4) from "needs `--yes`" (3); see [docs/commands.md](docs/commands.md) or run
`pmox guide`.

## Using with an AI agent

Point the agent at the CLI and let it run commands through the shell:

- pmox emits JSON automatically when something captures its output: the agent
  parses structure while you still see tables at your own terminal.
- Tell the agent to run `pmox guide` first: the complete guide (safety model,
  envelopes, recipes, recovery) in one call, so it onboards itself without a
  plugin.
- Leave dangerous mode off while it explores. Without `--dangerous` (and
  `--yes` for destructive ops) the agent cannot change anything.
- When you want changes, say so explicitly and have it add the flags:
  `pmox --dangerous vm start 100`.

### Claude Code plugin

This repo is also a Claude Code plugin ([`plugin/`](plugin/)) that teaches
Claude the safety rules:

```
/plugin marketplace add lukebward/pmox
/plugin install pmox@pmox-marketplace
```

It adds a `proxmox` skill that activates when you ask about your cluster, plus
`/pmox:cluster-status`, `/pmox:list-guests`, and `/pmox:run`. Install the CLI
first (`pip install pmox`). Permission details: [`plugin/README.md`](plugin/README.md).

## Commands

The most-used commands. The full reference, global flags, and exit codes live
in [docs/commands.md](docs/commands.md):

```
pmox health                           cluster health triage
pmox guide                            the built-in agent guide
pmox vm list / ct list                all guests, cluster-wide
pmox vm describe 100                  status + config + snapshots + tasks
pmox vm ip 100 --wait                 live IP: agent, cloud-init config, or ARP
pmox template build ubuntu-24.04      agent golden template      (--dangerous)
pmox template list                    templates, agent ones marked
pmox vm set 100 -o cores=4            edit config                (--dangerous)
pmox vm snapshot create 100 pre       snapshots                  (--dangerous)
pmox vm start 100                     also shutdown/reboot/...   (--dangerous)
pmox vm stop 100 --yes                also delete/reset/...      (--dangerous)
pmox storage list / task list / image list
```

`ct` mirrors `vm` for containers. You rarely need `--node`: pmox resolves it
from the VMID, the UPID, or the cluster. Add `--dry-run` to any change to
preview the exact API call without making it.

## Provisioning

One command from cloud image to SSH-able, agent-backed VM:

```bash
pmox --dangerous vm up web --image ubuntu-24.04 --wait
```

pmox downloads (and caches) the image, picks storage, injects your SSH key (it
generates one if needed), and boots with DHCP. On the first run per image it
also builds an **agent golden template**: a one-time VM where pmox installs
`qemu-guest-agent` over SSH, cleans it up, and converts it to a tagged
template. Every `vm up --image` after that clones the template, so the guest
agent answers `pmox vm ip <vmid> --wait` reliably, on any network. Build the
template explicitly (or with a static bootstrap address) via
`pmox --dangerous template build ubuntu-24.04`; opt out with
`--no-agent-template` or `agent_templates = false`.

The other modes (details and walkthroughs in
[docs/provisioning.md](docs/provisioning.md)):

```bash
# Fully specified cloud-init VM:
pmox --dangerous vm new web --image ubuntu-24.04 --size small --disk 50 \
     --ssh-key ~/.ssh/id_ed25519.pub --ip dhcp --wait

# LXC container from a template:
pmox --dangerous ct new box --template ubuntu-24.04 --ip dhcp --wait

# Golden template, then clone it:
pmox --dangerous image pull ubuntu-24.04 --storage local --node pve1 --as-template
pmox --dangerous vm new web --from-template <vmid> --ip dhcp --wait

# The address a DHCP guest actually got:
pmox vm ip <vmid> --wait    # guest agent, cloud-init config, or same-LAN ARP scan
```

Sizing profiles: `small` 1 core / 1 GiB · `medium` 2 / 4 · `large` 4 / 8.
Static addresses via `--ip <cidr>,gw=<ip>` or a `[network]` pool in config.
Image checksum verification is opt-in (`image pull --checksum sha256:<hex>`).

## Development

The test suite mocks the Proxmox API, so you need no live cluster:

```bash
git clone https://github.com/lukebward/pmox.git && cd pmox
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

## Architecture

```
pmox/
  config.py      settings + precedence merge (file < env < flags)
  client.py      thin, injectable wrapper over proxmoxer (the only API surface)
  catalog.py     cloud-image catalog and sizing profiles
  arp.py         same-LAN ARP discovery for agent-less DHCP guests
  views.py       composite read queries (describe, health, guest IP lookup)
  provision.py   VM / container creation workflows + fail-fast validation
  ipam.py        token-only static IPv4 allocation (cluster config as ledger)
  output.py      Rich tables + plain JSON; byte/uptime/percent formatters
  safety.py      the two gates: require_dangerous() and confirm()
  errors.py      typed errors carrying structured envelope fields
  guide.py       the `pmox guide` text (agent onboarding)
  cli.py         Typer app wiring it all together
```

## Support and contributing

Questions and bug reports: [open an issue](https://github.com/lukebward/pmox/issues).
Pull requests are welcome; run `pytest` first. Release history lives in the
[changelog](CHANGELOG.md).

## License

[MIT](LICENSE) © 2026 Luke Ward
