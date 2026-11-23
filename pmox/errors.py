"""Typed errors that carry structured fields for the JSON error envelope.

``PmoxError.extra`` is merged into the ``{"ok": false, ...}`` error envelope by
the CLI's error boundary, so machine-relevant facts (``upid``, ``node``,
``vmid``, ``hint``) reach an agent as fields instead of being buried in prose.
"""

from __future__ import annotations

import re
from typing import Optional

_TASK_WARNINGS = re.compile(r"WARNINGS: (\d+)")


def task_warning_count(exitstatus) -> Optional[int]:
    """Classify a finished task's ``exitstatus``: ``None`` means it failed.

    Proxmox reports success as ``OK`` (0 warnings) or as ``WARNINGS: <n>`` when the
    task completed but logged warnings (e.g. "Systemd 255 detected. You may need to
    enable nesting." on container create). Anything else is a real failure.
    """
    text = str(exitstatus)
    if text == "OK":
        return 0
    match = _TASK_WARNINGS.fullmatch(text)
    return int(match.group(1)) if match else None


class PmoxError(Exception):
    """A CLI error with structured envelope fields in :attr:`extra`."""

    def __init__(self, message: str, *, extra: Optional[dict] = None):
        super().__init__(message)
        self.extra: dict = dict(extra or {})


class TaskTimeout(PmoxError, TimeoutError):
    """A Proxmox task did not finish within --timeout (it may still be running)."""


class TaskFailed(PmoxError, RuntimeError):
    """A Proxmox task finished with a failing exit status (not ``OK`` / ``WARNINGS: <n>``)."""


class PlanError(PmoxError, RuntimeError):
    """A provisioning plan failed partway; ``extra`` says what completed and how to recover."""


class NotFoundError(PmoxError, LookupError):
    """A guest/node/storage/task lookup found nothing. Not worth retrying —
    re-list (``pmox vm list`` / ``ct list`` / ``task list``) instead."""
