"""Static reference data for pmox: VM sizing profiles and cluster-health thresholds.

Kept separate from the API client and CLI so the magic numbers live in one
obvious place and can be unit-tested in isolation.
"""

from __future__ import annotations

# VM sizing profiles: friendly name -> create parameters.
SIZE_PROFILES = {
    "small": {"cores": 1, "memory": 1024},
    "medium": {"cores": 2, "memory": 4096},
    "large": {"cores": 4, "memory": 8192},
}

# Fractions (0-1) above which `pmox health` flags a resource as under pressure.
CPU_PRESSURE = 0.85
MEM_PRESSURE = 0.85
STORAGE_PRESSURE = 0.85


def size_params(size: str) -> dict:
    """Return a fresh copy of the create parameters for a sizing profile."""
    if size not in SIZE_PROFILES:
        raise ValueError(
            f"unknown size {size!r}; choose from: {', '.join(SIZE_PROFILES)}."
        )
    return dict(SIZE_PROFILES[size])


# VM cloud images by short name. `filename` ends in .qcow2 because Proxmox's
# `import` content type recognises disk images by extension (an Ubuntu .img is a
# qcow2). Checksums are optional in v1 (a warning is printed when absent) and
# need periodic refresh; the --image <url|volid> escape hatch avoids hard
# dependence on this table.
IMAGE_CATALOG = {
    "ubuntu-24.04": {
        "url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
        "filename": "noble-server-cloudimg-amd64.qcow2",
        "checksum": None,
        "algo": None,
    },
    "ubuntu-22.04": {
        "url": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
        "filename": "jammy-server-cloudimg-amd64.qcow2",
        "checksum": None,
        "algo": None,
    },
    "debian-12": {
        "url": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
        "filename": "debian-12-genericcloud-amd64.qcow2",
        "checksum": None,
        "algo": None,
    },
}


def resolve_image(image: str) -> dict:
    """Resolve a ``--image`` argument to a fetch spec.

    Returns either ``{"kind": "url", "url", "filename", "checksum", "algo"}``
    (a catalog short-name or an explicit https URL) or ``{"kind": "volid",
    "volid"}`` (an image already present on a storage).
    """
    if image in IMAGE_CATALOG:
        e = IMAGE_CATALOG[image]
        return {"kind": "url", "url": e["url"], "filename": e["filename"], "checksum": e["checksum"], "algo": e["algo"]}
    if image.startswith("http://") or image.startswith("https://"):
        return {"kind": "url", "url": image, "filename": image.rsplit("/", 1)[-1], "checksum": None, "algo": None}
    return {"kind": "volid", "volid": image}
