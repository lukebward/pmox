# Command reference

Read-only commands need no flags. State changes need the global `--dangerous`
flag. Destructive operations additionally need `--yes`, which is a
per-subcommand flag and goes after the subcommand:
`pmox --dangerous vm delete 100 --yes`.

!!! abstract "The full contract lives in [Safety model](safety.md)"

    Exit codes, the eight error codes, and the JSON envelope shapes are documented
    there. `pmox guide` prints the agent-oriented version of all of it in one
    call.

## All commands

```
pmox version                         client version + server version if reachable (global config errors can still exit 2 before any command runs)
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

pmox template list [--node N]        VM templates cluster-wide; agent ones marked
pmox template build <image> [--ip <cidr>,gw=<ip>] [--user U] [--vmid N] [--name X]
                                     agent golden template (needs --dangerous)
```

`--as-template` and `--checksum` don't combine — verify the checksum on a plain
pull first; the cached image is then reused by later provisioning commands.
`template build` is covered in depth under [Provisioning](provisioning.md#agent-templates-template-build).

## Two output modes

Every command emits Rich tables at a terminal and JSON when its output is
captured or piped. Nothing about the command changes — only the rendering.

=== "At a terminal"

    ```console
    $ pmox vm ip 113 --wait
    VM 113 (web) on lukeserver · primary 192.168.0.140
    ┌───────────┬───────────────┬──────┐
    │ Interface │ IPv4          │ IPv6 │
    ├───────────┼───────────────┼──────┤
    │ eth0      │ 192.168.0.140 │ -    │
    └───────────┴───────────────┴──────┘
    ```

=== "Captured or piped"

    ```json
    {
      "vmid": 113,
      "node": "lukeserver",
      "kind": "qemu",
      "name": "web",
      "source": "guest-agent",
      "primary": "192.168.0.140",
      "interfaces": [
        {
          "name": "eth0",
          "mac": "bc:24:11:aa:bb:cc",
          "addresses": [
            {"family": "ipv4", "address": "192.168.0.140", "prefix": 24, "scope": "global"}
          ]
        }
      ]
    }
    ```

Force one or the other with `--json` / `--no-json`, or `PMOX_JSON=1|0|auto`.
The table view hides loopback and IPv6 link-local addresses unless you pass
`--all`; JSON always carries every interface and address.

## Conventions

`--node` is optional almost everywhere — resolved from the VMID, parsed from a
UPID, or auto-picked on single-node clusters (multi-node errors list the
candidates). Using `vm ...` on a container VMID (or vice versa) errors with the
corrective command. List commands accept `--fields vmid,name,status` to trim
output to just those keys.

Global flags are position-independent — they work before or after the
subcommand:

`--json/--no-json`, `--dangerous/--no-dangerous`, `--wait/--no-wait`,
`--timeout <s>` (default 600), `--dry-run`, `--host`, `--port`, `--token-id`,
`--token-secret`, `--verify-ssl/--no-verify-ssl`, `--config`.

Provisioning commands always wait on their internal steps; if a wait times out,
resume with `pmox task wait <upid>`.

!!! warning "`PMOX_DANGEROUS=1` comes from the real environment only"

    Never from a `.env` file. `--no-dangerous` forces read-only regardless.

`vm up --image` rides agent templates by default: it clones the image's
template when present and builds it first when missing (see
[Provisioning](provisioning.md)). `--no-agent-template` forces a raw
image import for one call; `PMOX_AGENT_TEMPLATES=0` or
`[defaults] agent_templates = false` disables the behavior entirely.

`--dry-run` prints the exact API call as JSON and makes zero mutations, but it
still performs read calls to resolve nodes and VMIDs, so cluster connectivity
is required.
