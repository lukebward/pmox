# Changelog

All notable changes to pmox are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.7.0] - 2026-06-10

One-shot agent-backed VMs: the first `vm up --image X` builds a golden
template with `qemu-guest-agent` baked in, and every later `vm up` clones it —
so `vm ip --wait` is answered by the agent in seconds instead of relying on
ARP scans.

### Added

- **`pmox template build <image>`**: build an agent-enabled golden template —
  boot a VM from a cloud image, SSH in with the key pmox manages, install
  `qemu-guest-agent`, clean the guest for cloning (cloud-init state,
  machine-id, SSH host keys), tag it (`pmox-agent` + `img-<image>`), and
  convert it to a template. Idempotent (reuses an existing template). The one
  pmox operation that reaches inside a guest — over SSH, using the key it
  injected moments earlier.
- **`vm up --image` default flow**: clones the image's agent template
  automatically (honoring `--size`/`--disk`/`--storage` on the clone); builds
  it first when missing (one-time, announced in human mode). The success
  envelope gains `template`, `agent`, and `template_built` fields. Opt out
  per-call with `--no-agent-template`, per-environment with
  `PMOX_AGENT_TEMPLATES=0`, or per-config with `[defaults] agent_templates = false`.
- **`pmox template list`**: VM templates cluster-wide, marking agent-enabled ones.
- **ARP-free SSH bootstrap**: the build reaches the guest via its MAC-derived
  IPv6 link-local address first (EUI-64 — a pure function of the config, NDP
  resolution, immune to ARP spoofing and DHCP state), then the assigned static
  IPv4, with DHCP discovery only as a last resort. SSH auth failures retry for
  a window because cloud images socket-activate sshd before cloud-init has
  written `authorized_keys`.
- Catalog images now carry their default login user (`ubuntu`/`debian`) for
  the build's SSH step; `template build --user` overrides for URL/volid images.

### Changed

- README rewritten around a quick start and the agent workflow; the full
  command, provisioning, and configuration references moved to `docs/`.

### Fixed

- README inaccuracies: the agent-template walkthrough placed `--yes` before
  the subcommand (the placement the safety docs themselves forbid); config
  precedence read as env < file instead of file < env; `vm ip --wait` was
  described as agent-only (the ARP fallback shipped in 0.6.0); `ct new
  --storage` was documented as a hardcoded `local-lvm` default instead of
  auto-detect; the architecture list omitted `arp.py` and misattributed
  container-template discovery to `catalog.py`.

## [0.6.0] - 2026-06-10

### Added

- **Same-LAN ARP discovery for `vm ip`**: when the guest agent is unavailable
  and no static cloud-init address exists, pmox now matches the VM's MAC
  against the local ARP table (nudging the subnet with empty UDP datagrams on
  a miss) and reports the address with `source: "arp"`. Automatic — no flag —
  for the `vm ip` / `vm ip --wait` path only (`describe` never scans). Works
  when pmox runs on the same L2 network as the guest; IPv4 only; the candidate
  subnet comes from `[network] cidr` or a /24 around the local outbound IP
  (capped at /22). Closes the loop for DHCP VMs created from stock cloud
  images, which don't ship `qemu-guest-agent`.

### Changed

- `vm up` DHCP hints now point at `pmox vm ip <vmid> --wait` (agent or
  same-LAN ARP scan) instead of the router's DHCP leases.
- When an ARP sweep ran and found nothing, the `vm ip` error notes that, so
  the message reflects exactly what was tried.

## [0.5.0] - 2026-06-09

Agent-ergonomics release: every error an agent can hit now lands as a
machine-actionable JSON envelope, mutations return structured results, and
recovery from interrupted provisioning is a documented, tool-assisted path.

### Added

- **`pmox guide`**: the full agent/automation guide (safety model, exit codes,
  envelope shapes, recipes, recovery) built into the CLI — any agent can
  self-onboard in one call, no plugin needed.
- **`pmox task wait <upid>`**: resume waiting on a server-side task (e.g. after
  a `--timeout` or an interrupted shell); the node is parsed from the UPID.
  `task status` / `task log` also no longer require `--node`.
- **`vm ip --wait` / `ct ip --wait`**: poll until the guest reports an address
  (bounded by `--timeout`), retrying through agent-not-up errors — completes
  the DHCP + guest-agent flow after `vm up --from-template`.
- **`--fields a,b,c`** on all list commands: project rows onto a key subset
  (missing keys are `null`) to keep output small on big clusters.
