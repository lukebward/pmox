# pmox 0.7.2 "trust patch" — design

**Date:** 2026-08-22
**Status:** approved-pending-review
**Scope class:** patch release. No new command surface. Every change either fixes a broken
contract, makes a failure teach its fix, or repairs a small trust-denting rough edge.

## 1. Context

A full audit (code, docs, design history, and live runs against the real 2-node cluster)
found that pmox's core design holds up — gates, envelopes, and plan-based provisioning all
behave exactly as documented — but several of its *promises* are currently broken:

1. **The usage-error JSON envelope is dead code.** `cli.py` imports the real `click`
   package when present, but typer >= 0.26 parses with its vendored `typer._click` and
   raises *its* exception classes. The `except click.exceptions.ClickException` in
   `main()` never matches, so every parse error (typo'd command, missing VMID, misplaced
   `--yes`) leaks a ~40-line Rich traceback with exit 1 instead of the documented
   `{"error": "usage"}` envelope with exit 2. `tests/test_cli.py::
   test_main_unknown_command_json_envelope` fails in any venv that has real click
   installed (mkdocs pulls it in). The original design spec called flag placement "the
   single most common invocation error" — that error currently gets the least parseable
   output the CLI can produce.
2. **Config failures betray instead of teach.** A non-numeric `PROXMOX_PORT` crashes with
   a raw `ValueError` traceback (exit 1, not the config envelope with exit 2). An
   *explicitly* passed `--config`/`PMOX_CONFIG` path that does not exist is silently
   ignored. `load_dotenv()` with default arguments resolves the `.env` relative to the
   installed package, so an editable install picks up the repo's credentials from any
   cwd (live-verified: pmox connected to the real cluster from an unrelated directory
   with a scrubbed environment) while a normal pip install never finds the cwd `.env` at
   all.
3. **The error taxonomy strands agents at the edges.** Guest-not-found and auth failures
   (401/403) both collapse into the catch-all `"error"` code, so an agent cannot
   distinguish "re-list, don't retry" from "credentials are wrong, stop" without parsing
   prose — which the guide explicitly forbids. And `confirm()` keys on *stdin* being a
   TTY while JSON mode keys on *stdout*, so a harness that captures stdout but leaves
   stdin a TTY gets a confirmation prompt written into the captured stream and hangs
   forever.
4. **`pmox health` is blind to the worst failures.** Warnings cover only CPU/mem/storage
   pressure. Lost quorum, offline nodes, and unavailable storage never appear, and
   repeated task failures are invisible — the live cluster currently has five
   consecutive failed `aptupdate` runs on lukeserver while `health` reports
   `"warnings": []`.
5. **Small trust dents:** the human `task list` truncates the UPID (the one value you
   need to copy) to 48 chars; `ct snapshot create` advertises a `--vmstate` flag the LXC
   API does not support; `pmox version` needs network and fails exactly where a new user
   first types it; help text and human output mojibake under cp1252 on Windows; roughly
   half the commands (including every destructive one) have blank help descriptions; and
   `plugin.json` says 0.3.0 while the CLI is 0.7.1.

## 2. Goals and non-goals

**Goals:** restore the documented error contract everywhere; make misconfiguration
produce a config envelope that names the fix; make `health` catch real failures; ship the
small repairs that make daily use less annoying. Simple upgrades only — nothing here
changes the safety model except to make it fail *faster* and *clearer*.

**Non-goals (deferred to later releases):** `pmox schema` and generated docs, the
`retryable` envelope field, `vm describe` output slimming, `_pct` field renaming,
name-based addressing, shell completion, watch/follow modes, `pmox init`/`doctor`,
backup, bulk selectors, template lifecycle. An MCP server is **permanently out of
scope** by owner decision — the "wrap it yourself" stance in `docs/agents.md` stands.

## 3. Design

### 3.1 Parse-error boundary

- Invert the import preference so `click` is always *the click typer parses with*:

  ```python
  try:  # typer >= 0.26 vendors click; that is the module whose exceptions typer raises
      from typer import _click as click
  except ImportError:  # older typer parses with the real package
      import click
  ```

  This alone re-arms `main()`'s existing handlers and `_handle_parse_error`'s
  `isinstance` checks in every environment, because both now reference the same
  hierarchy typer raises from. No duck-typing needed.
