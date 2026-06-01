"""Confirmation gating for destructive operations.

The guiding rule (so an AI driving this CLI cannot accidentally destroy things):
a destructive operation requires an explicit ``--yes``. When running
non-interactively (no TTY, e.g. an automated agent) and ``--yes`` was not
passed, the operation is refused rather than silently prompted.
"""

from __future__ import annotations

import sys
from typing import Callable, Optional

# Operations that change or destroy state and therefore require confirmation.
DESTRUCTIVE_OPS = frozenset(
    {
        "delete",
        "stop",  # hard power-off; a running guest can lose data
        "reset",  # hard reset
        "migrate",
        "rollback",
    }
)


class ConfirmationRequired(Exception):
    """Raised when a destructive op is attempted without confirmation in a non-interactive context."""


class DangerousNotEnabled(Exception):
    """Raised when a mutating operation is attempted while pmox is in read-only mode."""


def is_destructive(op: str) -> bool:
    return op in DESTRUCTIVE_OPS


def require_dangerous(enabled: bool) -> None:
    """Gate any state-changing operation behind dangerous mode (the ``--dangerous`` flag).

    This is the outer safety tier: pmox is read-only by default, so an AI can
    explore freely but cannot change anything unless dangerous mode is explicitly on.
    """
    if not enabled:
        raise DangerousNotEnabled(
            "This operation changes cluster state, but pmox is in read-only mode. "
            "Re-run with --dangerous (or set PMOX_DANGEROUS=1) to enable management operations."
        )


def stdin_is_tty() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:  # pragma: no cover - defensive
        return False


def confirm(
    action: str,
    assume_yes: bool = False,
    interactive: Optional[bool] = None,
    prompt_func: Optional[Callable[[str], bool]] = None,
) -> bool:
    """Decide whether a destructive ``action`` may proceed.

    - ``assume_yes`` (the ``--yes`` flag): proceed immediately.
    - Otherwise, if interactive (a TTY): ask the user; proceed only on yes.
    - Otherwise (non-interactive): raise :class:`ConfirmationRequired`.

    ``interactive`` defaults to whether stdin is a TTY. ``prompt_func`` is
    injectable so the prompt can be exercised in tests.
    """
    if assume_yes:
        return True
    if interactive is None:
        interactive = stdin_is_tty()
    if not interactive:
        raise ConfirmationRequired(
            f"Refusing to {action} without confirmation. "
            "Re-run with --yes to proceed (required for destructive operations when non-interactive)."
        )
    if prompt_func is None:  # pragma: no cover - exercised via the interactive path in real use
        import typer

        return bool(typer.confirm(f"Are you sure you want to {action}?"))
    return bool(prompt_func(f"Are you sure you want to {action}?"))
