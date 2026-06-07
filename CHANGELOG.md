# Changelog

All notable changes to pmox are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/lukebward/pmox/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/lukebward/pmox/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/lukebward/pmox/releases/tag/v0.1.0