- Construct the root app with `pretty_exceptions_enable=False` so anything that still
  escapes prints a plain traceback rather than a Rich wall.
- The dangling-group precheck and `_handle_parse_error` stay as they are; `--help` and
  `--version` (SystemExit paths) are untouched.

**Regression proofing:** add subprocess-level tests (run `python -m pmox` as a child
process, no mocking of the import machinery) asserting exit 2 plus a parseable
`{"ok": false, "error": "usage", ...}` envelope for the three live-failing probes:
`pmox nonexistent-cmd`, `pmox vm list --bogus`, `pmox --dangerous --yes vm delete 100`,
plus `pmox --json vm status` (missing argument). Add a CI matrix leg that additionally
`pip install click` before running the suite — the exact condition that broke the
boundary — so the bug class cannot silently return with a future typer bump.

### 3.2 Config trust

- **Typed coercion errors:** wrap the coercion calls in `_coerce` so a `ValueError`
  becomes `ConfigError("PROXMOX_PORT must be a number (got 'abc')")` naming the actual
  source key. Flows through the existing `except ConfigError` in `main_callback` →
  config envelope, exit 2. Applies identically to TOML values.
- **Explicit config path must exist:** `_load_config_file` returns `{}` for a missing
  path only when the path is the *implicit* default (`~/.config/pmox/config.toml` with
  no `PMOX_CONFIG` set). When the path came from `--config` or `PMOX_CONFIG`, a missing
  file raises `ConfigError(f"Config file not found: {path}")`. `load_settings` learns
  which case it is in (it already receives `config_path` for the flag; treat a set
  `PMOX_CONFIG` as explicit too).
- **`.env` from the cwd:** replace `load_dotenv()` with
  `load_dotenv(find_dotenv(usecwd=True))` so discovery walks up from where the user ran
  pmox, never from the installed package location. Behavior change, called out in the
  changelog: an editable install no longer leaks the repo's credentials into unrelated
  directories, and a pip/pipx install finally honors a cwd `.env` as the README
  promises. The `PMOX_DANGEROUS`-before-dotenv ordering is preserved untouched.
- **Kill the dead pointer:** the missing-config `ConfigError` message drops
  "See .env.example." (not shipped in the wheel) in favor of
  "See https://lukebward.github.io/pmox/configuration/".

### 3.3 Error taxonomy and the confirm hang

- **`not_found` (exit 1):** new typed error raised by the guest-lookup helpers
  (`guest_not_found`, vmid→node resolution). Envelope keeps today's helpful message and
  gains the machine code, so agents re-list instead of retrying.
- **`auth` (exit 1):** `error_boundary` learns to classify authentication failures —
  an exception whose status code (or leading message token) is 401 or 403 — into
  `{"error": "auth", "hint": "Token rejected. token-id looks like user@realm!name and
  the secret is the token secret, not the account password. Note: an under-privileged
  token often shows as EMPTY lists, not errors."}`. Detection is duck-typed
  (`status_code` attribute, else message prefix) so it does not depend on proxmoxer
  internals.
- **Fail-fast confirm:** the CLI decides interactivity itself and passes it explicitly:
  `interactive = stdin_is_tty() and stdout_is_tty() and not json_mode`. When any of
  those is false, `confirm()` raises `ConfirmationRequired` immediately — exit 3,
  `need: ["--yes"]` — instead of writing a prompt into a captured stream and blocking.
  `safety.confirm`'s signature is unchanged; only the caller's `interactive` argument
  changes.
- **Docs:** the error-code lists in `guide.py`, `plugin/skills/proxmox/SKILL.md`,
  `docs/safety.md`, and `docs/agents.md` go from six codes to eight in the same PR.
  SKILL.md's description of the misplaced-`--yes` failure is rewritten to match the
  restored reality (usage envelope, exit 2).

### 3.4 Health triage

