# Safety model

Two independent gates protect the cluster. Read-only commands need neither.

| Gate | Flag | Applies to | Default |
|------|------|-----------|---------|
| Dangerous mode | `--dangerous` (or `PMOX_DANGEROUS=1`) | any state change: power, create, edit, clone, migrate, snapshot | off — read-only |
| Confirmation | `--yes` | destructive ops: `delete`, `stop`, `reset`, `migrate`, `rollback`, snapshot `delete`, `set` with a `delete=` key | required when non-interactive |

## Where the flags go

`--dangerous` is global and position-independent. `--yes` belongs to the
subcommand and goes after it:

```bash
pmox --dangerous vm delete 100 --yes
```

!!! warning "`PMOX_DANGEROUS=1` is honored from the real environment only"

    A `.env` file **cannot** enable dangerous mode. This is deliberate: dropping
    a `.env` into a working directory should never silently arm every command in
    it. `--no-dangerous` forces read-only regardless of the environment.

## Non-interactive behavior

When output is captured — an agent, a CI job, a pipe — a destructive command
without `--yes` is *refused* rather than left hanging at a prompt. You get exit
code 3 and a structured envelope naming the flag you need.

!!! tip "Preview any change"

    Add `--dry-run` to a mutating command to print the exact API call as JSON
    without making it. It still performs read calls to resolve nodes and VMIDs,
    so cluster connectivity is required.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | error or network failure |
| 2 | config missing **or** CLI usage error (the envelope's `error` field tells them apart) |
| 3 | operation needs `--yes` |
| 4 | operation needs `--dangerous` |

## JSON envelopes

Under `--json` (or whenever output is captured), errors are a structured
envelope:

```json
{"ok": false, "error": "read_only", "need": ["--dangerous"], "message": "..."}
```

`error` is one of six fixed codes:

| Code | Exit | Meaning |
|------|------|---------|
| `read_only` | 4 | needs `--dangerous` |
| `confirm_required` | 3 | needs `--yes` |
| `config` | 2 | credentials not configured / config file invalid |
| `usage` | 2 | bad command line (typo'd flag or subcommand) |
| `network` | 1 | can't reach the Proxmox API (DNS/TLS/timeout) |
| `error` | 1 | general error |

Envelopes carry machine-actionable fields where relevant: task failures include
`upid` and `node` plus a `hint` (resume with `pmox task wait <upid>`); partial
provisioning failures include `vmid`, `completed_steps`, and `failed_step`.

!!! danger "Do not blindly retry a failed provision"

    A guest that was already created is **not** cleaned up. A second attempt
    creates a second guest under a new VMID. Follow the envelope's `hint` —
    `describe` the guest, then finish manually or delete it first.

### Successful mutations

Successes carry machine-readable fields. The created VMID is always a field,
never just prose:

```json
{"ok": true, "op": "qemu.up", "vmid": 112, "node": "pve1", "name": "web",
 "ip": null, "ssh": null, "hint": "..."}
```

With `--wait`, the `upid` is replaced by a `task` object holding the final task
status.

!!! note "Read the fields, not the message"

    `message` is prose for humans and may change between releases. `vmid`,
    `node`, `upid`, `op`, and `error` are the stable contract.
