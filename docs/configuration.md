# Configuration

pmox reads settings from three places, lowest to highest priority:

1. a TOML config file
2. environment variables (a `.env` file is loaded; variables already set in
   the real environment win over `.env` entries)
3. CLI flags

!!! warning "One setting ignores this order"

    `PMOX_DANGEROUS` is read from the real environment only — never from a
    `.env` file. See [Safety model](safety.md).

## API token

Create one in Proxmox under *Datacenter → Permissions → API Tokens*. For full
management, give the token the privileges it needs — or, for a homelab, uncheck
"Privilege Separation" so it inherits the user's permissions.

## `.env`

pmox discovers `.env` by walking up from the current directory (like `git`
finds `.git`) — not from wherever the package itself is installed, so an
editable install never leaks a repo's credentials into unrelated directories,
and a plain `pip install` still honors a `.env` in a parent of your cwd.

Copy [`.env.example`](https://github.com/lukebward/pmox/blob/main/.env.example)
to `.env` next to where you run pmox:

```ini title=".env"
PROXMOX_HOST=192.168.1.10
PROXMOX_TOKEN_ID=root@pam!pmox
PROXMOX_TOKEN_SECRET=00000000-0000-0000-0000-000000000000
PROXMOX_VERIFY_SSL=false
```

## TOML

At `~/.config/pmox/config.toml`, or point `--config` / `PMOX_CONFIG` at a path.
The default path is optional — pmox runs fine without it. An explicitly passed
`--config`/`PMOX_CONFIG` path is different: if that file does not exist, pmox
exits 2 with a `config` envelope (`Config file not found: <path>`) instead of
silently ignoring it.

```toml title="~/.config/pmox/config.toml"
host = "192.168.1.10"
port = 8006
token_id = "root@pam!pmox"
token_secret = "..."
verify_ssl = false
timeout = 30

# provisioning defaults (all optional)
ssh_key = "~/.ssh/id_ed25519.pub"
ciuser = "ubuntu"
import_storage = "local"
agent_templates = true   # vm up clones/builds agent golden templates (default on)

# static-IP pool for `vm up` / `vm new` (optional)
[network]
cidr = "192.168.0.0/24"
gateway = "192.168.0.1"
pool = "192.168.0.200-192.168.0.250"   # MUST be outside your DHCP scope
```

## Environment variables

| Variable | Meaning |
|----------|---------|
| `PROXMOX_HOST` | node hostname or IP (it can see the whole cluster) |
| `PROXMOX_PORT` | API port (default 8006) |
| `PROXMOX_TOKEN_ID` | `user@realm!tokenname` |
| `PROXMOX_TOKEN_SECRET` | the token secret |
| `PROXMOX_VERIFY_SSL` | TLS verification (default false — see below) |
| `PROXMOX_TIMEOUT` | API timeout in seconds (default 30) |
| `PROXMOX_NET_CIDR` / `PROXMOX_NET_GATEWAY` / `PROXMOX_NET_POOL` / `PROXMOX_NET_NAMESERVER` | static-IP pool settings |
| `PROXMOX_DEFAULT_IMPORT_STORAGE` | preferred storage for image imports |
| `PROXMOX_DEFAULT_SSH_KEY` | default `--ssh-key` for provisioning |
| `PROXMOX_DEFAULT_CIUSER` | default `--ciuser` for provisioning |
| `PMOX_AGENT_TEMPLATES` | `0` disables the `vm up` agent-template flow (default on) |
| `PMOX_JSON` | `1` always JSON, `0` always tables, `auto` detect (default) |
| `PMOX_DANGEROUS` | `1` enables write mode — honored from the real environment only, never from `.env` |
| `PMOX_CONFIG` | path to the TOML config file |

## Connection flags

`--host`, `--port`, `--token-id`, `--token-secret`,
`--verify-ssl/--no-verify-ssl`, and `--config` override everything else, and
are position-independent like all global flags.

## TLS verification

Verification defaults to off because homelab Proxmox uses self-signed
certificates. Set `PROXMOX_VERIFY_SSL=true` (or `--verify-ssl`) if your node
has a CA-signed cert.
