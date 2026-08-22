"""Subprocess-level checks that parse errors emit the documented usage envelope.

These run ``python -m pmox`` as a real child process so the actual
import-time click resolution is exercised — the regression this guards
against (typer's vendored click vs the real package) is invisible to
in-process tests that import cli.py under a specific environment.
"""

import json
import os
import subprocess
import sys

import pytest


def _run(*args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PMOX_", "PROXMOX_"))}
    env["PMOX_JSON"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pmox", *args],
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.parametrize(
    "argv",
    [
        ("nonexistent-cmd",),
        ("vm", "list", "--bogus"),
        ("--dangerous", "--yes", "vm", "delete", "100"),
        ("vm", "status"),
    ],
    ids=["unknown-command", "unknown-option", "misplaced-yes", "missing-argument"],
)
def test_parse_error_emits_usage_envelope(argv):
    proc = _run(*argv)
    assert proc.returncode == 2, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert payload["error"] == "usage"
    assert "hint" in payload
