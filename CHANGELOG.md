# Changelog

All notable changes to pmox are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/lukebward/pmox/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/lukebward/pmox/releases/tag/v0.1.0
