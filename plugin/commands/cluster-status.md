---
description: Read-only overview of the Proxmox cluster (nodes, guests, health).
allowed-tools: Bash(pmox:*), Bash(python:*)
---

Give me a concise health overview of my Proxmox cluster using the **read-only**
`pmox` CLI. Do NOT pass `--dangerous`.

Run and interpret (output is JSON automatically when captured):

- `pmox cluster status`
- `pmox nodes list`
- `pmox vm list`
- `pmox ct list`

Then report:
- Which nodes are online/offline.
- Per-node CPU and memory usage.
- How many VMs and containers are running vs stopped.
- Anything that looks unhealthy (offline nodes, very high resource use).

If `pmox` is not on PATH, use `python -m pmox` instead. If it reports a config
error, tell me exactly which environment variables to set.
