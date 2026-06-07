"""Provisioning orchestration: build an ordered list of API steps, then execute.

A *plan* is a list of step dicts (``op``/``args``/``await_task``/``describe``).
Splitting plan-building from execution lets ``--dry-run`` print the whole plan
and lets tests assert the plan without touching a cluster. ``execute_plan``
dispatches each step to the :class:`~pmox.client.ProxmoxClient` and waits on
task UPIDs between dependent steps.
"""

from __future__ import annotations

from urllib.parse import quote


def encode_sshkeys(text: str) -> str:
    """URL-encode SSH public keys for the QEMU ``sshkeys`` config value."""
    return quote(text, safe="")


def build_ipconfig(spec: str) -> str:
    """Turn a friendly --ip value into a Proxmox ipconfig string."""
    return "ip=dhcp" if spec == "dhcp" else f"ip={spec}"


def step(op: str, args: dict, *, await_task: bool = False, describe: str = "") -> dict:
    """Construct one plan step."""
    return {"op": op, "args": args, "await_task": await_task, "describe": describe}


def execute_plan(client, node: str, plan: list, waiter) -> list:
    """Run each step against ``client``; wait on UPID-returning steps via ``waiter``.

    ``waiter`` is a callable ``(node, upid) -> None`` (the CLI supplies one that
    polls task status with the configured timeout).
    """
    results = []
    for s in plan:
        result = getattr(client, s["op"])(**s["args"])
        if s["await_task"]:
            waiter(node, result)
        results.append(result)
    return results
