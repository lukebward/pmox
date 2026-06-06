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
  containers, nodes, storage, or snapshots. Claude runs `pmox` for you, staying
  read-only unless you explicitly ask for a change.
- **`/pmox:cluster-status`** — quick health overview of the whole cluster.
- **`/pmox:list-guests [running|stopped]`** — list all VMs and containers.
- **`/pmox:run <args>`** — run any pmox command and have Claude interpret it.

The plugin never bypasses pmox's safety gates: Claude only adds `--dangerous`
(and `--yes` for destructive actions) when you explicitly ask it to.
