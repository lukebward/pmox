# Using with an AI agent

This is what pmox was built for. Point the agent at the CLI and let it run
commands through the shell:

- **Output adapts on its own.** pmox detects when its output is captured and
  emits JSON automatically — the agent parses structure while you still see
  tables at your own terminal.
- **Tell it to run `pmox guide` first.** The complete guide (safety model,
  envelopes, recipes, recovery) arrives in one call, so the agent onboards
  itself without a plugin and without crawling `--help` screens.
- **Leave dangerous mode off while it explores.** Without `--dangerous` (and
  `--yes` for destructive ops) the agent *cannot* change anything.
- **Arm it explicitly.** When you want changes made, say so and have it add the
  flags: `pmox --dangerous vm start 100`.

!!! quote "From the built-in guide"

    Never try to work around the gates. If you get exit 3 or 4, surface the
    `need` field to the user and ask before retrying with the flag.

## No MCP server required

Anything with shell access can drive pmox, and the JSON envelopes make it
straightforward to wrap in an MCP server if you prefer. Unlike `qm` or `pvesh`,
it needs no root shell on a node: just a scoped API token, from any machine on
your network.

That property is what makes it safe to hand to a sandboxed agent — the blast
radius is whatever the token can reach, and the gates sit in front of that.

## Claude Code plugin

The repo is also a [Claude Code](https://claude.com/claude-code) plugin that
teaches Claude the safety rules up front:

```
/plugin marketplace add lukebward/pmox
/plugin install pmox@pmox-marketplace
```

It adds a `proxmox` skill that activates when you ask about your cluster, plus
three slash commands:

| Command | What it does |
|---------|--------------|
| `/pmox:cluster-status` | Read-only overview of nodes, guests, and health |
| `/pmox:list-guests` | All VMs and containers with status and resource usage |
| `/pmox:run` | Run an arbitrary pmox command and interpret the output |

!!! note "Install the CLI first"

    The plugin drives `pmox`; it doesn't bundle it. `pip install pmox`, then
    configure a token as in the [Quick start](quick-start.md).

Permission details are in
[`plugin/README.md`](https://github.com/lukebward/pmox/blob/main/plugin/README.md).

## Recovering from a partial failure

Provisioning is multi-step, and a failure partway through can leave a real guest
behind. The envelope says exactly what completed, what failed, and what to do:

```json
{"ok": false, "error": "error",
 "message": "Provisioning failed at 'start guest': ...",
 "vmid": 114, "node": "pve1",
 "completed_steps": ["import disk", "create guest"],
 "failed_step": "start guest",
 "hint": "vm 114 was created on pve1 but provisioning stopped at 'start guest'. Inspect with `pmox vm describe 114`; finish manually or delete it before retrying (a plain retry would create a second guest under a new VMID)."}
```

!!! danger "Read the hint before retrying"

    pmox does **not** roll the guest back. When one was created, an agent that
    retries the same command lands a duplicate under a new VMID.

    The `hint` field distinguishes the two cases for you: if nothing was created
    yet it says so explicitly — *"No guest was created yet, so retrying the same
    command is safe."*
