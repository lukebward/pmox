# Guest IP Exposure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `pmox vm ip <vmid>` / `pmox ct ip <vmid>` to read a guest's live IP address(es) (e.g. the DHCP lease), and surface the same data inside `describe`.

**Architecture:** Two thin client methods hit the QEMU guest-agent endpoint (VMs) and the LXC interfaces endpoint (CTs). A `views.guest_ip_addresses` composer normalizes both wire shapes into one structure; the CLI renders a filtered table by default (`--all` for everything) and always emits the full dict as JSON. `describe` embeds the same data behind a graceful-degradation wrapper.

**Tech Stack:** Python, Typer, proxmoxer (mocked in tests), pytest with 100% statement coverage enforced (`--cov-fail-under=100`).

**Conventions (from the existing suite):**
- Client tests use `client`/`api` fixtures (a `MagicMock` proxmoxer API). Assert both return value and endpoint chain.
- View tests build a local `MagicMock` client and call `views.*` directly.
- CLI tests use `inv(args, creds)` (Typer `CliRunner`) with the `fake_client` fixture; `plain(text)` strips ANSI before substring asserts; JSON asserts via `json.loads(r.output)`.
- **Run individual tests with `--no-cov`** (the `--cov-fail-under=100` gate fails any partial run). Run the **full** suite with coverage only at the end.

---

### Task 1: Client methods for the two IP endpoints

**Files:**
- Modify: `pmox/client.py` (add two methods after `guest_config`, ~line 77)
- Test: `tests/test_client.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_client.py`:

```python
def test_agent_network_interfaces(client, api):
    guest = api.nodes.return_value.qemu.return_value
    getattr(guest.agent, "network-get-interfaces").get.return_value = {"result": [{"name": "eth0"}]}
    out = client.agent_network_interfaces("pve1", 100)
    assert out == {"result": [{"name": "eth0"}]}
    api.nodes.assert_called_with("pve1")
    api.nodes.return_value.qemu.assert_called_with(100)
    getattr(guest.agent, "network-get-interfaces").get.assert_called_once_with()


def test_lxc_interfaces(client, api):
    guest = api.nodes.return_value.lxc.return_value
    guest.interfaces.get.return_value = [{"name": "eth0", "inet": "10.0.0.5/24"}]
    out = client.lxc_interfaces("pve2", 200)
    assert out == [{"name": "eth0", "inet": "10.0.0.5/24"}]
    api.nodes.assert_called_with("pve2")
    api.nodes.return_value.lxc.assert_called_with(200)
    guest.interfaces.get.assert_called_once_with()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_client.py::test_agent_network_interfaces tests/test_client.py::test_lxc_interfaces -v --no-cov`
Expected: FAIL with `AttributeError: ... does not have the attribute 'agent_network_interfaces'`

- [ ] **Step 3: Implement the methods**

In `pmox/client.py`, add immediately after `guest_config` (after the `update_config`/`resize_disk` block is fine too — keep it in the guests section):

```python
    def agent_network_interfaces(self, node: str, vmid) -> Any:
        """QEMU guest-agent network interfaces (needs the agent running in the guest).

        The hyphenated path segment ``network-get-interfaces`` isn't a valid Python
        identifier, so it's addressed via ``getattr`` (same as ``download-url``).
        """
        agent = self._guest(node, "qemu", vmid).agent
        return getattr(agent, "network-get-interfaces").get()

    def lxc_interfaces(self, node: str, vmid) -> list:
        """Network interfaces of a running LXC container (no guest agent needed)."""
        return self._guest(node, "lxc", vmid).interfaces.get()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_client.py::test_agent_network_interfaces tests/test_client.py::test_lxc_interfaces -v --no-cov`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add pmox/client.py tests/test_client.py
