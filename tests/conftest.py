from unittest.mock import MagicMock

import pytest

import pmox.cli as cli
from pmox.client import ProxmoxClient


@pytest.fixture
def api():
    """A MagicMock standing in for proxmoxer.ProxmoxAPI's fluent interface."""
    return MagicMock()


@pytest.fixture
def client(api):
    return ProxmoxClient(api)


@pytest.fixture
def creds():
    """Valid-looking credentials so Settings.validate() passes in CLI tests."""
    return {
        "PROXMOX_HOST": "pve.local",
        "PROXMOX_TOKEN_ID": "root@pam!test",
        "PROXMOX_TOKEN_SECRET": "secret",
    }


@pytest.fixture
def fake_client(monkeypatch):
    """Replace the real client factory with a MagicMock so CLI tests never connect."""
    fc = MagicMock()
    monkeypatch.setattr(cli, "_client_factory", lambda settings: fc)
    return fc