- **`--no-dangerous`**: force read-only mode even when `PMOX_DANGEROUS` is set.
- **`image pull --checksum <algo>:<hexdigest>`**: verify catalog/URL downloads;
  the verified, cached image is reused by `vm new` / `vm up` / `--as-template`.
- Structured **success envelopes** for all mutations:
  `{ok, message, op, vmid, node, upid | task, hint}` — `vm new`, `ct new`,
  `image pull --as-template` and `vm up` return the created VMID as a field
  (previously only embedded in the message prose).
- Structured **error envelopes**: task failures/timeouts carry `upid`, `node`
  and a `hint`; partial provisioning failures carry `vmid`, `completed_steps`,
  `failed_step` and a recovery `hint` (created guests are not cleaned up and a
  blind retry would duplicate them — the hint says what to do instead).
- New error codes in the envelope: **`usage`** (exit 2 — bad command line,
  distinguishing it from `config`) and **`network`** (exit 1 — concise
  DNS/TLS/timeout diagnosis instead of a urllib3 exception wall).
- `--storage` for `vm new` / `vm up` / `ct new` now **auto-detects** a capable
  storage when omitted (local-lvm preferred, validated when explicit) instead
  of hardcoding `local-lvm`.
- Fail-fast validation before any download/create: guest names (DNS-label
  rules — underscores rejected), `--ip` syntax, and `--storage` content types.
- Node auto-pick for `task list`, `storage content` and `image list --ct` on
  single-node clusters; multi-node errors list the candidate node names.

### Fixed

- Unknown VMIDs now produce a proper JSON error envelope (previously plain
  text on stderr, breaking the documented JSON contract) with an actionable
  message.
- Using `vm ...` on a container VMID (or `ct ...` on a VM) now errors with the
  corrective command instead of a misleading Proxmox 500 about a missing
  config file.
- `vm up --ip dhcp` no longer reports the literal string `"dhcp"` as the
  address (`ip` is now `null`, with a hint for finding the real address).
- `--ssh-key ~/...` is tilde-expanded in `vm new` / `ct new` (PowerShell and
  quoted shell arguments pass `~` through literally); missing key files get an
  actionable error. `vm up --ssh-key` is now repeatable like `vm new`'s.
- Malformed TOML config files now exit 2 with a `config` envelope instead of
  an unhandled `TOMLDecodeError` traceback.
- CT template name matching prefers an exact match, then the newest
  `-standard` build, then the newest substring match (previously: first
  substring hit in aplinfo order), shared by `ct new` and `image pull --ct`.
- `PMOX_DANGEROUS` is honored only from the real environment — a `.env` file
  in the working directory can no longer silently enable dangerous mode.
- `vm new` / `vm up` warn when `--from-template` ignores `--size` /
  `--storage` / `-o` options.

### Changed

- Success envelopes replace the untyped `result` field with typed `upid` /
  `task` / `result` fields (breaking for consumers of the old `"result"` key).
- Plugin: `allowed-tools` no longer pre-approves arbitrary `python` commands —
  only `pmox` and `python -m pmox`. The permission trade-off of the blanket
  `pmox` allow is now documented in `plugin/README.md`.

## [0.4.0] - 2026-06-07

### Added

- `vm up`: one-call, token-only creation of a ready-to-SSH VM. Auto-allocates a
  static IP from a configured pool (scanning the cluster's existing static
  `ipconfigN` as the ledger), or falls back to **DHCP** with zero config. Bakes in
  an SSH key (generating `~/.ssh/id_ed25519.pub` if absent), routes the cloud-image
  import to a file-based storage, resizes the disk, and boots. `--from-template
  <vmid>` clones an existing template (e.g. one with the guest agent baked in)
  through the same flow; `--image` imports a cloud image; `--ip` sets an explicit
  static address. Exactly one of `--image` / `--from-template` is required.
- `[network]` config table (`cidr`, `gateway`, `pool`, `nameserver`) and a
  `[defaults]` table (`import_storage`, `ssh_key`, `ciuser`), read from a TOML
  config file or `PROXMOX_NET_*` / `PROXMOX_DEFAULT_*` environment variables. Used
  by `vm up` for static-IP auto-allocation; entirely optional (DHCP otherwise).
- `vm ip`: when the QEMU guest agent is unavailable, fall back to the static IP
  declared in the guest's cloud-init `ipconfigN` config (`source: "config"`), so
  statically addressed VMs report their IP without an agent.
- `--import-storage` option on `vm new` / `vm up` to choose the storage that holds
  an imported cloud image (auto-detected when omitted).
- New internal `ipam` module: token-only static IPv4 allocation using the
  cluster's own static `ipconfigN` as the ledger (no local state file).