git commit -m "Add client methods for QEMU agent + LXC network interfaces"
```

---

### Task 2: Normalization in views.py

**Files:**
- Modify: `pmox/views.py` (add helpers + `guest_ip_addresses` after `describe_guest`)
- Test: `tests/test_views.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_views.py`:

```python
def test_addr_scope_classifies():
    assert views._addr_scope("ipv4", "127.0.0.1") == "loopback"
    assert views._addr_scope("ipv4", "169.254.1.1") == "link"
    assert views._addr_scope("ipv4", "192.168.1.50") == "global"
    assert views._addr_scope("ipv6", "::1") == "loopback"
    assert views._addr_scope("ipv6", "fe80::1") == "link"
    assert views._addr_scope("ipv6", "2001:db8::5") == "global"


def test_locate_guest_finds_row():
    c = MagicMock()
    c.cluster_resources.return_value = [
        {"vmid": 100, "node": "pve1", "name": "web"},
        {"vmid": 200, "node": "pve2", "name": "db"},
    ]
    assert views._locate_guest(c, 200) == {"vmid": 200, "node": "pve2", "name": "db"}
    assert views._locate_guest(c, 999) is None


def _ip_client(row):
    c = MagicMock()
    c.cluster_resources.return_value = [row] if row else []
    return c


