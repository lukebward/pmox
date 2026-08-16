# pmox

A command-line tool for exploring and managing a [Proxmox VE](https://www.proxmox.com/)
cluster, built so an AI agent can drive it safely.

`pmox` wraps the Proxmox API with concise commands, pretty tables for humans, and
JSON for machines.

!!! danger "Read-only by default"

    Nothing changes without `--dangerous`, and nothing is destroyed without
    `--dangerous` **and** `--yes`. The two gates are independent, and neither can
    be enabled from a `.env` file. See [Safety model](safety.md).

![pmox demo](demo.gif)

```bash
pip install pmox
```

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **Quick start**

    ---

    Install, create an API token, and go from nothing to an SSH-able VM in one
    command.

    [:octicons-arrow-right-24: Get started](quick-start.md)

-   :material-shield-lock:{ .lg .middle } **Safety model**

    ---

    The two gates, the exit codes, and the JSON error envelopes that make the
    tool safe to hand to an agent.

    [:octicons-arrow-right-24: How the gates work](safety.md)

-   :material-console:{ .lg .middle } **Command reference**

    ---

    Every command, the global flags, and the conventions that let you skip
    `--node` almost everywhere.

    [:octicons-arrow-right-24: All commands](commands.md)

-   :material-server-plus:{ .lg .middle } **Provisioning**

    ---

    Cloud-init VMs, LXC containers, and the agent golden templates that make
    DHCP addresses reliable.

    [:octicons-arrow-right-24: Provisioning guide](provisioning.md)

-   :material-cog:{ .lg .middle } **Configuration**

    ---

    TOML file, environment variables, and flags — and the precedence order
    between them.

    [:octicons-arrow-right-24: Configure pmox](configuration.md)

-   :material-robot:{ .lg .middle } **Using with an agent**

    ---

    Why the JSON envelopes exist, and the Claude Code plugin that teaches the
    safety rules.

    [:octicons-arrow-right-24: Agent setup](agents.md)

</div>

## What it looks like

Output auto-detects its destination: tables at a terminal, JSON when captured or
piped. Force either way with `--json` / `--no-json`.

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

=== "Captured by an agent"

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

The `source` field says where the answer came from — `guest-agent`, the static
cloud-init `config`, or a same-LAN `arp` scan.

## Why not `qm` or `pvesh`

Both need a root shell on a node. `pmox` needs a scoped API token and runs from
any machine on your network — your laptop, a CI runner, or an agent's sandbox.
And unlike a raw API wrapper, it refuses to mutate anything until you say so
twice.

---

[MIT](https://github.com/lukebward/pmox/blob/main/LICENSE) © 2026 Luke Ward ·
[Changelog](https://github.com/lukebward/pmox/blob/main/CHANGELOG.md)
