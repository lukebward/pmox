"""Provisioning orchestration: build an ordered list of API steps, then execute.

A *plan* is a list of step dicts (``op``/``args``/``await_task``/``describe``).
Splitting plan-building from execution lets ``--dry-run`` print the whole plan
and lets tests assert the plan without touching a cluster. ``execute_plan``
dispatches each step to the :class:`~pmox.client.ProxmoxClient` and waits on
task UPIDs between dependent steps.
"""

from __future__ import annotations

from urllib.parse import quote

from . import catalog


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


def _resolve_image_volid(client, node: str, storage: str, image: str):
    """Return (volid, download_step_or_None) for a --image argument."""
    spec = catalog.resolve_image(image)
    if spec["kind"] == "volid":
        return spec["volid"], None
    volid = f"{storage}:import/{spec['filename']}"
    present = any(c.get("volid") == volid for c in client.storage_content(node, storage))
    if present:
        return volid, None
    download = step(
        "download_url",
        {
            "node": node,
            "storage": storage,
            "url": spec["url"],
            "content": "import",
            "filename": spec["filename"],
            "checksum": spec["checksum"],
            "checksum_algorithm": spec["algo"],
        },
        await_task=True,
        describe=f"download {spec['filename']}",
    )
    return volid, download


def build_vm_image_plan(
    client,
    *,
    node,
    vmid,
    name,
    cores,
    memory,
    disk,
    storage,
    image,
    sshkeys=None,
    ipconfig=None,
    ciuser=None,
    cipassword=None,
    nameserver=None,
    start=True,
) -> list:
    """Build the ordered plan for an all-in-one cloud-init VM."""
    volid, download = _resolve_image_volid(client, node, storage, image)
    plan = []
    if download:
        plan.append(download)

    create_args = {
        "node": node,
        "kind": "qemu",
        "vmid": vmid,
        "cores": cores,
        "memory": memory,
        "scsihw": "virtio-scsi-single",
        "net0": "virtio,bridge=vmbr0",
        "ostype": "l26",
        "serial0": "socket",
        "vga": "serial0",
        "agent": 1,
        "scsi0": f"{storage}:0,import-from={volid},iothread=1",
        "ide2": f"{storage}:cloudinit",
        "boot": "order=scsi0",
    }
    if name:
        create_args["name"] = name
    if sshkeys:
        create_args["sshkeys"] = encode_sshkeys(sshkeys)
    if ipconfig:
        create_args["ipconfig0"] = ipconfig
    if ciuser:
        create_args["ciuser"] = ciuser
    if cipassword:
        create_args["cipassword"] = cipassword
    if nameserver:
        create_args["nameserver"] = nameserver
    plan.append(step("create_guest", create_args, await_task=True, describe=f"create VM {vmid} (import {volid})"))

    if disk:
        plan.append(
            step(
                "resize_disk",
                {"node": node, "kind": "qemu", "vmid": vmid, "disk": "scsi0", "size": f"{disk}G"},
                await_task=False,
                describe=f"grow scsi0 to {disk}G",
            )
        )
    if start:
        plan.append(
            step(
                "guest_power",
                {"node": node, "kind": "qemu", "vmid": vmid, "action": "start"},
                await_task=True,
                describe="start",
            )
        )
    return plan
