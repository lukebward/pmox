---
name: proxmox
description: Use when the user wants to inspect or manage their Proxmox VE cluster - VMs, LXC containers, nodes, storage, snapshots, tasks, or cluster health. Drives the `pmox` command-line tool (read-only by default; explicit gates before any change).
allowed-tools: Bash(pmox:*), Bash(python:*)
---

# Managing Proxmox with the `pmox` CLI

`pmox` is a command-line tool for exploring and managing a Proxmox VE cluster.
Use it whenever the user asks about their Proxmox cluster, VMs, containers,
nodes, storage, snapshots, or tasks.

## Invoking it

Run `pmox <args>`. If `pmox` is not on PATH, fall back to `python -m pmox <args>`.
Add `--json` whenever you need to parse output reliably:

```
pmox --json vm list
```

If a command fails with a **config error** (exit 2), the user hasn't set
credentials — tell them to set `PROXMOX_HOST`, `PROXMOX_TOKEN_ID`, and
`PROXMOX_TOKEN_SECRET` (or create a `.env`). pmox reads these automatically.

## Safety model — IMPORTANT

pmox has two independent gates. **Respect them; never try to work around them.**

1. **Read-only by default.** Inspection always works. Any state change
   (start/stop, create, delete, clone, migrate, snapshot) requires the global
   `--dangerous` flag.
2. **Confirmation for destructive ops.** `delete`, `stop`, `reset`, `migrate`,
   and snapshot `rollback`/`delete` *also* require `--yes`.

**Rules for you:**
- Default to read-only. For pure exploration, never pass `--dangerous`.
- Add `--dangerous` only when the user explicitly asks to change something.
- Add `--yes` (together with `--dangerous`) only when the user explicitly asks
  for a destructive action — and confirm the target with them first.
- Exit codes: `0` ok · `1` error · `2` config · `3` needs `--yes` · `4` read-only.

## Command cheat-sheet

Inspect (always safe):
```
pmox version
pmox cluster status
pmox cluster resources [--type vm|node|storage]
pmox nodes list
pmox nodes status <node>
pmox vm list [--node N]          # use `ct list` for containers
pmox vm status <vmid>            # node auto-resolved from the cluster
pmox vm config <vmid>
pmox vm snapshot list <vmid>
pmox storage list
pmox task list --node <node>
```

Change (need `--dangerous`):
```
pmox --dangerous vm start <vmid>
pmox --dangerous vm clone <vmid> --newid <id> [--full]
pmox --dangerous vm create <vmid> --node <node> --name <name> -o key=value
pmox --dangerous vm snapshot create <vmid> <name>
```

Destroy (need `--dangerous --yes`):
```
pmox --dangerous vm stop <vmid> --yes
pmox --dangerous vm delete <vmid> --yes
pmox --dangerous vm migrate <vmid> --target <node> --yes
pmox --dangerous vm snapshot rollback <vmid> <name> --yes
```

`<vmid>` works for both VMs (`vm ...`) and containers (`ct ...`); the node is
resolved automatically, so `--node` is optional for guest commands.
