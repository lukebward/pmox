# Quick start

Requires Python 3.11+, Proxmox VE 8.x, and an API token.

## 1. Install

```bash
pip install pmox
```

## 2. Create an API token

In Proxmox, go to *Datacenter → Permissions → API Tokens*.

!!! tip "Homelab shortcut"

    Uncheck **Privilege Separation** so the token inherits the user's
    permissions. For anything shared, give the token only the privileges it
    needs instead.

## 3. Point pmox at your cluster

Put the connection details in a `.env` file where you run pmox — or use a TOML
file, environment variables, or flags ([Configuration](configuration.md)):

``` { .ini .annotate title=".env" }
PROXMOX_HOST=192.168.1.10
PROXMOX_TOKEN_ID=root@pam!pmox
PROXMOX_TOKEN_SECRET=00000000-0000-0000-0000-000000000000
PROXMOX_VERIFY_SSL=false    # (1)!
```

1.  Verification is off by default because homelab Proxmox uses self-signed
    certificates. Set it to `true` if your node has a CA-signed cert.

## 4. Confirm it connects

```bash
pmox health
```

That is a read-only command — it reports quorum, node load, storage pressure,
and guest counts without touching anything.

## 5. Create your first VM

``` { .bash .annotate }
pmox --dangerous vm up web --image ubuntu-24.04 --wait    # (1)!
pmox vm ip <vmid> --wait                                  # (2)!
ssh ubuntu@<ip>
```

1.  `--dangerous` is required for any state change. It is a global flag, so it
    can go before or after the subcommand.
2.  The agent-reported DHCP address, usually in seconds.

!!! info "The first run per image is slow — on purpose"

    `vm up` builds a **golden template** with `qemu-guest-agent` baked in, then
    clones it. That takes a few minutes once. Every later `vm up` clones in
    seconds, and `vm ip` gets its answer from the guest agent — no scans, no
    guessing. See [Provisioning](provisioning.md#agent-templates-template-build).

On a multi-node cluster add `--node <name>`. Cloud-image provisioning needs
PVE 8.2+ and a storage with the `import` content type.

## 6. Tear it down

```bash
pmox --dangerous vm delete <vmid> --yes
```

!!! danger "`--yes` is not optional here"

    `delete` is destructive, so it needs both gates. Run non-interactively
    without `--yes` and pmox refuses with exit code 3 rather than hanging at a
    prompt. See [Safety model](safety.md).

## Where to go next

- [Safety model](safety.md) — the two gates, exit codes, and error envelopes
- [Command reference](commands.md) — everything pmox can do
- [Provisioning](provisioning.md) — templates, static IPs, containers
- [Using with an AI agent](agents.md) — the part this tool was built for
