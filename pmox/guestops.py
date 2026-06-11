"""In-guest setup over SSH: turn a fresh cloud VM into an agent-ready template.

pmox is token-only against the Proxmox API, which cannot install software
inside a guest. ``template build`` needs exactly one in-guest operation —
installing ``qemu-guest-agent`` — and the guest is one pmox just created with
its own injected SSH key, so a short SSH session adds no new trust. This is
the only module that reaches inside a guest.

Subprocess/socket edges are module-level functions so tests monkeypatch them;
naming/script helpers are pure.
"""

from __future__ import annotations

import re
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

from .arp import normalize_mac

AGENT_TAG = "pmox-agent"

_TAG_SAFE = re.compile(r"[^a-z0-9_.-]+")
_NAME_SAFE = re.compile(r"[^a-z0-9-]+")
_STAGE_RE = re.compile(r"===pmox:([a-z-]+)===")

_PORT_PROBE_SECONDS = 5
_PORT_POLL_SECONDS = 2


def image_tag(image: str) -> str:
    """Proxmox tag marking which image a template was built from."""
    return "img-" + _TAG_SAFE.sub("-", image.lower()).strip("-")


def agent_template_name(image: str) -> str:
    """A valid guest name for the agent template of ``image`` (≤63 chars)."""
    return ("agent-" + _NAME_SAFE.sub("-", image.lower()))[:63].strip("-")


def agent_tags(image: str) -> str:
    """The tag pair that makes an agent template discoverable by ``vm up``."""
    return f"{AGENT_TAG};{image_tag(image)}"


def private_key_path(pub_path: str) -> str:
    """The private key matching a ``.pub`` path; the SSH session needs it."""
    pub = Path(pub_path).expanduser()
    if pub.suffix == ".pub":
        priv = pub.with_suffix("")
        if priv.exists():
            return str(priv)
    raise FileNotFoundError(
        f"No private key found for {pub_path!r} — the template build SSHes into the "
        f"guest, which needs the private half next to the .pub file."
    )


def ssh_command(user: str, host: str, key_path: str, remote_cmd: str, platform: str = None) -> list:
    """Build the ssh argv: batch mode, no known-hosts pollution, our key only."""
    devnull = "NUL" if (platform or sys.platform) == "win32" else "/dev/null"
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", f"UserKnownHostsFile={devnull}",
        "-o", "ConnectTimeout=15",
        "-i", key_path,
        f"{user}@{host}",
        remote_cmd,
    ]


def eui64_link_local(mac: str) -> str:
    """The EUI-64 IPv6 link-local address derived from a MAC.

    This is the no-discovery transport: the address is a pure function of the
    MAC (which the Proxmox config provides), and neighbor resolution uses NDP —
    so ARP spoofing, DHCP state, and IPv4 config are all irrelevant.
    """
    b = bytes.fromhex(normalize_mac(mac))
    b = bytes([b[0] ^ 0x02]) + b[1:]
    return f"fe80::{b[0]:02x}{b[1]:02x}:{b[2]:02x}ff:fe{b[3]:02x}:{b[4]:02x}{b[5]:02x}"


def link_local_candidates(mac: str, platform: str = None) -> list:
    """Scoped link-local literals for ``mac``, one per local interface.

    Link-local addresses need a zone: Windows wants the interface index,
    POSIX systems the interface name. Enumeration failure just means no
    candidates — callers fall back to IPv4.
    """
    address = eui64_link_local(mac)
    try:
        interfaces = socket.if_nameindex()
    except OSError:
        return []
    use_index = (platform or sys.platform) == "win32"
    return [f"{address}%{index if use_index else name}" for index, name in interfaces]


