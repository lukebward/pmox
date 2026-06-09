---
description: List all Proxmox VMs and containers with status and resource usage.
argument-hint: "[running|stopped]  (optional state filter)"
allowed-tools: Bash(pmox:*), Bash(python -m pmox:*)
---

List my Proxmox guests using the **read-only** `pmox` CLI (do NOT pass `--dangerous`).

Run `pmox vm list` and `pmox ct list` (JSON is emitted automatically), then present a single
combined table sorted by node, then VMID, with columns:
VMID · name · type (VM/CT) · node · status · CPU% · memory used / max.

If `$ARGUMENTS` is `running` or `stopped`, show only guests in that state.

For deeper detail on a specific guest, run `pmox vm describe <id>` (or
`pmox ct describe <id>`) — this returns a consolidated view of status, config,
snapshots, and recent tasks in a single read-only call.

Use `python -m pmox` if `pmox` is not on PATH.
