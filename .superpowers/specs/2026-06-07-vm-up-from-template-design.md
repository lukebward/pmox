# `vm up --from-template`: seamless clone of an agent-baked template

- **Date:** 2026-06-07
- **Status:** Design approved; ready for implementation
- **Author:** Luke Ward (with Claude)

## Summary

Add `--from-template <vmid>` to `pmox vm up` as an alternative source to `--image`,
so the seamless flow (DHCP-default / auto-static IP, auto SSH key, result output)
works when **cloning a template** — not only when importing a cloud image.

This closes the DHCP-IP gap **token-only**: if you clone a golden template that
already runs `qemu-guest-agent`, the resulting DHCP VM reports its address, so
`pmox vm ip <vmid>` works without any static-IP configuration.

## Motivation

`vm up` currently only imports a cloud image (`--image`). Stock cloud images ship
no guest agent, so a DHCP `vm up` VM can't report its IP. Installing the agent
can't be done with an API token. The agreed token-only answer (see the earlier
brainstorm) is: the user builds a golden template with the agent once, and pmox
clones it. `vm new --from-template` can already clone, but without the seamless
conveniences. This brings those conveniences to the clone path.

## Design

### Command change (`pmox/cli.py`, the `_up` command)

- `--image` becomes **optional**; add `--from-template <vmid>`. **Exactly one** of
  the two is required and they are mutually exclusive — otherwise raise
  `ValueError("vm up needs exactly one of --image or --from-template.")`.

- **Source branch (the only new branching):**
  - `--from-template`: `target_node = --node or client.resolve_node(from_template)`
    (raise `LookupError` if the template can't be located); no import-storage step;
    build via the existing `provision.build_vm_clone_plan(...)`. `--size` /
    `--storage` / `--import-storage` don't apply (the clone inherits the template's
    hardware); `--disk` still resizes after the clone.
  - `--image`: unchanged — `_single_node_or_die`, import-storage resolution, and
    `provision.build_vm_image_plan(...)`.

- **Shared across both paths (unchanged from the DHCP-default work):**
  - IP resolution: explicit `--ip` → static; else `[network]` pool set →
    auto-allocate static via `ipam.allocate_ip`; else → **DHCP** (`ip=dhcp`,
    `chosen_ip = None`).
  - SSH key: resolved only **after** the `--dangerous` gate (never during
    `--dry-run`, which reads an existing key but never generates one).
  - Output: static → `VM <id> <name> ip <addr>` + `ssh` hint; DHCP → `... ip via
    DHCP (not known yet)` + a lookup hint; JSON mirrors with `ip`/`ssh` = `null`
    for DHCP.

- A local `_build(sshkeys)` closure returns the clone plan when `from_template` is
  set, else the image plan — so the dry-run, gate, execute, and output code stay
  single-path.

### Docs (`README.md`, `plugin/skills/proxmox/SKILL.md`)

A one-time recipe to build the agent template, then clone it:

1. `pmox --dangerous vm up base --image ubuntu-24.04 --ip <static> --wait`
2. `ssh <user>@<ip> "sudo apt-get update && sudo apt-get install -y qemu-guest-agent && sudo systemctl enable --now qemu-guest-agent"`
3. Stop it and convert to a template (Proxmox UI → Convert to template, or `qm
   template <vmid>` on the node).
4. Thereafter: `pmox --dangerous vm up web --from-template <vmid> --wait` →
   DHCP VM with the agent; `pmox vm ip <vmid>` returns the address.

## Error handling

| Condition | Behavior |
|-----------|----------|
| Neither `--image` nor `--from-template` | `ValueError` → exit 1 |
| Both `--image` and `--from-template` | `ValueError` → exit 1 |
| `--from-template` VMID not in the cluster | `LookupError` → exit 1 |
| Standard gates | create → needs `--dangerous` |

## Testing

TDD; the repo's 100% line-coverage gate is held (`.venv/Scripts/python -m pytest`).

- Clone path with a pool → auto-static: `clone_guest` + `update_config`
  (`ipconfig0`, `sshkeys`) correct; node resolved from the template.
- Clone path, no pool, no `--ip` → DHCP (`update_config` `ipconfig0 == "ip=dhcp"`).
- `--from-template` with an unresolvable VMID → exit 1.
- Neither source → exit 1; both sources → exit 1.
- Existing `--image` `vm up` tests remain green (image path unchanged).

## Out of scope

Installing the agent for the user (node/guest SSH, cloud-init snippets), and a
`vm template` convert command — both deliberately excluded to keep pmox
token-only. The template build is the user's one-time step.