def wait_for_port(host: str, port: int = 22, timeout: int = 180) -> None:
    """Block until ``host:port`` accepts a TCP connection (the guest's sshd)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            socket.create_connection((host, port), timeout=_PORT_PROBE_SECONDS).close()
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Port {port} on {host} did not open within {timeout}s — cannot SSH "
                    f"into the build VM. Check that the address is reachable from here."
                ) from None
        time.sleep(_PORT_POLL_SECONDS)


def wait_for_any_port(candidates: list, port: int = 22, timeout: int = 240) -> str:
    """Round-robin the candidate addresses until one accepts TCP; returns it.

    Covers the guest's boot window: candidates are probed in order, repeatedly,
    so whichever address comes up first wins.
    """
    deadline = time.monotonic() + timeout
    while True:
        for candidate in candidates:
            try:
                socket.create_connection((candidate, port), timeout=_PORT_PROBE_SECONDS).close()
                return candidate
            except OSError:
                continue
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Port {port} did not open within {timeout}s on any candidate address "
                f"({', '.join(candidates)}). Check that the guest boots and is reachable from here."
            )
        time.sleep(_PORT_POLL_SECONDS)


def establish_agent_ssh(user: str, candidates: list, key_path: str,
                        port_timeout: int = 240, auth_retry_seconds: int = 120) -> str:
    """Run the agent setup over SSH against the first candidate that works.

    Tries the address whose port opens first, then falls through the remaining
    candidates on failure (a poisoned LAN can route an IPv4 candidate to the
    wrong box — the link-local candidates are immune, so one of them lands).
    Returns the address that succeeded.
    """
    winner = wait_for_any_port(candidates, 22, timeout=port_timeout)
    order = [winner] + [c for c in candidates if c != winner]
    errors = []
    for candidate in order:
        try:
            if candidate != winner:
                wait_for_port(candidate, 22, timeout=20)
            install_agent(user, candidate, key_path, retry_seconds=auth_retry_seconds)
            return candidate
        except RuntimeError as exc:
            errors.append(f"{candidate}: {exc}")
    raise RuntimeError("Agent setup failed on every candidate address — " + " | ".join(errors))


def run_ssh(user: str, host: str, key_path: str, remote_cmd: str, timeout: int = 900):
    """Run one remote command; returns the CompletedProcess (never raises on rc)."""
    argv = ssh_command(user, host, key_path, remote_cmd)
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError(
            "The OpenSSH client (`ssh`) is required for template build but was not "
            "found on PATH."
        ) from None


# One root shell, staged with markers so a failure names the step that died.
# Kept free of single quotes so it survives shlex.quote unchanged.
AGENT_SETUP_SCRIPT = """set -e
echo ===pmox:cloud-init===
cloud-init status --wait || true
echo ===pmox:install===
if command -v apt-get >/dev/null 2>&1; then
  for i in 1 2 3 4 5; do
    if apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq qemu-guest-agent; then break; fi
    if [ "$i" = 5 ]; then exit 1; fi
    sleep 10
  done
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y qemu-guest-agent
else
  echo no supported package manager found - need apt-get or dnf >&2
  exit 1
fi
echo ===pmox:enable===
systemctl enable --now qemu-guest-agent
echo ===pmox:clean===
cloud-init clean --logs
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
rm -f /etc/ssh/ssh_host_*
echo ===pmox:done===
"""


_AUTH_RETRY_POLL_SECONDS = 5


def install_agent(user: str, host: str, key_path: str, timeout: int = 900,
                  retry_seconds: int = 180) -> None:
    """Install + enable qemu-guest-agent and clean the guest for templating.

    The cleanup (cloud-init state, machine-id, SSH host keys) is what makes
    clones come up as distinct hosts with fresh DHCP identities.

    SSH exit 255 (connection/auth layer) is retried for ``retry_seconds``:
    cloud images socket-activate sshd, so the port opens before cloud-init has
    written authorized_keys, and early attempts get "Permission denied". A
    non-255 exit means the remote script itself failed — no retry.
    """
    cmd = f"sudo bash -c {shlex.quote(AGENT_SETUP_SCRIPT)}"
    deadline = time.monotonic() + retry_seconds
    while True:
        proc = run_ssh(user, host, key_path, cmd, timeout=timeout)
        if proc.returncode == 0:
            return
        if proc.returncode == 255 and time.monotonic() < deadline:
            time.sleep(_AUTH_RETRY_POLL_SECONDS)
            continue
        stages = _STAGE_RE.findall(proc.stdout or "")
        stage = stages[-1] if stages else "connect"
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise RuntimeError(
            f"Agent setup failed at stage {stage!r} (ssh exit {proc.returncode}): {tail}"
        )
