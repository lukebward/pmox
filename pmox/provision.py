"""Provisioning orchestration: build an ordered list of API steps, then execute.

A *plan* is a list of step dicts (``op``/``args``/``await_task``/``describe``).
Splitting plan-building from execution lets ``--dry-run`` print the whole plan
and lets tests assert the plan without touching a cluster. ``execute_plan``
dispatches each step to the :class:`~pmox.client.ProxmoxClient` and waits on
task UPIDs between dependent steps.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from . import catalog
from .errors import PlanError


def encode_sshkeys(text: str) -> str:
    """URL-encode SSH public keys for the QEMU ``sshkeys`` config value."""
    return quote(text, safe="")


def ensure_ssh_key(path: str) -> str:
    """Return the public-key text at ``path``; generate an ed25519 keypair if absent."""
    pub = Path(path).expanduser()
    if not pub.exists():
        if pub.suffix != ".pub":
            raise ValueError(f"ssh key path must end in .pub to generate (got {path!r}).")
        priv = pub.with_suffix("")
        priv.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(priv), "-N", "", "-C", "pmox"],
            check=True, capture_output=True, text=True,
        )
    return pub.read_text().strip()


def read_ssh_keys(paths) -> Optional[str]:
    """Read and join SSH public keys, expanding ``~`` (PowerShell and quoted shell
    arguments pass it through literally)."""
    keys = []
    for p in paths or []:
        path = Path(p).expanduser()
        if not path.exists():
            raise FileNotFoundError(
                f"SSH public key not found: {p}. Generate one with `ssh-keygen -t ed25519` "
                f"or pass a different --ssh-key."
            )
        keys.append(path.read_text().strip())
    return "\n".join(keys) or None


_DNS_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def validate_guest_name(name: str) -> str:
    """Fail fast on names Proxmox would reject server-side (DNS-name format).

    Without this, a bad name (underscores are the classic) surfaces as an API
    error only *after* a potentially minutes-long image download.
    """
    text = str(name)
    if text and len(text) <= 253 and all(_DNS_LABEL.match(label) for label in text.split(".")):
        return name
    raise ValueError(
        f"Invalid guest name {name!r}: must be a DNS name — letters, digits and hyphens "
        f"(no underscores; labels can't start or end with '-')."
    )


_IPCONFIG_KEYS = frozenset({"gw", "gw6", "ip6"})


def validate_ip_spec(spec: str) -> str:
    """Validate a --ip value: ``dhcp`` or ``<addr>/<prefix>[,gw=<ip>][,gw6=…][,ip6=…]``.

    Returns the normalized spec (``dhcp`` lowercased). Like name validation, this
    runs before any download/create so typos fail instantly.
    """
    if spec.strip().lower() == "dhcp":
        return "dhcp"
    addr, _, rest = spec.partition(",")
    try:
        if "/" not in addr:
            raise ValueError
        ipaddress.ip_interface(addr)
    except ValueError:
        raise ValueError(
            f"Invalid --ip {spec!r}: expected 'dhcp' or '<address>/<prefix>[,gw=<gateway>]' "
            f"(e.g. 192.168.1.50/24,gw=192.168.1.1)."
        ) from None
    for part in filter(None, rest.split(",")):
        key, sep, val = part.partition("=")
        if not sep or key not in _IPCONFIG_KEYS:
            raise ValueError(f"Invalid --ip option {part!r}: allowed keys are gw=, gw6=, ip6=.")
        if key in ("gw", "gw6"):
            try:
                ipaddress.ip_address(val)
            except ValueError:
                raise ValueError(f"Invalid gateway {val!r} in --ip {spec!r}.") from None
        elif val.lower() not in ("dhcp", "auto", "manual"):
            try:
                ipaddress.ip_interface(val)
            except ValueError:
                raise ValueError(f"Invalid ip6 value {val!r} in --ip {spec!r}.") from None
    return spec


def build_ipconfig(spec: str) -> str:
    """Validate and turn a friendly --ip value into a Proxmox ipconfig string."""
    validated = validate_ip_spec(spec)
    return "ip=dhcp" if validated == "dhcp" else f"ip={validated}"


_CHECKSUM_ALGOS = frozenset({"md5", "sha1", "sha224", "sha256", "sha384", "sha512"})


def parse_checksum(value: str) -> tuple:
    """Split ``<algo>:<hexdigest>`` for --checksum; raises ``ValueError`` on bad shape."""
    algo, sep, digest = value.partition(":")
    algo = algo.strip().lower()
    if not sep or algo not in _CHECKSUM_ALGOS or not digest.strip():
        raise ValueError(
            f"--checksum must be <algo>:<hexdigest> with algo one of "
            f"{', '.join(sorted(_CHECKSUM_ALGOS))} (got {value!r})."
        )
    return algo, digest.strip()


def resolve_import_storage(client, node: str, explicit=None) -> str:
    """Pick a file-based storage that can hold imported cloud-image disks.

    A token can't enable storage content types, so when none is available we
    raise an actionable error instead of guessing.
    """
    storages = client.list_storage(node)
    if explicit:
        match = next((s for s in storages if s.get("storage") == explicit), None)
        if match is None:
            raise LookupError(f"Storage {explicit!r} not found on {node}.")
        if "import" not in str(match.get("content", "")).split(","):
            raise RuntimeError(
                f"Storage {explicit!r} on {node} lacks the 'import' content type. "
                f"Enable it: Datacenter -> Storage -> {explicit} -> Edit -> check 'Import', "
                f"or `pvesm set {explicit} --content <existing>,import`."
            )
        return explicit
    candidates = [s for s in storages if "import" in str(s.get("content", "")).split(",")]
    if not candidates:
        raise RuntimeError(
            f"No storage on {node} has the 'import' content type, required to import a "
            f"cloud image. Enable it on a directory storage: Datacenter -> Storage -> "
            f"<storage> -> Edit -> check 'Import', or `pvesm set <storage> --content <existing>,import`."
        )
    candidates.sort(key=lambda s: (s.get("plugintype") != "dir", s.get("storage", "")))
    return candidates[0]["storage"]


def resolve_disk_storage(client, node: str, explicit=None, *, content: str = "images") -> str:
    """Pick a storage for guest disks (``images``) or a container rootfs (``rootdir``).

    ``local-lvm`` is preferred when present (the Proxmox default), else the first
    capable active storage alphabetically. An explicit choice is validated so a
    typo fails fast instead of after a slow image download.
    """
    storages = client.list_storage(node)

    def _contents(s) -> list:
        return str(s.get("content", "")).split(",")

    if explicit:
        match = next((s for s in storages if s.get("storage") == explicit), None)
        if match is None:
            raise LookupError(f"Storage {explicit!r} not found on {node}.")
        if content not in _contents(match):
            raise RuntimeError(
                f"Storage {explicit!r} on {node} does not support {content!r} content; "
                f"pick one that does (see `pmox storage list`)."
            )
        return explicit
    candidates = [
        s for s in storages
        if content in _contents(s) and s.get("active", 1) != 0 and s.get("enabled", 1) != 0
    ]
    if not candidates:
        raise RuntimeError(
            f"No active storage on {node} supports {content!r} content; pass --storage explicitly."
        )
    candidates.sort(key=lambda s: (s.get("storage") != "local-lvm", s.get("storage", "")))
    return candidates[0]["storage"]


def step(op: str, args: dict, *, await_task: bool = False, describe: str = "") -> dict:
    """Construct one plan step."""
    return {"op": op, "args": args, "await_task": await_task, "describe": describe}


def _plan_error(exc, *, plan: list, index: int, node: str, started: bool) -> PlanError:
    """Wrap a step failure with everything an agent needs to recover.

    ``started`` is True when the step's API call succeeded but waiting on its task
    failed — for a create step that means the guest may exist half-configured.
    """
    failed = plan[index]
    label = failed["describe"] or failed["op"]
    create_ops = ("create_guest", "clone_guest")
    newid = next((s["args"].get("newid") for s in plan if s["args"].get("newid") is not None), None)
    vmid = newid if newid is not None else next(
        (s["args"].get("vmid") for s in plan if s["args"].get("vmid") is not None), None
    )
    kind = next((s["args"].get("kind") for s in plan if s["args"].get("kind")), "qemu")
    cmd = "vm" if kind == "qemu" else "ct"
    created = any(s["op"] in create_ops for s in plan[:index]) or (started and failed["op"] in create_ops)
    extra = dict(getattr(exc, "extra", {}) or {})
    extra.update({
        "node": node,
        "failed_step": label,
        "completed_steps": [s["describe"] or s["op"] for s in plan[:index]],
    })
    if vmid is not None:
        extra["vmid"] = vmid
    if created and vmid is not None:
        extra["hint"] = (
            f"{cmd} {vmid} was created on {node} but provisioning stopped at {label!r}. "
            f"Inspect with `pmox {cmd} describe {vmid}`; finish manually or delete it before "
            f"retrying (a plain retry would create a second guest under a new VMID)."
        )
    else:
        extra["hint"] = "No guest was created yet, so retrying the same command is safe."
    return PlanError(f"Provisioning failed at {label!r}: {exc}", extra=extra)


def execute_plan(client, node: str, plan: list, waiter) -> list:
    """Run each step against ``client``; wait on UPID-returning steps via ``waiter``.

    ``waiter`` is a callable ``(node, upid) -> None`` (the CLI supplies one that
    polls task status with the configured timeout). A step failure is re-raised
    as :class:`PlanError` carrying the vmid, completed steps, and a recovery hint.
    """
    results = []
    for idx, s in enumerate(plan):
        try:
            result = getattr(client, s["op"])(**s["args"])
        except Exception as exc:  # noqa: BLE001 - wrap with recovery context
            raise _plan_error(exc, plan=plan, index=idx, node=node, started=False) from exc
        try:
            if s["await_task"]:
                waiter(node, result)
        except Exception as exc:  # noqa: BLE001 - the call succeeded; its task did not
            raise _plan_error(exc, plan=plan, index=idx, node=node, started=True) from exc
        results.append(result)
    return results


def _resolve_image_volid(client, node: str, import_storage: str, image: str):
    """Return (volid, download_step_or_None) for a --image argument.

    ``import_storage`` is the file-based storage that holds the downloaded image;
    the VM's disk lives on a separate (possibly lvmthin) storage.
    """
    spec = catalog.resolve_image(image)
    if spec["kind"] == "volid":
        if ":iso/" in spec["volid"]:
            raise ValueError(
                f"{spec['volid']!r} is an installer ISO, not a cloud image. Use a cloud image: "
                f"a catalog name (e.g. 'ubuntu-24.04'), an https URL, or an import volid."
            )
        return spec["volid"], None
    volid = f"{import_storage}:import/{spec['filename']}"
    present = any(c.get("volid") == volid for c in client.storage_content(node, import_storage))
    if present:
        return volid, None
    download = step(
        "download_url",
        {
            "node": node,
            "storage": import_storage,
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
    import_storage=None,
    image,
    sshkeys=None,
    ipconfig=None,
    ciuser=None,
    cipassword=None,
    nameserver=None,
    start=True,
    extra=None,
) -> list:
    """Build the ordered plan for an all-in-one cloud-init VM."""
    if name:
        validate_guest_name(name)
    volid, download = _resolve_image_volid(client, node, import_storage or storage, image)
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
    if extra:
        create_args.update(extra)
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


def build_vm_clone_plan(
    client,
    *,
    node,
    template_id,
    newid,
    name,
    disk,
    sshkeys=None,
    ipconfig=None,
    ciuser=None,
    cipassword=None,
    nameserver=None,
    full=True,
    start=True,
) -> list:
    """Build the plan for cloning a template into a ready-to-SSH VM."""
    if name:
        validate_guest_name(name)
    clone_args = {"node": node, "kind": "qemu", "vmid": template_id, "newid": newid}
    if name:
        clone_args["name"] = name
    if full:
        clone_args["full"] = 1
    plan = [step("clone_guest", clone_args, await_task=True, describe=f"clone {template_id} -> {newid}")]

    ci = {}
    if sshkeys:
        ci["sshkeys"] = encode_sshkeys(sshkeys)
    if ipconfig:
        ci["ipconfig0"] = ipconfig
    if ciuser:
        ci["ciuser"] = ciuser
    if cipassword:
        ci["cipassword"] = cipassword
    if nameserver:
        ci["nameserver"] = nameserver
    if ci:
        plan.append(step("update_config", {"node": node, "kind": "qemu", "vmid": newid, **ci}, await_task=False, describe="set cloud-init"))

    if disk:
        plan.append(step("resize_disk", {"node": node, "kind": "qemu", "vmid": newid, "disk": "scsi0", "size": f"{disk}G"}, await_task=False, describe=f"grow scsi0 to {disk}G"))
    if start:
        plan.append(step("guest_power", {"node": node, "kind": "qemu", "vmid": newid, "action": "start"}, await_task=True, describe="start"))
    return plan


def match_appliance(names, template: str) -> Optional[str]:
    """Best aplinfo filename for a template query: exact > ``<query>-standard``
    prefix > substring. Among version candidates the lexicographically greatest
    (newest) wins, so ``ubuntu-24.04`` resolves to the latest standard build."""
    if template in names:
        return template
    standard = [n for n in names if n.startswith(f"{template}-standard")]
    if standard:
        return max(standard)
    subs = [n for n in names if template in n]
    return max(subs) if subs else None


def _resolve_appliance(client, node, template_storage, template):
    """Return (ostemplate_volid, download_step_or_None) for a CT template name/volid."""
    if ":vztmpl/" in template:
        return template, None
    names = [a.get("template", "") for a in client.list_appliances(node)]
    filename = match_appliance(names, template)
    if not filename:
        raise LookupError(
            f"No container template matching {template!r} available on {node}. "
            f"See `pmox image list --ct --node {node}`."
        )
    volid = f"{template_storage}:vztmpl/{filename}"
    present = any(c.get("volid") == volid for c in client.storage_content(node, template_storage))
    if present:
        return volid, None
    download = step(
        "download_appliance",
        {"node": node, "storage": template_storage, "template": filename},
        await_task=True,
        describe=f"download template {filename}",
    )
    return volid, download


def build_ct_plan(
    client,
    *,
    node,
    vmid,
    hostname,
    template,
    storage,
    template_storage,
    disk,
    cores,
    memory,
    sshkeys=None,
    ip="dhcp",
    password=None,
    start=True,
) -> list:
    """Build the ordered plan for creating a ready-to-use LXC container."""
    if hostname:
        validate_guest_name(hostname)
    ip = validate_ip_spec(ip)
    ostemplate, download = _resolve_appliance(client, node, template_storage, template)
    plan = []
    if download:
        plan.append(download)

    create_args = {
        "node": node,
        "kind": "lxc",
        "vmid": vmid,
        "ostemplate": ostemplate,
        "rootfs": f"{storage}:{disk}",
        "cores": cores,
        "memory": memory,
        "net0": f"name=eth0,bridge=vmbr0,ip={ip}",
        "unprivileged": 1,
    }
    if hostname:
        create_args["hostname"] = hostname
    if sshkeys:
        create_args["ssh-public-keys"] = sshkeys
    if password:
        create_args["password"] = password
    plan.append(step("create_guest", create_args, await_task=True, describe=f"create CT {vmid}"))

    if start:
        plan.append(step(
            "guest_power",
            {"node": node, "kind": "lxc", "vmid": vmid, "action": "start"},
            await_task=True,
            describe="start",
        ))
    return plan


def build_template_plan(client, *, node, vmid, name, storage, image, cores=1, memory=1024) -> list:
    """Build a golden cloud-init template: import the image + cloud-init drive, then convert."""
    plan = build_vm_image_plan(
        client,
        node=node,
        vmid=vmid,
        name=name,
        cores=cores,
        memory=memory,
        disk=None,
        storage=storage,
        image=image,
        start=False,
    )
    plan.append(step("convert_to_template", {"node": node, "kind": "qemu", "vmid": vmid}, await_task=False, describe="convert to template"))
    return plan
