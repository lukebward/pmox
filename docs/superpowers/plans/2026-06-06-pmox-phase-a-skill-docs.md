# pmox Phase A: Skill + Docs Rewrite — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Document the full agent-native surface built in B1/B2/C so any AI driving `pmox` is instantly fluent — rewrite the `proxmox` skill, refresh both READMEs, the CHANGELOG, and the `/pmox:*` commands. This is the autopsy's #1 fix (the smarts move into the context).

**Architecture:** Docs only — no Python changes, so coverage is unaffected, but the **full test suite must stay green** and every documented command/flag must match the real CLI (`pmox <cmd> --help`). Accuracy is the acceptance criterion.

**Source of truth:** spec §7; the real CLI surface (captured below); the research caveats.

## The real CLI surface (verified — document exactly this)

Top-level: `version`, `health`, `nodes`, `vm`, `ct`, `storage`, `cluster`, `task`, `image`.
Global flags (position-independent — work before OR after the subcommand): `--json/--no-json`, `--dangerous`, `--wait/--no-wait`, `--timeout <s>` (default 600), `--dry-run`, `--host`, `--port`, `--token-id`, `--token-secret`, `--verify-ssl/--no-verify-ssl`, `--config`, `--version`.
`vm`/`ct` subcommands: `list`, `status`, `config`, `describe`, `set`, `resize`, `rename`, `tag`, `start`, `shutdown`, `reboot`, `suspend`, `resume`, `stop`, `reset`, `create`, `new`, `clone`, `migrate`, `delete`, `snapshot {list,create,delete,rollback}`.
 - `vm new`: blank shell | `--image <name|url|volid>` (cloud-init) | `--from-template <vmid>` (clone). Shared cloud-init opts: `--ssh-key <path>` (repeatable), `--ip dhcp|<cidr>,gw=<ip>`, `--ciuser`, `--cipassword`, `--nameserver`. Plus `--size`, `--disk`, `--storage`, `--node`, `--vmid`, `-o`.
 - `ct new`: `--template <name|volid>` (required), `--size`, `--disk`, `--storage`, `--template-storage`, `--node`, `--vmid`, `--ssh-key`, `--ip`, `--password`.
`image`: `list [--ct --node N]`, `pull <name|url> --storage S --node N [--ct] [--as-template] [--vmid] [--name]`.

Safety: read-only by default; `--dangerous` for any change; `--yes` additionally for destructive (`delete`, `stop`, `reset`, `migrate`, snapshot `rollback`/`delete`, and `set` with a `delete=` key). Exit codes: 0 ok · 1 error · 2 config · 3 needs `--yes` · 4 needs `--dangerous`. Under `--json`, errors are a JSON envelope `{"ok":false,"error":...,"need":[...],"message":...}`.

Sizing profiles: small = 1 core / 1024 MiB; medium = 2 / 4096; large = 4 / 8192.

**Caveats to document (from research + phase reviews):**
- `vm new --image` / `image pull` need **PVE 8.2+ (8.4+ recommended)** and a storage with the **`import` content type enabled**; pmox uses an API token, so it imports by volume-ID (absolute paths would need `root@pam`).
- Catalog images download over HTTPS **without checksum verification** (no warning in v1) — for integrity, pass `--image <url>` from a trusted source or a checksummed image.
- `ct new`: `--template-storage` (default `local`, holds the `vztmpl`) differs from `--storage` (default `local-lvm`, the rootfs). Discover templates with `pmox image list --ct --node N`. `ssh-public-keys` is sent raw (no manual encoding).
- `--dry-run` prints the full plan but still makes **read** API calls (needs cluster connectivity); it performs **zero** mutations.
- Clones (`vm new --from-template`) inherit the template's disk size — pass `--disk` or resize after.
- `image pull --as-template` / converting to a template is **one-way**.

---

## Conventions for every task