def test_guest_ip_addresses_qemu_normalizes():
    c = _ip_client({"vmid": 150, "node": "lukeserver", "name": "web-01"})
    c.agent_network_interfaces.return_value = {"result": [
        {"name": "lo", "hardware-address": "00:00:00:00:00:00",
         "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "127.0.0.1", "prefix": 8}]},
        {"name": "eth0", "hardware-address": "bc:24:11:aa:bb:cc",
         "ip-addresses": [
             {"ip-address-type": "ipv4", "ip-address": "192.168.1.50", "prefix": 24},
             {"ip-address-type": "ipv6", "ip-address": "fe80::1", "prefix": 64},
             {"ip-address-type": "ipv6", "ip-address": "2001:db8::5", "prefix": 64},
             {"ip-address-type": "ipv4"},  # incomplete -> skipped
         ]},
    ]}
    out = views.guest_ip_addresses(c, "qemu", 150)
    assert out["vmid"] == 150 and out["node"] == "lukeserver" and out["name"] == "web-01"
    assert out["kind"] == "qemu" and out["source"] == "guest-agent"
    assert out["primary"] == "192.168.1.50"
    eth0 = next(i for i in out["interfaces"] if i["name"] == "eth0")
    assert eth0["mac"] == "bc:24:11:aa:bb:cc"
    assert len(eth0["addresses"]) == 3  # incomplete entry dropped
    scopes = {(a["address"], a["scope"]) for a in eth0["addresses"]}
    assert ("192.168.1.50", "global") in scopes
    assert ("fe80::1", "link") in scopes
    assert ("2001:db8::5", "global") in scopes
    c.agent_network_interfaces.assert_called_once_with("lukeserver", 150)


def test_guest_ip_addresses_lxc_normalizes():
    c = _ip_client({"vmid": 200, "node": "pve1", "name": "ct-db"})
    c.lxc_interfaces.return_value = [
        {"name": "lo", "hwaddr": "00:00:00:00:00:00", "inet": "127.0.0.1/8"},
        {"name": "eth0", "hwaddr": "aa:bb:cc:dd:ee:ff", "inet": "10.0.0.5/24", "inet6": "fe80::2/64"},
    ]
    out = views.guest_ip_addresses(c, "lxc", 200)
    assert out["kind"] == "lxc" and out["source"] == "lxc-interfaces"
    assert out["primary"] == "10.0.0.5"
    eth0 = next(i for i in out["interfaces"] if i["name"] == "eth0")
    assert {"family": "ipv4", "address": "10.0.0.5", "prefix": 24, "scope": "global"} in eth0["addresses"]
    assert {"family": "ipv6", "address": "fe80::2", "prefix": 64, "scope": "link"} in eth0["addresses"]
    c.lxc_interfaces.assert_called_once_with("pve1", 200)


def test_guest_ip_addresses_explicit_node_no_row():
    c = _ip_client(None)  # vmid not present in cluster_resources
    c.agent_network_interfaces.return_value = {"result": []}
    out = views.guest_ip_addresses(c, "qemu", 150, node="pve9")
    assert out["node"] == "pve9" and out["name"] is None
    assert out["primary"] is None and out["interfaces"] == []
    c.agent_network_interfaces.assert_called_once_with("pve9", 150)


def test_guest_ip_addresses_not_found_raises():
    c = _ip_client(None)
    with pytest.raises(LookupError):
        views.guest_ip_addresses(c, "qemu", 999)


def test_guest_ip_addresses_qemu_agent_down_raises():
    c = _ip_client({"vmid": 150, "node": "lukeserver", "name": "web"})
    c.agent_network_interfaces.side_effect = RuntimeError("500 guest agent is not running")
    with pytest.raises(RuntimeError, match="agent: 1"):
        views.guest_ip_addresses(c, "qemu", 150)


def test_guest_ip_addresses_lxc_stopped_raises():
    c = _ip_client({"vmid": 200, "node": "pve1", "name": "ct"})
    c.lxc_interfaces.side_effect = RuntimeError("500 not running")
    with pytest.raises(RuntimeError, match="stopped"):
        views.guest_ip_addresses(c, "lxc", 200)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_views.py -k "addr_scope or locate_guest or guest_ip_addresses" -v --no-cov`
Expected: FAIL with `AttributeError: module 'pmox.views' has no attribute '_addr_scope'`

- [ ] **Step 3: Implement the normalization**

In `pmox/views.py`, add after `describe_guest` (before `summarize_health`):

```python
def _addr_scope(family: str, address: str) -> str:
    """Classify an IP as 'loopback', 'link', or 'global'."""
    addr = (address or "").lower()
    if family == "ipv4":
        if addr.startswith("127."):
            return "loopback"
        if addr.startswith("169.254."):
            return "link"
        return "global"
    if addr == "::1":
        return "loopback"
    if addr.startswith("fe80"):
        return "link"
    return "global"


def _locate_guest(client, vmid):
    """Cluster-resource row for a vmid (carries node + name), or None."""
    target = int(vmid)
    for r in client.cluster_resources(type="vm"):
        if int(r.get("vmid", -1)) == target:
            return r
    return None


def _parse_qemu_interfaces(payload) -> list:
    """Normalize the QEMU guest-agent network-get-interfaces payload."""
    result = payload.get("result", payload) if isinstance(payload, dict) else payload
    interfaces = []
    for iface in result or []:
        addresses = []
        for a in iface.get("ip-addresses") or []:
            family = a.get("ip-address-type")
            address = a.get("ip-address")
            if not family or not address:
                continue
            addresses.append(
                {"family": family, "address": address, "prefix": a.get("prefix"),
                 "scope": _addr_scope(family, address)}
            )
        interfaces.append({"name": iface.get("name"), "mac": iface.get("hardware-address"), "addresses": addresses})
    return interfaces


def _parse_lxc_interfaces(rows) -> list:
    """Normalize the LXC /interfaces payload (inet/inet6 CIDR strings)."""
    interfaces = []
    for iface in rows or []:
        addresses = []
        for family, key in (("ipv4", "inet"), ("ipv6", "inet6")):
            raw = iface.get(key)
            if not raw:
                continue
            for cidr in str(raw).split():
                address, _, prefix = cidr.partition("/")
                addresses.append(
                    {"family": family, "address": address,
                     "prefix": int(prefix) if prefix.isdigit() else None,
                     "scope": _addr_scope(family, address)}
                )
        interfaces.append({"name": iface.get("name"), "mac": iface.get("hwaddr"), "addresses": addresses})
    return interfaces


def _primary_ipv4(interfaces):
    """First global IPv4 across interfaces, in order (or None)."""
    for iface in interfaces:
        for a in iface["addresses"]:
            if a["family"] == "ipv4" and a["scope"] == "global":
                return a["address"]
    return None


def guest_ip_addresses(client, kind: str, vmid: int, node: Optional[str] = None) -> dict:
    """Live network interfaces + IPs for a guest, normalized across qemu/lxc.

    Raises ``LookupError`` if the guest can't be located, or ``RuntimeError`` with
    an actionable message if the agent/interfaces endpoint can't be read.
    """
    row = _locate_guest(client, vmid)
    node = node or (row.get("node") if row else None)
    if not node:
        raise LookupError(f"Could not locate guest {vmid} in the cluster.")
    name = row.get("name") if row else None

    if kind == "qemu":
        source = "guest-agent"
        try:
            payload = client.agent_network_interfaces(node, vmid)
        except Exception as exc:  # noqa: BLE001 - any agent failure -> actionable message
            raise RuntimeError(
                f"Could not read network interfaces for VM {vmid}: {exc}. "
                f"Ensure qemu-guest-agent is installed and running in the guest and "
                f"'agent: 1' is set (pmox vm set {vmid} -o agent=1 --dangerous)."
            ) from exc
        interfaces = _parse_qemu_interfaces(payload)
    else:
        source = "lxc-interfaces"
        try:
            rows = client.lxc_interfaces(node, vmid)
        except Exception as exc:  # noqa: BLE001 - any failure -> actionable message
            raise RuntimeError(
                f"Could not read network interfaces for CT {vmid}: {exc}. "
                f"The container may be stopped."
            ) from exc
        interfaces = _parse_lxc_interfaces(rows)

    return {
        "vmid": vmid, "node": node, "kind": kind, "name": name, "source": source,
        "primary": _primary_ipv4(interfaces), "interfaces": interfaces,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_views.py -k "addr_scope or locate_guest or guest_ip_addresses" -v --no-cov`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add pmox/views.py tests/test_views.py
git commit -m "Add guest_ip_addresses normalization (qemu agent + lxc interfaces)"
```

---

### Task 3: Embed network in describe (graceful degradation)

**Files:**
- Modify: `pmox/views.py` (`describe_guest` + a `_safe_ip_addresses` wrapper)
- Test: `tests/test_views.py` (update the `_client()` helper, add 2 tests)

- [ ] **Step 1: Update `_client()` and write the failing tests**

In `tests/test_views.py`, extend the existing `_client()` helper so describe's new network call is deterministic — add these three lines before `return c`:

```python
    c.cluster_resources.return_value = [{"vmid": 100, "node": "pve1", "name": "web"}]
    c.agent_network_interfaces.return_value = {"result": []}
    c.lxc_interfaces.return_value = []
```

Then add two tests:

```python
def test_describe_guest_embeds_network():
    c = _client()
    c.agent_network_interfaces.return_value = {"result": [
        {"name": "eth0", "hardware-address": "x",
         "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "10.0.0.9", "prefix": 24}]}
    ]}
    out = views.describe_guest(c, "qemu", 100)
    assert out["network"]["available"] is True
    assert out["network"]["primary"] == "10.0.0.9"


def test_describe_guest_network_degrades_when_agent_down():
    c = _client()
    c.agent_network_interfaces.side_effect = RuntimeError("agent down")
    out = views.describe_guest(c, "qemu", 100)
    assert out["network"]["available"] is False
    assert "reason" in out["network"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_views.py -k "describe_guest_embeds_network or describe_guest_network_degrades" -v --no-cov`
Expected: FAIL with `KeyError: 'network'`

- [ ] **Step 3: Implement the wrapper and wire it into describe_guest**

In `pmox/views.py`, add this helper just above `guest_ip_addresses`:

```python
def _safe_ip_addresses(client, kind, vmid, node) -> dict:
    """guest_ip_addresses wrapped for embedding in describe: never raises."""
    try:
        return {"available": True, **guest_ip_addresses(client, kind, vmid, node=node)}
    except Exception as exc:  # noqa: BLE001 - describe must not break if the agent is down
        return {"available": False, "reason": str(exc)}
```

Then in `describe_guest`, add a `network` entry to the returned dict (after `recent_tasks`):

```python
    return {
        "vmid": vmid,
        "node": node,
        "kind": kind,
        "status": client.guest_status(node, kind, vmid),
        "config": client.guest_config(node, kind, vmid),
        "snapshots": client.list_snapshots(node, kind, vmid),
        "recent_tasks": tasks,
        "network": _safe_ip_addresses(client, kind, vmid, node),
    }
```

- [ ] **Step 4: Run the full views suite to verify it passes**

Run: `python -m pytest tests/test_views.py -v --no-cov`
Expected: PASS (all view tests, including the pre-existing describe tests)

- [ ] **Step 5: Commit**

```bash
git add pmox/views.py tests/test_views.py
git commit -m "Embed live network/IP info in describe with graceful degradation"
```

---

### Task 4: CLI `vm ip` / `ct ip` command + describe rendering

**Files:**
- Modify: `pmox/cli.py` (column specs, render helpers, `ip` command in `build_guest_app`, describe human branch)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py`:

```python
def _ip_row(name="web-01"):
    row = {"vmid": 150, "node": "lukeserver"}
    if name is not None:
        row["name"] = name
    return [row]


def test_vm_ip_filtered(fake_client, creds):
    fake_client.cluster_resources.return_value = _ip_row()
    fake_client.agent_network_interfaces.return_value = {"result": [
        {"name": "lo", "hardware-address": "0", "ip-addresses": [
            {"ip-address-type": "ipv4", "ip-address": "127.0.0.1", "prefix": 8}]},
        {"name": "eth0", "hardware-address": "bc:24:11:aa:bb:cc", "ip-addresses": [
            {"ip-address-type": "ipv4", "ip-address": "192.168.1.50", "prefix": 24},
            {"ip-address-type": "ipv6", "ip-address": "fe80::1", "prefix": 64}]},
    ]}
    r = inv(["--no-json", "vm", "ip", "150"], creds)
    assert r.exit_code == 0, r.output
    out = plain(r.output)
    assert "primary 192.168.1.50" in out
    assert "eth0" in out
    assert "127.0.0.1" not in out  # loopback hidden by default
    assert "fe80::1" not in out    # link-local hidden by default


def test_vm_ip_all_shows_loopback_and_mac(fake_client, creds):
    fake_client.cluster_resources.return_value = _ip_row(name=None)  # exercises name-absent header
    fake_client.agent_network_interfaces.return_value = {"result": [
        {"name": "lo", "ip-addresses": [  # no hardware-address -> MAC '-'
            {"ip-address-type": "ipv4", "ip-address": "127.0.0.1", "prefix": 8}]},
        {"name": "eth0", "hardware-address": "bc:24:11:aa:bb:cc", "ip-addresses": [
            {"ip-address-type": "ipv4", "ip-address": "192.168.1.50", "prefix": 24}]},
    ]}
    r = inv(["--no-json", "vm", "ip", "150", "--all"], creds)
    assert r.exit_code == 0, r.output
    out = plain(r.output)
    assert "127.0.0.1" in out      # loopback shown with --all
    assert "bc:24:11" in out       # MAC shown (fold-safe prefix)


def test_vm_ip_json_full_data(fake_client, creds):
    fake_client.cluster_resources.return_value = _ip_row()
    fake_client.agent_network_interfaces.return_value = {"result": [
        {"name": "eth0", "hardware-address": "bc:24:11:aa:bb:cc", "ip-addresses": [
            {"ip-address-type": "ipv4", "ip-address": "192.168.1.50", "prefix": 24}]},
    ]}
    r = inv(["--json", "vm", "ip", "150"], creds)
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["primary"] == "192.168.1.50"
    assert data["interfaces"][0]["name"] == "eth0"
    fake_client.agent_network_interfaces.assert_called_once_with("lukeserver", 150)


def test_ct_ip_uses_interfaces_endpoint(fake_client, creds):
    fake_client.cluster_resources.return_value = [{"vmid": 200, "node": "pve1", "name": "ct"}]
    fake_client.lxc_interfaces.return_value = [{"name": "eth0", "hwaddr": "aa:bb", "inet": "10.0.0.5/24"}]
    r = inv(["--json", "ct", "ip", "200"], creds)
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["primary"] == "10.0.0.5" and data["source"] == "lxc-interfaces"
    fake_client.lxc_interfaces.assert_called_once_with("pve1", 200)


def test_vm_ip_agent_down_error_envelope(fake_client, creds):
    fake_client.cluster_resources.return_value = _ip_row()
    fake_client.agent_network_interfaces.side_effect = RuntimeError("guest agent is not running")
    r = inv(["--json", "vm", "ip", "150"], creds)
    assert r.exit_code == 1, r.output
    payload = json.loads(r.output)
    assert payload["ok"] is False and payload["error"] == "error"
    assert "agent: 1" in payload["message"]


def test_describe_includes_network(fake_client, creds):
    fake_client.resolve_node.return_value = "lukeserver"
    fake_client.guest_status.return_value = {"status": "running"}
    fake_client.guest_config.return_value = {"cores": 2}
    fake_client.list_snapshots.return_value = []
    fake_client.list_tasks.return_value = []
    fake_client.cluster_resources.return_value = _ip_row()
    fake_client.agent_network_interfaces.return_value = {"result": [
        {"name": "eth0", "hardware-address": "x", "ip-addresses": [
            {"ip-address-type": "ipv4", "ip-address": "192.168.1.50", "prefix": 24}]},
    ]}
    r = inv(["--json", "vm", "describe", "150"], creds)
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["network"]["available"] is True and data["network"]["primary"] == "192.168.1.50"


def test_describe_human_network_unavailable(fake_client, creds):
    fake_client.resolve_node.return_value = "pve1"
    fake_client.guest_status.return_value = {"status": "running"}
    fake_client.guest_config.return_value = {}
    fake_client.list_snapshots.return_value = []
    fake_client.list_tasks.return_value = []
    fake_client.cluster_resources.return_value = [{"vmid": 100, "node": "pve1", "name": "x"}]
    fake_client.agent_network_interfaces.side_effect = RuntimeError("agent down")
    r = inv(["--no-json", "vm", "describe", "100"], creds)
    assert r.exit_code == 0, r.output
    assert "network: unavailable" in plain(r.output)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -k "vm_ip or ct_ip or describe_includes_network or describe_human_network_unavailable" -v --no-cov`
Expected: FAIL (no such command `ip` / missing `network` rendering)

- [ ] **Step 3a: Add column specs and render helpers**

In `pmox/cli.py`, add after the `HEALTH_STORAGE_COLUMNS` block (~line 423):

```python
IP_COLUMNS = [
    Column("Interface", "name"),
    Column("IPv4", "ipv4"),
    Column("IPv6", "ipv6"),
]

IP_ALL_COLUMNS = [
    Column("Interface", "name"),
    Column("MAC", "mac"),
    Column("IPv4", "ipv4"),
    Column("IPv6", "ipv6"),
]


def _ip_rows_filtered(interfaces) -> List[dict]:
    """Non-loopback interfaces with their global IPv4/IPv6 (default view)."""
    rows = []
    for iface in interfaces:
        v4 = [a["address"] for a in iface["addresses"] if a["family"] == "ipv4" and a["scope"] == "global"]
        v6 = [a["address"] for a in iface["addresses"] if a["family"] == "ipv6" and a["scope"] == "global"]
        if not v4 and not v6:
            continue
        rows.append({"name": iface["name"], "ipv4": ", ".join(v4) or "-", "ipv6": ", ".join(v6) or "-"})
    return rows


def _ip_rows_all(interfaces) -> List[dict]:
    """Every interface and address with prefixes + MAC (--all view)."""
    rows = []
    for iface in interfaces:
        v4 = [f'{a["address"]}/{a["prefix"]}' for a in iface["addresses"] if a["family"] == "ipv4"]
        v6 = [f'{a["address"]}/{a["prefix"]}' for a in iface["addresses"] if a["family"] == "ipv6"]
        rows.append({
            "name": iface["name"], "mac": iface.get("mac") or "-",
            "ipv4": ", ".join(v4) or "-", "ipv6": ", ".join(v6) or "-",
        })
    return rows


def _print_network_section(network) -> None:
    """Render the network block inside `describe` (human mode)."""
    if not network.get("available"):
        console.print(f"[dim]network: unavailable ({network.get('reason', 'unknown')})[/dim]")
        return
    console.print(f"network · primary {network.get('primary') or '-'}")
    emit(_ip_rows_filtered(network.get("interfaces", [])), columns=IP_COLUMNS, json_output=False)
```

- [ ] **Step 3b: Add the `ip` command to the guest factory**

In `pmox/cli.py`, inside `build_guest_app`, add this command right after the `_describe` command (after its closing, ~line 612):

```python
    @group.command("ip", help=f"Show the live IP address(es) of a {label} (VM: via guest agent; CT: via interfaces).")
    def _ip(
        ctx: typer.Context,
        vmid: int = vmid_arg,
        node: Optional[str] = node_opt,
        all_: bool = typer.Option(False, "--all", "-a", help="Include loopback, IPv6 link-local, and MAC addresses."),
    ):
        with error_boundary(ctx.obj.json):
            client = _get_client(ctx)
            data = views.guest_ip_addresses(client, kind, vmid, node=node)
            if ctx.obj.json:
                emit(data, json_output=True)
                return
            name = f" ({data['name']})" if data.get("name") else ""
            console.print(f"{label} {vmid}{name} on {data['node']} · primary {data['primary'] or '-'}")
            rows = _ip_rows_all(data["interfaces"]) if all_ else _ip_rows_filtered(data["interfaces"])
            emit(rows, columns=(IP_ALL_COLUMNS if all_ else IP_COLUMNS), json_output=False)
```

- [ ] **Step 3c: Render the network section in describe (human mode)**

In `pmox/cli.py`, in the `_describe` command's `else` (human) branch, insert the network section between the `config` table and the snapshots table:

```python
            else:
                console.print(build_kv_table(data["status"], title=f"{label} {vmid} status"))
                console.print(build_kv_table(data["config"], title="config"))
                _print_network_section(data["network"])
                emit(data["snapshots"], columns=SNAPSHOT_COLUMNS, json_output=False, title="snapshots")
                emit(data["recent_tasks"], columns=TASK_COLUMNS, json_output=False, title="recent tasks")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -k "vm_ip or ct_ip or describe_includes_network or describe_human_network_unavailable" -v --no-cov`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add pmox/cli.py tests/test_cli.py
git commit -m "Add vm ip / ct ip command and surface IPs in describe"
```

---

### Task 5: Docs + full-suite verification

**Files:**
- Modify: `README.md`, `plugin/skills/proxmox/SKILL.md`, `CHANGELOG.md`

- [ ] **Step 1: README — add the command + a short note**

Open `README.md`, find the command listing that includes `vm describe` / `vm status` / `vm config`, and add an `ip` line in the same style, e.g.:

```
pmox vm ip <vmid>                        # live IP(s) from the guest agent (--all for everything)
pmox ct ip <vmid>                        # live IP(s) from the container interfaces
```

Then add a one-paragraph note near the provisioning/`--ip dhcp` material:

> **Finding a guest's IP.** After creating a DHCP guest, read its assigned address with `pmox vm ip <vmid>` (or `ct ip`). For VMs this uses the QEMU guest agent, so the guest needs `qemu-guest-agent` installed and running and `agent: 1` set (cloud-init VMs from `vm new` already have `agent: 1`). Containers need no agent.

- [ ] **Step 2: SKILL.md — document the command**

Open `plugin/skills/proxmox/SKILL.md`. Next to the existing `vm describe` / `vm status` / `vm config` references (around lines 110 and 246), add:

```
pmox vm ip <vmid>                        # live IP(s): VM via guest agent, CT via interfaces
pmox ct ip <vmid>
```

And add a short caveat where guest-agent behavior is relevant:

> `vm ip` needs the QEMU guest agent running in the guest (cloud-init VMs from `vm new` enable `agent: 1`); `ct ip` needs no agent.

- [ ] **Step 3: CHANGELOG — add an entry**

Open `CHANGELOG.md`. Under the top/Unreleased section, add:

```markdown
### Added
- `vm ip` / `ct ip`: read a guest's live IP address(es) — VMs via the QEMU guest agent, containers via the LXC interfaces endpoint. Filtered by default (`--all` shows loopback, IPv6 link-local, and MACs); JSON returns the full per-interface data. IPs also appear in `describe`.
```

- [ ] **Step 4: Commit docs**

```bash
git add README.md plugin/skills/proxmox/SKILL.md CHANGELOG.md
git commit -m "Document vm ip / ct ip command"
```

- [ ] **Step 5: Run the FULL suite with coverage (the 100% gate)**

Run: `python -m pytest`
Expected: PASS, `Required test coverage of 100% reached`. If coverage < 100%, read the `term-missing` report and add tests for the uncovered lines (do not lower the threshold).

- [ ] **Step 6: Smoke-check the CLI wiring (no cluster needed)**

Run: `python -m pmox vm ip --help` and `python -m pmox ct ip --help`
Expected: help text shows the `--all/-a` and `--node/-n` options; exit 0.

---

## Self-Review

**1. Spec coverage** (`docs/superpowers/specs/2026-06-07-guest-ip-exposure-design.md`):
- §3.1 client methods → Task 1. ✓
- §4 normalization (`guest_ip_addresses`, `_addr_scope`, `_locate_guest`, parsers, `primary`) → Task 2. ✓
- §4.1 describe embed with uniform `available` flag → Task 3. ✓
- §5 CLI surface (`vm ip`/`ct ip`, filtered default, `--all/-a`, JSON full dict, describe rendering) → Task 4. ✓
- §6 error handling (LookupError; actionable RuntimeError for VM agent + CT stopped; running-but-no-lease → primary null) → Tasks 2 & 4. ✓
- §7 tests (client, views, cli) → Tasks 1–4. ✓
- §8 docs (README, SKILL.md, CHANGELOG) → Task 5. ✓
- §9 out-of-scope items are not implemented. ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows complete code; doc steps give exact text. ✓

**3. Type/name consistency:** `guest_ip_addresses`, `_addr_scope`, `_locate_guest`, `_parse_qemu_interfaces`, `_parse_lxc_interfaces`, `_primary_ipv4`, `_safe_ip_addresses`, `_ip_rows_filtered`, `_ip_rows_all`, `_print_network_section`, `IP_COLUMNS`, `IP_ALL_COLUMNS` are used consistently across tasks. Normalized interface shape (`name`/`mac`/`addresses[{family,address,prefix,scope}]`) and top-level keys (`vmid/node/kind/name/source/primary/interfaces`) match between producer (Task 2) and consumers (Tasks 3–4). ✓

**Coverage note:** statement coverage (no `--cov-branch`), so each line need only execute once. The `continue` lines are exercised (incomplete QEMU address; LXC interface missing `inet6`); `_print_network_section` hits both available/unavailable lines; the `ip` command hits json/human/`--all` lines and the name-present/name-absent header (the `--all` test uses a row without `name`).
