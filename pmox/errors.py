"""Typed errors that carry structured fields for the JSON error envelope.

``PmoxError.extra`` is merged into the ``{"ok": false, ...}`` error envelope by
the CLI's error boundary, so machine-relevant facts (``upid``, ``node``,
``vmid``, ``hint``) reach an agent as fields instead of being buried in prose.
"""

from __future__ import annotations

from typing import Optional


class PmoxError(Exception):
    """A CLI error with structured envelope fields in :attr:`extra`."""

    def __init__(self, message: str, *, extra: Optional[dict] = None):
        super().__init__(message)
        self.extra: dict = dict(extra or {})


class TaskTimeout(PmoxError, TimeoutError):
    """A Proxmox task did not finish within --timeout (it may still be running)."""


class TaskFailed(PmoxError, RuntimeError):
    """A Proxmox task finished with a non-OK exit status."""


class PlanError(PmoxError, RuntimeError):
    """A provisioning plan failed partway; ``extra`` says what completed and how to recover."""
