---
description: Read-only overview of the Proxmox cluster (nodes, guests, health).
allowed-tools: Bash(pmox:*), Bash(python:*)
---

Give me a concise health overview of my Proxmox cluster using the **read-only**
`pmox` CLI. Do NOT pass `--dangerous`.

Run and interpret (use `--json` for parsing):

- `pmox --json cluster status`
- `pmox --json nodes list`
- `pmox --json vm list`
- `pmox --json ct list`

Then report:
- Which nodes are online/offline.
- Per-node CPU and memory usage.
- How many VMs and containers are running vs stopped.
- Anything that looks unhealthy (offline nodes, very high resource use).

If `pmox` is not on PATH, use `python -m pmox` instead. If it reports a config
error, tell me exactly which environment variables to set.
