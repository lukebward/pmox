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
