# pmox: expose a guest's live IP address(es)

**Status:** approved design — ready for implementation planning
**Date:** 2026-06-07
**Scope:** a read-only `ip` command for both guest kinds (`vm ip` / `ct ip`) plus an IP section in `describe`. Nothing state-changing; no new dependencies.

## 1. Motivation

`pmox vm new --image … --ip dhcp` creates a cloud-init VM whose address is assigned by DHCP at boot — but there is currently **no way to read that address back**. `vm config` shows `ipconfig0=ip=dhcp` (the *request*, not the *lease*), and neither `status` nor `describe` reports the live interfaces. The user has to open the Proxmox UI or SSH in by guessing. This feature closes that loop: ask the running guest what IP it actually has.

## 2. Principles & constraints

Inherited from the codebase and **must be preserved**:

- **Clean module boundaries.** `client.py` stays a thin *one-API-endpoint-per-method* wrapper. Multi-endpoint *read* composition lives in `views.py`. Rendering lives in `cli.py`/`output.py`. No new logic bloats `cli.py` beyond wiring + columns.
- **100% test coverage is enforced** (`pyproject.toml`: `--cov-fail-under=100`). Every new line ships with a fully-mocked test — no live cluster.
- **Read-only command.** `ip` changes nothing, so it needs neither `--dangerous` nor `--yes`. It works in the default safe mode.
- **Auto-JSON contract preserved.** JSON when piped/`--json`, Rich table at a TTY. JSON always carries the *full* structured data; terminal filtering is cosmetic only.
- **Exit codes unchanged:** `0` ok · `1` error · `2` config · `3` confirm-required · `4` read-only.

## 3. Data sources (per guest kind)

| Kind | Endpoint (proxmoxer) | Notes |
|---|---|---|
| `qemu` | `getattr(nodes(node).qemu(vmid).agent, "network-get-interfaces").get()` | Needs `qemu-guest-agent` running in the guest **and** `agent: 1` in config. `vm new --image/--from-template` already sets `agent: 1`. Returns `{"result": [ …ifaces… ]}`. |
| `lxc` | `nodes(node).lxc(vmid).interfaces.get()` | No agent required; the host reports container interfaces directly. Returns a list of `{name, hwaddr, inet, inet6}`. |

The hyphenated QEMU path segment `network-get-interfaces` is addressed via `getattr` (same pattern as the existing `download-url` method).

### 3.1 New `client.py` methods (thin, one endpoint each)

| Method | Endpoint | Used by |
|---|---|---|
| `agent_network_interfaces(node, vmid)` | `getattr(qemu(vmid).agent, "network-get-interfaces").get()` | `guest_ip_addresses` (qemu) |
| `lxc_interfaces(node, vmid)` | `lxc(vmid).interfaces.get()` | `guest_ip_addresses` (lxc) |

## 4. Normalization (`views.py`)

New `guest_ip_addresses(client, kind, vmid, node=None) -> dict`. It resolves the node/name (one `cluster_resources(type="vm")` scan via a local `_locate_guest` helper), calls the kind-appropriate client method, and normalizes both wire shapes into one structure:

```json
{
  "vmid": 150, "node": "lukeserver", "kind": "qemu", "name": "web-01",
  "source": "guest-agent",
  "primary": "192.168.1.50",
  "interfaces": [
    {"name": "eth0", "mac": "bc:24:11:aa:bb:cc", "addresses": [
      {"family": "ipv4", "address": "192.168.1.50", "prefix": 24, "scope": "global"},
      {"family": "ipv6", "address": "2001:db8::5",  "prefix": 64, "scope": "global"},
      {"family": "ipv6", "address": "fe80::be24:11ff:feaa:bbcc", "prefix": 64, "scope": "link"}
    ]}
  ]
}
```

- `source` is `"guest-agent"` (qemu) or `"lxc-interfaces"` (lxc).
- `scope` ∈ `global | link | loopback`, from a pure `_addr_scope(family, address)` helper: `127./::1` → `loopback`; `fe80::` / `169.254.` → `link`; else `global`.
- `primary` = the first **global IPv4** on a non-loopback interface, in interface order (or `null`).
- **Parsers:** the QEMU branch reads `ip-addresses[].{ip-address-type, ip-address, prefix}` and `hardware-address`; tolerates a bare list or a `{"result": […]}` envelope. The LXC branch parses `inet`/`inet6` CIDR strings (`addr/prefix`), splitting on whitespace defensively, and reads `hwaddr`.
- **JSON includes every address** (loopback + link-local too) — full structured data. Filtering happens only at render time.

