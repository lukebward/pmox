---
description: List all Proxmox VMs and containers with status and resource usage.
argument-hint: "[running|stopped]  (optional state filter)"
allowed-tools: Bash(pmox:*), Bash(python:*)
---

List my Proxmox guests using the **read-only** `pmox` CLI (do NOT pass `--dangerous`).

Run `pmox vm list` and `pmox ct list` (JSON is emitted automatically), then present a single
combined table sorted by node, then VMID, with columns:
VMID · name · type (VM/CT) · node · status · CPU% · memory used / max.

If `$ARGUMENTS` is `running` or `stopped`, show only guests in that state.
Use `python -m pmox` if `pmox` is not on PATH.
