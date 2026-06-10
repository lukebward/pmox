# Command reference

Read-only commands need no flags. State changes need the global `--dangerous`
flag. Destructive operations additionally need `--yes`, which is a
per-subcommand flag and goes after the subcommand:
`pmox --dangerous vm delete 100 --yes`.

`pmox guide` prints the agent-oriented version of this page (safety model,
envelopes, recipes, recovery) in one call.

## All commands

```
pmox version                         Proxmox version of the connected node
pmox guide                           print the built-in agent/automation guide
pmox health                          one-shot cluster health triage (read-only)
pmox nodes list                      nodes + CPU/mem/uptime
pmox nodes status <node>             detailed node status
pmox cluster status                  cluster membership/quorum
pmox cluster resources [--type]      everything the cluster sees (vm|node|storage|...)

pmox vm list [--node N]              QEMU VMs (cluster-wide)
pmox vm status <vmid>                live status (node auto-resolved)
pmox vm config <vmid>                raw configuration
pmox vm describe <vmid>              consolidated view: status + config + snapshots + tasks
pmox vm ip <vmid> [--all] [--wait]   live IP(s): agent, static config, or same-LAN ARP scan

pmox vm set <vmid> -o key=val        update config (needs --dangerous; delete=key needs --yes)
pmox vm resize <vmid> --disk D --size [+]G    grow a disk (needs --dangerous)
pmox vm rename <vmid> <newname>      rename (needs --dangerous)
pmox vm tag <vmid> --add t1,t2       add tags; --remove / --set also available (needs --dangerous)

pmox vm start|shutdown|reboot|suspend|resume <vmid>      (needs --dangerous)
pmox vm stop|reset <vmid>                                (needs --dangerous --yes)
pmox vm new [name] [--image img | --from-template id]    create VM (needs --dangerous)
pmox vm up <name> --image <name>                         ready-to-SSH VM (DHCP; --ip or [network] pool for static)
pmox vm create <vmid> --node N [-o key=val ...]          low-level create
pmox vm clone <vmid> --newid <id> [--name X] [--full] [--target N]
pmox vm migrate <vmid> --target N [--online]             (needs --dangerous --yes)
pmox vm delete <vmid> [--purge]                          (needs --dangerous --yes)
pmox vm snapshot list|create|delete|rollback <vmid> ...

pmox ct ...                          same as `vm`, for LXC containers
pmox ct describe <ctid>              consolidated view of a container
pmox ct new [name] --template <t>    create container from template (needs --dangerous)

pmox storage list [--node N]         storage usage
pmox storage content <id> [--node N]
pmox task list [--node N]            recent tasks (node auto-picked on single-node clusters)
pmox task status|log|wait <upid>     node parsed from the UPID; `wait` polls to completion

pmox image list                      list VM cloud-image catalog
pmox image list --ct [--node N]      list LXC container templates available on a node
pmox image pull <name|url> --storage S --node N [--as-template | --checksum sha256:<hex>]
```

`--as-template` and `--checksum` don't combine — verify the checksum on a plain
pull first; the cached image is then reused by later provisioning commands.

## Conventions

`--node` is optional almost everywhere — resolved from the VMID, parsed from a
UPID, or auto-picked on single-node clusters (multi-node errors list the
candidates). Using `vm ...` on a container VMID (or vice versa) errors with the
corrective command. List commands accept `--fields vmid,name,status` to trim
output to just those keys.

Global flags are position-independent — they work before or after the
subcommand: `--json/--no-json`, `--dangerous/--no-dangerous`, `--wait/--no-wait`,
`--timeout <s>` (default 600), `--dry-run`, `--host`, `--port`, `--token-id`,
`--token-secret`, `--verify-ssl/--no-verify-ssl`, `--config`.

Provisioning commands always wait on their internal steps; if a wait times out,
resume with `pmox task wait <upid>`. `PMOX_DANGEROUS=1` is honored from the
real environment only — never from a `.env` file — and `--no-dangerous` forces
read-only regardless.

`--dry-run` prints the exact API call as JSON and makes zero mutations, but it
still performs read calls to resolve nodes and VMIDs, so cluster connectivity
is required.

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
provisioning failures include `vmid`, `completed_steps`, and `failed_step`. A
guest that was already created is not cleaned up — follow the `hint` rather
than blindly retrying, or a second guest will be created.

Successful mutations return structured results — the created VMID is always a
field, never just prose:

```json
{"ok": true, "op": "qemu.up", "vmid": 112, "node": "pve1", "name": "web",
 "ip": null, "ssh": null, "hint": "..."}
```

With `--wait`, the `upid` is replaced by a `task` object holding the final task
status.