`summarize_health` gains a structured `issues` list — additive, `warnings` (flat
strings) keeps its exact current entries and also receives one string per new issue, so
existing consumers see strictly more, never differently:

```json
"issues": [{"code": "task_failures", "severity": "warning", "node": "lukeserver",
            "message": "last 5 aptupdate runs failed; pmox task log UPID:..."}]
```

Detectors, all from endpoints the client already wraps:

- `quorum_lost` (critical) when the cluster status reports `quorate` false.
- `node_offline` (critical) for each node whose status is not `online` — and an offline
  node's `cpu: 0` no longer reads as calm, because the issue names it.
- `storage_unavailable` (warning) when a storage resource's `status` is present and not
  `active`/`available` (today only fullness is checked).
- `task_failures` (warning): per node, scan the same recent-task window `describe`
  already uses; any task *type* whose last 3+ consecutive runs all ended non-OK is
  flagged, message carrying the newest failing UPID. This detector catches the live
  cluster's current aptupdate loop.

Also: guest counts exclude `template == 1` rows, and a `"templates": N` count is added.
The human table renders issues with severity coloring. One extra `list_tasks` call per
node — negligible at homelab scale.

### 3.5 Small repairs

- **UPID column:** drop the `[:48]` slice; the column folds like every other.
- **`ct snapshot create --vmstate`:** removed (the LXC snapshot API has no such
  parameter; the flag can only fail server-side). The shared factory takes a
  per-kind switch; `vm` keeps the flag.
- **`pmox version` never fails:** always reports the client version; probes the server
  best-effort with a short (5s) timeout. JSON: `{"client": "0.7.2", "server": {...}}`
  or `{"client": "0.7.2", "server": null, "note": "not connected — see
  https://lukebward.github.io/pmox/configuration/"}`, exit 0 either way. Human output
  is one line. Output-shape change noted in the changelog; `pmox health` remains the
  hard connectivity check.
- **Windows encoding:** at process entry, `sys.stdout.reconfigure(encoding="utf-8",
  errors="replace")` (same for stderr) when supported; all *static help text* becomes
  ASCII-only (`->` not `→`, `...` not `…`), enforced by a test that walks every
  registered command's help strings; runtime human output routes its few glyphs
  (checkmark, arrows, separators) through a `glyph()` helper that falls back to ASCII
  when the stream encoding cannot represent them. JSON is already `\u`-escaped and
  unaffected.
- **Blank help strings:** every command and option currently rendering an empty
  description cell gets one (`vm/ct list, status, config, create, clone, migrate,
  delete`, all four snapshot subcommands, `storage list/content`, `cluster
  status/resources`, `task list/status/log`, snapshot `-d`). `vm create`'s help
  explicitly positions it against `vm new`/`vm up`.
- **Plugin version:** `plugin/.claude-plugin/plugin.json` version is bumped to match
  `pyproject.toml`, with a test asserting the two stay equal.

## 4. Compatibility

Alpha status permits the two shape changes here (`pmox version` output, `.env`
discovery), and both are safety-positive. Everything else is additive (`issues`,
`templates`, two new error codes) or restores documented behavior. All three doc
surfaces (guide.py, SKILL.md, docs site) are updated in the same PR per the sync rule in
`docs/development.md`.

## 5. Testing and verification

- Existing fully-mocked suite with the enforced 100% coverage gate; new unit tests per
  change.
- New subprocess-level envelope tests (3.1) that exercise the real import path.
- New CI matrix leg with real `click` installed.
- ASCII-help and plugin-version-sync tests as standing guards.
- Manual live check before release: the three parse-error probes return usage envelopes;
  `PROXMOX_PORT=abc` returns a config envelope; `pmox health` on the live cluster
  reports the aptupdate failures under `issues`; `task list --node lukeserver` shows
  full UPIDs; `pmox version` answers without credentials.

## 6. Release

Version 0.7.2. Changelog sections: Fixed (boundary, coercion, silent config, confirm
hang, mojibake, `--vmstate`, UPID truncation, template counting), Added (`not_found`/
`auth` codes, health `issues` + detectors, help descriptions), Changed (`.env`
discovery, `pmox version` shape).