- After writing each doc, **verify accuracy**: for any command/flag you document, confirm it exists via `.venv\Scripts\python.exe -m pmox <path> --help`. Do not invent flags.
- Before each commit, run the full suite `.venv\Scripts\python.exe -m pytest` (must stay green at 100% — docs don't change coverage, but confirm nothing broke).
- Work in place in `C:\Users\Luke\Workspace\pmox` on `feat/agent-native-commands`. No worktree.

---

## Task 1: Rewrite the `proxmox` skill (the agent-facing doc)

**Files:** Modify `plugin/skills/proxmox/SKILL.md` (full rewrite).

- [ ] **Step 1: Capture the real surface**

Run and read: `.venv\Scripts\python.exe -m pmox --help`, `... vm --help`, `... vm new --help`, `... ct new --help`, `... image --help`, `... image pull --help`. Confirm the command/flag names before writing.

- [ ] **Step 2: Rewrite `SKILL.md`** with these sections (keep the YAML frontmatter `name: proxmox`, update `description` to mention provisioning; keep `allowed-tools: Bash(pmox:*), Bash(python:*)`):

  1. **Intro** — pmox explores AND provisions a Proxmox cluster; read-only by default.
  2. **Invoking** — `pmox <args>` (fallback `python -m pmox`); output auto-JSON when captured (no `--json` needed); parse it. Config error (exit 2) → tell the user to set `PROXMOX_HOST`/`PROXMOX_TOKEN_ID`/`PROXMOX_TOKEN_SECRET`.
  3. **Safety model** — the two gates (`--dangerous`, `--yes`), the destructive set (incl. `set` with `delete=`), exit codes 0/1/2/3/4, and the JSON error envelope (`error`/`need`) to branch on. Flags are **position-independent** (work before or after the subcommand).
  4. **Discovery** — `pmox health`; `pmox vm describe <id>` / `ct describe`; `pmox image list` (VM) and `pmox image list --ct --node N` (containers); plus `cluster resources`, `vm list`, `ct list`.
  5. **Recipes** (lead with these):
     - **One-call cloud-init VM:** `pmox --dangerous vm new web --image ubuntu-24.04 --size small --disk 50 --ssh-key ~/.ssh/id_ed25519.pub --ip dhcp --wait`
     - **Ready container:** `pmox --dangerous ct new box --template ubuntu-24.04 --ssh-key ~/.ssh/id_ed25519.pub --ip dhcp --wait`
     - **Edit a guest:** `pmox --dangerous vm set 100 -o cores=4 -o memory=4096`; `vm resize 100 --disk scsi0 --size +10G`; `vm rename 100 web01`; `vm tag 100 --add prod,k3s`
     - **Blank VM:** `pmox --dangerous vm new web --size small --disk 50 --node pve1`
     - **Golden template + clone:** `pmox --dangerous image pull ubuntu-24.04 --storage local --node pve1 --as-template` then `pmox --dangerous vm new web --from-template <id> --ssh-key ~/.ssh/id_ed25519.pub --ip dhcp --wait`
     - **Preview before acting:** add `--dry-run` to any change to print the exact plan (zero mutations); confirm completion with `--wait` or `pmox vm describe <id>`.
  6. **Sizing profiles** table (small/medium/large).
  7. **Command cheat-sheet** — the full surface grouped: *Inspect (safe)*, *Change (need `--dangerous`)*, *Destroy (need `--dangerous --yes`)*. Use the real command names from the captured surface.
  8. **Requirements & caveats** — the bulleted caveats list above (PVE 8.2+/import content type; no checksum verification; ct vztmpl vs rootfs storage + `image list --ct`; dry-run still reads; clones inherit disk size; template conversion one-way).
  9. **Rules for the AI** — default read-only; add `--dangerous` only when the user asks to change something; add `--yes` only for destructive actions and confirm the target first; never try to bypass the gates.

- [ ] **Step 3: Verify accuracy** — for every command in the cheat-sheet and recipes, confirm it (and its flags) exist via `--help`. Fix any mismatch.

- [ ] **Step 4: Run the suite + commit**

Run: `.venv\Scripts\python.exe -m pytest` (green).

```bash
git add plugin/skills/proxmox/SKILL.md
git commit -m "Rewrite proxmox skill for the agent-native surface (edit, provisioning, recipes, caveats)"
```

---

## Task 2: Refresh the READMEs

**Files:** Modify `README.md` (top-level) and `plugin/README.md`.

- [ ] **Step 1: Update `README.md`**
  - In the intro/quickstart, add the headline cloud-init one-liner and the edit commands.
  - Replace the **Commands** block with the current surface (add `health`; the `vm/ct` `describe`/`set`/`resize`/`rename`/`tag`/`new` modes; the `image list`/`pull` group). Keep it accurate to `--help`.
  - Add a short **Provisioning** subsection: `vm new --image`, `ct new --template`, `image pull --as-template` + the PVE 8.2+/`import` content-type requirement and the no-checksum caveat.
  - Update the **Architecture** module list to include `catalog.py`, `views.py`, `provision.py`.
  - Keep the safety-model table; add the `set delete=` → `--yes` note and that errors are JSON under `--json`.

- [ ] **Step 2: Update `plugin/README.md`**
  - Under "What you get", note the skill now covers edit + provisioning (one-call cloud-init servers, containers, templates), `health`, and `describe`.

- [ ] **Step 3: Verify accuracy** against `--help`; run the suite (green).

- [ ] **Step 4: Commit**

```bash
git add README.md plugin/README.md
git commit -m "Refresh READMEs for edit/provisioning surface and new modules"
```

---

## Task 3: CHANGELOG + `/pmox:*` commands

**Files:** Modify `CHANGELOG.md`; lightly update `plugin/commands/cluster-status.md` and `plugin/commands/list-guests.md` (run.md is generic — leave unless inaccurate).

- [ ] **Step 1: Update `CHANGELOG.md`** — under `## [Unreleased]`, add an `### Added` list covering: position-independent global flags; `--wait`/`--timeout`/`--dry-run`; machine-readable JSON errors; `vm/ct set` (with delete-guard), `resize`, `rename`, `tag`; `vm new` (profiles, auto-VMID, node auto-pick) with `--image` cloud-init and `--from-template` clone modes; `ct new`; `vm/ct describe`; `health`; the `image` group (`list`/`pull`, `--ct`, `--as-template`); new modules `catalog`/`views`/`provision`. Follow the existing Keep-a-Changelog style.

- [ ] **Step 2: Light-touch the command files** — in `plugin/commands/cluster-status.md`, mention `pmox health` as the one-shot triage. In `plugin/commands/list-guests.md`, optionally mention `pmox vm describe <id>` for detail. Keep them read-only and accurate. Do not change `run.md` unless something is wrong.

- [ ] **Step 3: Verify + commit**

Run the suite (green).

```bash
git add CHANGELOG.md plugin/commands/cluster-status.md plugin/commands/list-guests.md
git commit -m "Update CHANGELOG and /pmox commands for the agent-native surface"
```

---

## Self-review (completed during planning)

- **Spec coverage (§7):** SKILL.md rewrite (T1) — full surface, recipes, profiles, caveats, JSON errors, position-independent flags ✓; READMEs (T2) — commands, provisioning, modules, safety ✓; CHANGELOG + `/pmox:*` (T3) ✓.
- **Accuracy gate:** every task verifies documented commands/flags against `pmox --help`; the captured surface above is the reference. No invented flags.
- **Caveats:** PVE 8.2+/import content type, no checksum verification, ct vztmpl-vs-rootfs storage, dry-run still reads, clones inherit disk size, template conversion one-way — all land in the skill and (briefly) the README.
- **No code changes:** coverage unaffected; the full suite must stay green (confirm per task).
```