### 4.1 `describe` integration

`describe_guest` gains a `network` key carrying a uniform `available` flag. The `guest_ip_addresses` call is wrapped in `try/except` so a downed agent never breaks `describe`:

- success → `"network": {"available": true, …normalized…}`
- failure → `"network": {"available": false, "reason": "<message>"}`

(The standalone `guest_ip_addresses` return value — used by the `ip` command — does **not** carry `available`; it returns the normalized dict on success and raises on failure per §6. The `available` flag exists only on `describe`'s embedded copy, which is the sole place that degrades instead of raising.)

## 5. CLI surface (`cli.py`)

Added inside the shared `build_guest_app(kind, label)` factory, so **one definition yields both `vm ip` and `ct ip`**:

```
pmox vm ip <vmid> [--node/-n NODE] [--all/-a]
pmox ct ip <vmid> [--node/-n NODE] [--all/-a]
```

- **Filtered (default):** a header line — `VM 150 (web-01) on lukeserver · primary 192.168.1.50` — then a table of non-loopback interfaces showing global IPv4/IPv6 (loopback + `fe80::` link-local hidden, interfaces with no global address dropped).
- **`--all` / `-a`:** every interface and address (incl. loopback + link-local) with prefixes, plus the MAC column.
- **JSON / piped:** emits the full normalized dict regardless of `--all`.

Render helpers (`_ip_rows_filtered`, `_ip_rows_all`) flatten `interfaces` into row dicts for the existing `Column`/`emit` machinery (two new column sets: `IP_COLUMNS`, `IP_ALL_COLUMNS`). `describe`'s terminal branch reuses the filtered renderer (or prints a dim `network: unavailable (<reason>)` line when `available` is false).

## 6. Error handling

`guest_ip_addresses` raises (caught by the CLI `error_boundary` → error envelope, exit 1) only when it cannot retrieve interfaces. The endpoint call is wrapped so any failure becomes a kind-specific, actionable `RuntimeError`:

- **VM — agent absent/disabled/not responding:** *"Could not read network interfaces for VM 150: \<underlying\>. Ensure qemu-guest-agent is installed and running in the guest and `agent: 1` is set (`pmox vm set 150 -o agent=1 --dangerous`)."*
- **CT — not running:** *"Could not read network interfaces for CT 200: \<underlying\>. The container may be stopped."*
- **Guest not found** (bad vmid, can't resolve node): `LookupError` → error envelope.
- **Running but no lease yet / IPv6-only:** **not** an error — interfaces are returned with `primary: null`.

`describe` swallows these into `{"available": false, "reason": …}` (see §4.1) and never fails on account of the IP lookup.

## 7. Tests

- `test_client.py` — `agent_network_interfaces` (asserts the `getattr` agent chain) and `lxc_interfaces` (asserts the `lxc(vmid).interfaces.get` chain).
- `test_views.py` — QEMU agent shape → normalized; LXC shape → normalized; `_addr_scope` classification; `primary` selection (first global IPv4; `null` when none); agent-down / CT-stopped → raises actionable `RuntimeError`; `describe_guest` embeds `network` on success and degrades to `available: false` on failure.
- `test_cli.py` — `vm ip` filtered table, `vm ip --all`, `vm ip --json` (full dict), `ct ip`, agent-down → error envelope + exit 1, `describe` includes the `network` section. Uses the existing fake-client fixture.

## 8. Documentation

- `README.md` — add `vm ip` / `ct ip` to the command listing and a short "find a guest's IP" note (mention the guest-agent requirement for VMs).
- `plugin/skills/proxmox/SKILL.md` — document `vm ip` / `ct ip` alongside `status`/`config`/`describe` (≈ lines 110, 246) and the agent caveat.
- `CHANGELOG.md` — new entry under Unreleased.

## 9. Out of scope (YAGNI)

- Polling/`--wait` for a DHCP lease to appear (single-shot read; clear message when nothing yet).
- Public-vs-private (RFC1918) classification — only loopback/link-local/global.
- Setting or changing IPs (that's already `vm set -o ipconfig0=…`).
- A pre-flight `agent: 0` config check (the actionable error message already covers the disabled case).
