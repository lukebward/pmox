# pmox — Claude Code plugin

Lets Claude Code explore and manage your Proxmox VE cluster through the `pmox`
CLI, with **read-only-by-default** safety.

## Prerequisite: install the pmox CLI

The plugin drives the `pmox` command, so install it first (from the repo root):

```bash
pipx install .        # recommended: puts `pmox` on PATH globally
# or
pip install -e .      # editable; `pmox` available in that Python environment
```

Then configure credentials (see the main [README](../README.md)): set
`PROXMOX_HOST`, `PROXMOX_TOKEN_ID`, and `PROXMOX_TOKEN_SECRET` in your
environment or a `.env` file.

## Install the plugin

```
/plugin marketplace add lukebward/pmox
/plugin install pmox@pmox-marketplace
```

(`/plugin marketplace add` accepts a GitHub `owner/repo`, a git URL, or a local
path to the repo root, where `.claude-plugin/marketplace.json` lives.)

## What you get

- **Skill `proxmox`** — auto-activates when you ask about your cluster, VMs,
  containers, nodes, storage, or snapshots. The skill covers:
  - **Health and triage** — `pmox health` for a one-shot cluster overview.
  - **Describe** — `pmox vm describe <id>` / `pmox ct describe <id>` for a
    consolidated view of any guest (status, config, snapshots, recent tasks).
  - **Edit** — set config keys, resize disks, rename guests, manage tags.
  - **Provisioning** — one-call cloud-init VMs (`vm new --image`), ready
    containers (`ct new --template`), and golden templates (`image pull
    --as-template`).
  Claude runs `pmox` for you, staying read-only unless you explicitly ask for a
  change.
- **`/pmox:cluster-status`** — quick health overview of the whole cluster.
- **`/pmox:list-guests [running|stopped]`** — list all VMs and containers.
- **`/pmox:run <args>`** — run any pmox command and have Claude interpret it.

The plugin never bypasses pmox's safety gates: Claude only adds `--dangerous`
(and `--yes` for destructive actions) when you explicitly ask it to.

## Permission model — read before installing

The skill pre-approves `Bash(pmox:*)` and `Bash(python -m pmox:*)` (and nothing
else), so pmox commands run without per-call permission prompts. Note what that
means: **the prefix match cannot see flags**, so a `pmox --dangerous vm delete
100 --yes` is auto-approved at the Claude Code layer too — once installed, the
protection against unwanted changes is pmox's own gates plus the skill's rules
(Claude adds `--dangerous`/`--yes` only when you explicitly ask). Because
pmox's global flags are position-independent, a `deny: Bash(pmox --dangerous:*)`
rule would not be reliable either. If you want a hard prompt on every mutation,
remove the `allowed-tools` lines from the skill and command files after
installing — you'll then be asked before each pmox invocation instead.