### Fixed

- `vm new --image <catalog|url>` no longer fails with `can't upload to storage
  type 'lvmthin', not a file based storage!` when the disk storage is LVM-thin:
  the cloud-image import is now auto-routed to a file-based storage that has the
  `import` content type, separate from the (block) disk storage.

## [0.3.0] - 2026-06-07

### Added

- `vm ip` / `ct ip`: read a guest's live IP address(es) — VMs via the QEMU
  guest agent, containers via the LXC interfaces endpoint. Filtered by default
  (`--all` adds loopback, IPv6 link-local, and MACs); JSON returns the full
  per-interface data. The same info now also appears in `describe`.

## [0.2.0] - 2026-06-06

### Added

- `health` command: one-shot cluster health triage (read-only).
- `vm describe` / `ct describe`: consolidated view of a guest — status,
  config, snapshots, and recent tasks in a single call.
- `vm set` / `ct set`: update guest configuration via `key=value` options;
  `delete=<key>` requires `--yes` (delete-guard).
- `vm resize` / `ct resize`: grow a guest disk (grow-only).
- `vm rename` / `ct rename`: rename a VM or container.
- `vm tag` / `ct tag`: add, remove, or replace tags on a guest.
- `vm new`: create a VM in three modes — blank shell, cloud-init server
  (`--image <name|url|volid>`), or clone (`--from-template <vmid>`). Supports
  sizing profiles (`--size small|medium|large`), auto-VMID, and single-node
  auto-pick. Cloud-init options: `--ssh-key` (repeatable), `--ip`,
  `--ciuser`, `--cipassword`, `--nameserver`.
- `ct new`: create a ready-to-SSH LXC container from a catalog or volid
  template (`--template`, required). Supports `--size`, `--disk`, `--storage`,
  `--template-storage`, `--node`, `--vmid`, `--ssh-key`, `--ip`, `--password`.
- `image` command group:
  - `image list`: list the VM cloud-image catalog; `--ct --node <N>` lists
    LXC container templates live from a node.
  - `image pull`: download a VM cloud image or (`--ct`) an LXC template to
    storage; `--as-template` converts to a reusable golden VM template.
    Requires `--dangerous`.
- Position-independent global flags — `--json`/`--no-json`, `--dangerous`,
  `--wait`/`--no-wait`, `--timeout`, `--dry-run`, and connection flags work
  before **or** after the subcommand.
- `--wait` / `--no-wait`: wait for the resulting Proxmox task to finish and
  report its outcome.
- `--timeout <s>`: seconds to wait when `--wait` is active (default 600).
- `--dry-run`: print the intended API call as JSON and exit; performs no
  mutations (read API calls still execute for cluster connectivity).
- Machine-readable JSON error envelope under `--json`:
  `{"ok": false, "error": "read_only", "need": ["--dangerous"], "message": "..."}`.
  `error` is one of `read_only` (4), `confirm_required` (3), `config` (2), `error`
  (1); the `need` array (present only for `read_only`/`confirm_required`) lists the
  flag to add (`--dangerous` or `--yes`).
- New internal modules: `catalog` (cloud-image and container-template
  catalogue), `views` (consolidated describe output), `provision` (VM/CT
  creation and cloud-init wiring).

## [0.1.0] - 2026-06-06

Initial release.

### Added

- Proxmox VE CLI with `version`, `nodes`, `cluster`, `vm`, `ct`, `storage`, and
  `task` commands. Guest commands work cluster-wide and auto-resolve the owning
  node from the cluster resources endpoint.
- Two-tier safety model: read-only by default; any state change requires
  `--dangerous`, and destructive ops (`delete`, `stop`, `reset`, `migrate`,
  `rollback`) additionally require `--yes`. Non-zero exit codes distinguish
  error (1), config (2), confirmation-required (3), and read-only (4).
- Auto-detecting output: Rich tables at an interactive terminal, JSON when piped
  or captured. Override with `--json` / `--no-json`, or `PMOX_JSON` (`1`/`0`/`auto`).
- Configuration via environment variables, a `.env` file, a TOML config file, or
  CLI flags (precedence: file < env < flags).
- Claude Code plugin (`plugin/`) with a `proxmox` skill and the
  `/pmox:cluster-status`, `/pmox:list-guests`, and `/pmox:run` commands.

[Unreleased]: https://github.com/lukebward/pmox/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/lukebward/pmox/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/lukebward/pmox/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/lukebward/pmox/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/lukebward/pmox/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/lukebward/pmox/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/lukebward/pmox/releases/tag/v0.1.0
