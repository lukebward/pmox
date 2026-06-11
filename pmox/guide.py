"""The built-in agent guide: `pmox guide` prints GUIDE verbatim.

This exists so ANY agent (not just one with the Claude Code plugin installed)
can self-onboard in a single call instead of crawling --help screens. Keep it
in sync with README.md and plugin/skills/proxmox/SKILL.md when behavior changes.
"""

GUIDE = """\
pmox — guide for agents and automation
======================================

pmox explores and manages a Proxmox VE cluster. It is READ-ONLY BY DEFAULT:
nothing can change cluster state unless --dangerous is passed.

Output
------
- Output auto-detects: JSON when captured/piped (your case), tables on a TTY.
  Force with --json / --no-json or PMOX_JSON=1|0|auto.
- List commands accept --fields a,b,c to project rows onto just those keys
  (missing keys come back as null). Cuts token cost on big clusters.

Safety model (two independent gates)
------------------------------------
1. --dangerous       global flag; required for ANY state change (power, create,
                     set, clone, migrate, snapshot, pull). PMOX_DANGEROUS=1 in
                     the real environment also enables it; a .env file cannot.
                     --no-dangerous forces read-only regardless of environment.
2. --yes / -y        per-SUBCOMMAND flag (goes AFTER the subcommand) required
                     for destructive ops: delete, stop, reset, migrate,
                     snapshot rollback/delete, and `set` with a delete= key.
                     Example: pmox --dangerous vm delete 100 --yes

Never try to work around the gates. If you get exit 3 or 4, surface the `need`
field to the user and ask before retrying with the flag.

Exit codes
----------
0 success · 1 error (see envelope `error` field) · 2 config OR usage error
(envelope disambiguates: "config" vs "usage") · 3 needs --yes · 4 needs --dangerous

Error envelope (stdout, JSON mode)
----------------------------------
{"ok": false, "error": <code>, "message": "...", ...}
  error codes: read_only | confirm_required | config | usage | network | error
  - read_only / confirm_required carry `need`: ["--dangerous"] / ["--yes"].
  - network = connectivity/TLS/DNS trouble (usually worth a retry or a config check).
  - Task failures carry `upid` + `node` + `hint`.
  - Provisioning failures carry `vmid`, `completed_steps`, `failed_step`, `hint`
    explaining how to recover (see "Long tasks & recovery" below).

Success envelope (mutations)
----------------------------
{"ok": true, "message": "...", "op": "qemu.start", "vmid": 100, "node": "pve1",
 "upid": "UPID:..." | "task": {...final task status...}, "hint": "..."}
Read vmid/node/upid from the fields — never parse the message prose.
`vm up` additionally returns name, ip (null when DHCP), ssh, template (the
template VMID it cloned, or null) and agent (true when the clone came from an
agent template, so `vm ip --wait` is agent-backed).

Discovery (always safe, no flags)
---------------------------------
pmox health                          one-shot cluster triage
pmox nodes list / cluster status / cluster resources [--type vm|node|storage]
pmox vm list / ct list [--node N] [--fields ...]
pmox vm describe <vmid>              status+config+snapshots+tasks+network in one call
pmox vm ip <vmid> [--wait]           live address (agent, static config, or same-LAN ARP scan)
pmox storage list / storage content <id>
pmox task list / task status <upid> / task log <upid>
pmox image list [--ct]
--node is optional almost everywhere: it is resolved from the VMID, parsed from
the UPID, or auto-picked on single-node clusters. Multi-node clusters list the
candidates in the error when a choice is needed.

Provisioning (needs --dangerous)
--------------------------------
pmox --dangerous vm up <name> --image ubuntu-24.04
    One call: routes the image to an import-capable storage, picks a disk
    storage (local-lvm preferred), ensures an SSH key (~/.ssh/id_ed25519.pub,
    generated if missing), creates + starts the VM. DHCP by default; --ip
    <cidr>,gw=<ip> or a configured [network] pool gives a static address.
    AGENT TEMPLATES (the default flow): the first `vm up --image X` on a node
    also builds an agent golden template for X — boots a build VM, SSHes in
    with the key pmox manages, installs qemu-guest-agent, cleans it for
    cloning, converts it to a tagged template — then clones it for your VM.
    That first run takes a few minutes; every later `vm up --image X` clones
    in seconds, and `vm ip --wait` is answered by the guest agent (no ARP
    scans, works over VPN). The envelope's `agent`/`template`/`template_built`
    fields say which path ran. Opt out with --no-agent-template or
    PMOX_AGENT_TEMPLATES=0. Images without a known login user (raw URLs or
    volids without --ciuser) fall back to a plain image VM with a hint.
pmox --dangerous template build ubuntu-24.04
    Pre-build the agent template explicitly (same thing vm up does on first
    use, but with control over --ip/--user/--vmid/--name). Build VM address:
    --ip <cidr>,gw=<ip>, else the [network] pool, else DHCP + discovery.
    Idempotent: reuses an existing template (`reused`: true). Use this when
    the LAN makes DHCP discovery unreliable — a static --ip sidesteps it.
pmox template list
    VM templates cluster-wide; `agent: true` marks pmox-built agent templates.
pmox --dangerous vm up <name> --from-template <vmid>
    Clone a specific template VMID instead (inherits its hardware; the clone of
    an agent-baked template makes `vm ip --wait` work on DHCP).
pmox --dangerous ct new <name> --template ubuntu-24.04
    Same idea for LXC containers.
pmox --dangerous image pull <name|url> --storage S --node N [--checksum sha256:<hex>]
    Pre-download (cached; verified when --checksum given). --as-template builds
    a golden template and returns its vmid.
Validation is fail-fast: bad names (DNS rules — no underscores), bad --ip syntax,
and wrong --storage content types are rejected before anything is downloaded.

Waiting, timeouts, recovery
---------------------------
- Provisioning commands (vm up / vm new --image / ct new / image pull) always
  wait on their internal steps; --timeout <s> (default 600) bounds each wait.
  --wait additionally applies to one-shot ops like start/stop/delete.
- Your shell may time out before pmox finishes. If that happens, or --timeout
  expires, the Proxmox task keeps running server-side:
    pmox task wait <upid>            resume waiting (node parsed from the UPID)
    pmox task status/log <upid>      inspect
- After a DHCP create: pmox vm ip <vmid> --wait polls until the guest reports
  an address — via the guest agent, or a same-LAN ARP scan by the VM's MAC
  (source: "arp"; needs pmox to run on the same network as the guest, so it
  works from the LAN but not over a VPN; IPv4 only). Agent-backed guests
  (clones of a `template build` template) answer via the agent in seconds and
  need no scan — prefer that path; suggest `template build` when scans fail.
- If a provisioning plan fails partway, the error envelope tells you exactly
  what completed and what to do: a guest that was already created is NOT
  cleaned up, and a blind retry would create a second one under a new VMID —
  follow the envelope's hint (describe, then finish manually or delete first).
  Failures before the create step are always safe to retry; image downloads
  are cached and skipped on the next run.

Flag placement
--------------
Global flags (--json, --dangerous, --no-dangerous, --wait, --timeout, --dry-run,
--host, --config, ...) work BEFORE or AFTER the subcommand. --yes and other
per-subcommand options (-o, --node, --target, --fields) must come AFTER it.
--dry-run prints the intended API call/plan as JSON and changes nothing (it
still performs read calls to resolve nodes and VMIDs).
"""
