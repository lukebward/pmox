# ARP-based IP discovery for agent-less DHCP guests

- **Date:** 2026-06-10
- **Status:** approved (design reviewed in session)
- **Target release:** 0.6.0

## Problem

A VM created from a stock cloud image (`vm up --image ubuntu-24.04`) boots with
`agent: 1` set but no `qemu-guest-agent` installed inside the guest. With DHCP
addressing, `pmox vm ip` therefore has nothing to report: the agent source
fails and there is no static `ipconfigN` to fall back to. The user must hunt
the address down manually (router leases, or — as performed by hand in this
session — a ping sweep plus ARP-table lookup against the VM's MAC). pmox
already knows the MAC from the guest config, so this discovery is mechanical
and should be automated.

## Goals

- `pmox vm ip <vmid>` resolves the address of an agent-less DHCP VM when the
  machine running pmox shares an L2 network with the VM's bridge.
- `pmox vm ip <vmid> --wait` becomes the universal post-create follow-up: it
  returns as soon as the fresh VM starts talking on the LAN.
- Zero new cluster privileges, zero guest modification, token-only preserved.
- Honest provenance: results carry `"source": "arp"` so consumers know the
  confidence level.

## Non-goals

- Installing the guest agent (vendor-data `--install-agent` is a separate,
  complementary future feature).
- Discovery across L3 boundaries (VPN/WireGuard, other VLANs). Off-LAN, the
  command degrades to today's actionable error.
- IPv6 discovery (NDP). ARP is IPv4-only; the result has no prefix length.
- Containers. Running CTs already report interfaces agentlessly; stopped
  guests have nothing to discover.

## Design

### Source chain

`views.guest_ip_addresses` gains a third, last-resort source, tried in order:

1. `guest-agent` (existing)
2. `config` — static cloud-init `ipconfigN` (existing)
3. `arp` — same-LAN neighbor discovery (new; QEMU guests only)

The fallback is automatic — no new flag. It fires only when sources 1 and 2
both produce nothing, and only when scanning is enabled for the call site
(below).

### Call-site gating

`guest_ip_addresses` gains a keyword-only `scan: bool = False` parameter.

- `vm ip` / `ct ip` command path passes `scan=True` (the `kind == "qemu"`
  check inside the implementation makes it a no-op for CTs).
- `vm describe`'s embedded network section keeps `scan=False`: a describe of
  an agent-less VM must not pay a 1–2 s sweep penalty.
- `_wait_for_ip` (the `--wait` loop) needs no change; it calls
  `guest_ip_addresses` via the `vm ip` path and inherits the fallback. Each
  poll iteration may sweep (~1 s); with `_POLL_SECONDS = 2` an iteration costs
  roughly 3 s, bounded as today by `--timeout`.

### Discovery mechanics (new module `pmox/arp.py`)

One job: find the IPv4 address(es) for a set of MACs on the local L2 network.

1. **MAC extraction** (from the guest config pmox already fetched):
   `netN` values are comma-separated `key=value` lists. Any value matching the
   MAC regex `[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}` is collected — this
   covers every QEMU model key (`virtio=`, `e1000=`, …) and LXC's `hwaddr=`
   without enumerating model names. No parseable MAC → skip the scan and
   raise the existing error.
2. **Neighbor-table check first** (free): read the OS neighbor table and match
   normalized MACs (lowercased, separators stripped). A VM that has talked
   recently resolves with no sweep.
3. **Nudge sweep on miss**: send one empty UDP datagram (port 9, discard) to
   every host in the candidate subnet. UDP sends are unprivileged and force
   the OS to ARP-resolve each target even when the guest firewalls ICMP.
   Sleep ~1 s, re-read the neighbor table, match again.
4. **Candidate subnet selection**:
   - `[network] cidr` from settings, when configured (it declares the VM
     subnet explicitly);
   - else a /24 around the machine's primary outbound IPv4, derived via the
     UDP-connect trick *toward the configured Proxmox host* (route lookup
     only, no packet sent) so a VPN default route cannot mislead it;
   - subnets larger than /22 (1024 addresses) are never swept — the free
     neighbor-table check still runs, the sweep is skipped.
5. **Neighbor-table read**: `ip neigh` on Linux (fallback `arp -a`),
   `arp -a` on Windows and macOS. A single line parser handles all three
   formats: per line, extract the first IPv4 literal and the first MAC-shaped
   token, normalize, map MAC → IP. Lines without both are ignored (covers
   `FAILED`/incomplete entries; all-zero or broadcast MACs simply never match
   a real guest MAC).

Injectable edges for testing: the neighbor-table reader (subprocess) and the
nudge sender (socket) are module-level functions that tests monkeypatch; the
parser is exercised against captured fixture output from all three platforms.
No real packets or subprocesses in CI.

### Result shape

A successful ARP resolution returns the existing schema with:

- `source: "arp"`
- one interface entry per matched MAC:
  `{"name": "<netN key>", "mac": "<as configured>", "addresses":
  [{"family": "ipv4", "address": "<ip>", "prefix": null, "scope": "global"}]}`
- `primary` = the first matched address (config `netN` order).

### Error path

When the agent fails, no static config exists, and the scan also misses, the
existing actionable `RuntimeError` is raised, extended with one sentence
noting that a same-LAN ARP scan found no match for the MAC(s). When the scan
never ran (CT, `scan=False`, no parseable MACs, oversized subnet) the message
stays exactly as today — it only mentions what was actually tried. Off-LAN
users therefore see today's behavior plus an honest note, not a new failure
mode.

### User-visible text

- `vm up` DHCP hints reword to: run `pmox vm ip <vmid> --wait` — the address
  is found via the guest agent or a same-LAN ARP scan.
- `vm ip` command help mentions the third source and its same-L2 constraint.
- Guide, SKILL.md, README: document the source chain
  (`guest-agent → config → arp`), the same-L2 limitation (works from the
  LAN, not over WireGuard), possible staleness after lease changes, and
  IPv4-only results.

## Testing

- `tests/test_arp.py`: parser fixtures (Windows `arp -a`, macOS `arp -a`,
  Linux `ip neigh`), MAC extraction from qemu/lxc config values, MAC
  normalization, subnet selection (settings cidr / fallback /24 / >/22 skip),
  neighbor-first-then-sweep ordering, no-match returns None.
- `tests/test_views.py`: fallback ordering (agent → config → arp), `scan`
  gating, `source: "arp"` shape, error message includes the scan note.
- `tests/test_cli.py`: `vm ip` resolves via arp end-to-end with a fake
  client + monkeypatched arp module; `vm ip --wait` returns once a sweep
  iteration matches; `vm describe` never triggers a scan; `ct ip` unaffected.
- 100% coverage gate, as for the rest of the project.

## Release

0.6.0 with CHANGELOG entry (Added: ARP discovery source; Changed: vm up DHCP
hint text).
