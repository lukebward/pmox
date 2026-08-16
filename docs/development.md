# Development

The test suite mocks the Proxmox API — no live cluster required.

```bash
git clone https://github.com/lukebward/pmox.git && cd pmox
python -m venv .venv
```

=== "Linux / macOS"

    ```bash
    . .venv/bin/activate
    pip install -e ".[dev]"
    pytest
    ```

=== "Windows (PowerShell)"

    ```powershell
    .venv\Scripts\Activate.ps1
    pip install -e ".[dev]"
    pytest
    ```

!!! info "Coverage is enforced at 100%"

    `pytest` runs with `--cov-fail-under=100`. New code needs new tests or the
    suite fails.

## Architecture

Each module owns one concern, and `client.py` is the only thing that talks to
Proxmox:

| Module | Responsibility |
|--------|----------------|
| `config.py` | Settings + precedence merge (file < env < flags) |
| `client.py` | Thin, injectable wrapper over proxmoxer — the only API surface |
| `catalog.py` | Cloud-image catalog and sizing profiles |
| `arp.py` | Same-LAN ARP discovery for agent-less DHCP guests |
| `views.py` | Composite read queries (describe, health, guest IP lookup) |
| `provision.py` | VM / container creation workflows + fail-fast validation |
| `ipam.py` | Token-only static IPv4 allocation (cluster config as ledger) |
| `output.py` | Rich tables + plain JSON; byte/uptime/percent formatters |
| `safety.py` | The two gates: `require_dangerous()` and `confirm()` |
| `errors.py` | Typed errors carrying structured envelope fields |
| `guide.py` | The `pmox guide` text (agent onboarding) |
| `cli.py` | Typer app wiring it all together |

The injectable client is what makes the suite cluster-free: tests hand `views`
and `provision` a stub instead of a real connection.

## Building the docs

This site is [MkDocs](https://www.mkdocs.org/) with
[Material](https://squidfunk.github.io/mkdocs-material/).

```bash
pip install -e ".[docs]"
mkdocs serve
```

That serves at `http://127.0.0.1:8000` with live reload. To check for broken
links and nav problems the way CI does:

```bash
mkdocs build --strict
```

!!! warning "`.superpowers/` is not part of the site"

    Design specs and implementation plans live in `.superpowers/` at the repo
    root, deliberately outside `docs/`. Anything you put in `docs/` is
    published.

Docs deploy to GitHub Pages automatically when `main` changes — see
`.github/workflows/docs.yml`.

## Keeping the guide in sync

`pmox guide` (in `guide.py`), this site, and
`plugin/skills/proxmox/SKILL.md` describe the same behavior to three different
audiences. When the safety model or the envelopes change, all three need the
edit.
