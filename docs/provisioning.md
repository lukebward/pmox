# Provisioning guide

`pmox` builds cloud-init VMs, containers, and golden templates in a single
command.

**Requirements:** PVE 8.2+ (8.4+ recommended) for `vm new --image` and
`image pull`. The target storage must have the `import` content type enabled.
pmox uses an API token, so it imports by volume ID (absolute paths would need
`root@pam`).

**Checksum verification is opt-in:** catalog images download over HTTPS without
integrity verification by default. Pre-pull with
`pmox --dangerous image pull ubuntu-24.04 --checksum sha256:<hex> ...` to
verify; the cached image is then reused by `vm new` / `vm up` / `--as-template`.

## One-shot VM (`vm up`)

No network config needed — the VM gets its address via DHCP:

```
pmox --dangerous vm up web --image ubuntu-24.04 --wait
```

pmox routes the image import to a file-based storage, picks a disk storage
(local-lvm preferred; override with `--storage`), ensures an SSH key (generating
`~/.ssh/id_ed25519.pub` if absent; `--ssh-key` is repeatable and `~` is
expanded), and creates the VM.

By default this rides the **agent template** for the image: if one exists on
the node it is cloned (your `--size`/`--disk`/`--storage` still apply); if not,
`vm up` builds it first — a one-time job of a few minutes, announced as it
happens — and then clones it. Either way `pmox vm ip <vmid> --wait` is
answered by the clone's guest agent in seconds. Opt out per call with
`--no-agent-template` (raw image import, agent-less), or globally with
`PMOX_AGENT_TEMPLATES=0` / `[defaults] agent_templates = false`.

## Agent templates (`template build`)

The agent template is what makes DHCP IPs reliable: a golden image with
`qemu-guest-agent` installed, built once per image per node:

```
pmox --dangerous template build ubuntu-24.04
```

What it does, in order: create a VM from the image (cloud-init injects the SSH
key pmox manages) → SSH in and install + enable `qemu-guest-agent` → clean the
guest for cloning (`cloud-init clean`, truncate `/etc/machine-id`, remove SSH
host keys — so clones get fresh identities and DHCP leases) → verify the agent
answers through the Proxmox API → shut down → tag `pmox-agent` +
`img-<image>` → convert to a template. It is idempotent: when a matching
template already exists, it is reported and reused.

To reach the build VM over SSH, pmox tries — in order, none of it relying on
ARP — the VM's MAC-derived IPv6 link-local address (a pure function of the
config, resolved via NDP), then the static IPv4 you assigned, then DHCP
discovery as a last resort:

- `--ip 192.168.0.250/24,gw=192.168.0.1` pins a static bootstrap address
  (released back to DHCP before conversion).
- A configured `[network]` pool allocates one automatically.
- With neither, the build VM uses DHCP and pmox finds it via link-local.

`--user` sets the SSH login for images whose default user pmox doesn't know
(catalog images are known: `ubuntu`, `debian`); `--vmid`/`--name` pin identity;
`--size`/`--disk`/`--storage` shape the template (clones can resize). This is
the one pmox operation that reaches inside a guest — over SSH, with the key it
injected moments earlier. Containers don't need any of this: `ct ip` reads
interfaces directly.

Provisioning defaults can live in config so you don't repeat them: `ssh_key`,
`ciuser`, and `import_storage` in the TOML file, or
`PROXMOX_DEFAULT_SSH_KEY`, `PROXMOX_DEFAULT_CIUSER`, and
`PROXMOX_DEFAULT_IMPORT_STORAGE` in the environment.

### Static addresses

For a known static IP, either pass `--ip 192.168.0.50/24,gw=192.168.0.1`, or
configure a pool once so pmox auto-allocates the lowest free address (scanning
existing static `ipconfigN` across the cluster — no guest agent required):

```toml
[network]
cidr = "192.168.0.0/24"
gateway = "192.168.0.1"
pool = "192.168.0.200-192.168.0.250"   # MUST be outside your DHCP scope
```

### A known IP on a DHCP VM

The default `vm up --image` flow already handles this — clones of the agent
template report their DHCP address via the guest agent. To clone a specific
template VMID instead (e.g. one you built by hand):

```
pmox --dangerous vm up web --from-template 9000 --wait
pmox vm ip <vmid> --wait   # polls until the agent reports the DHCP address
```

## VM from a cloud image (`vm new --image`)

The fully specified form:

```bash
pmox --dangerous vm new web \
    --image ubuntu-24.04 \
    --size small \
    --disk 50 \
    --ssh-key ~/.ssh/id_ed25519.pub \
    --ip dhcp \
    --wait

# Sizing profiles: small = 1 core / 1 GiB  ·  medium = 2 / 4 GiB  ·  large = 4 / 8 GiB
```

`--image` accepts a catalog name (e.g. `ubuntu-24.04`), an `https://` URL, or a
Proxmox volume ID. Cloud-init options: `--ssh-key` (repeatable),
`--ip dhcp|<cidr>,gw=<ip>`, `--ciuser`, `--cipassword`, `--nameserver`.

## Clone from a template (`vm new --from-template`)

```bash
pmox --dangerous vm new web --from-template 9000 --ssh-key ~/.ssh/id_ed25519.pub --ip dhcp --wait
```

Clones inherit the template's disk size — pass `--disk` or resize afterward.

## Container from a template (`ct new`)

```bash
pmox --dangerous ct new box \
    --template ubuntu-24.04 \
    --ssh-key ~/.ssh/id_ed25519.pub \
    --ip dhcp \
    --wait
```

`--template` accepts a catalog/aplinfo name or a `vztmpl` volume ID. Discover
available templates with `pmox image list --ct --node N`.

`--template-storage` (default `local`, stores the `vztmpl`) differs from
`--storage` (the rootfs — auto-detected when omitted, local-lvm preferred).
SSH public keys are sent raw — no manual encoding needed.

## Golden template (`image pull --as-template`)

```bash
pmox --dangerous image pull ubuntu-24.04 --storage local --node pve1 --as-template
# then clone it:
pmox --dangerous vm new web --from-template <id> --ssh-key ~/.ssh/id_ed25519.pub --ip dhcp --wait
```

`--as-template` conversion is **one-way**. Same PVE 8.2+/`import` content-type
requirements apply. (`--checksum` applies to a plain pull only — verify first,
then convert a separate pull, or reuse the verified cache.)

## Finding a guest's IP (`vm ip` / `ct ip`)

After creating a DHCP guest, read the address it actually got:

```bash
pmox vm ip 100              # primary IP + per-interface table
pmox vm ip 100 --all        # also show loopback, IPv6 link-local, and MACs
pmox ct ip 200              # same for containers
```

For VMs this tries three sources in order: the QEMU guest agent; the static
cloud-init `ipconfigN` config; and — for agent-less DHCP VMs — a same-LAN ARP
scan by the guest's MAC (`source: "arp"`). The scan nudges the local subnet
with empty UDP datagrams and reads the OS neighbor table, so it needs pmox to
run on the same L2 network as the VM's bridge (your desk: yes; over a VPN: no),
and it is IPv4-only. Containers report their interfaces directly, so `ct ip`
needs no agent. JSON output (the default when piped) carries every interface
and address; the table view hides loopback and link-local unless you pass
`--all`.
