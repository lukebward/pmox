"""Configuration loading for pmox.

Settings are merged from (lowest to highest priority):
    defaults  <  TOML config file  <  environment variables  <  explicit CLI overrides
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - we require >=3.11
    tomllib = None  # type: ignore[assignment]

DEFAULT_PORT = 8006
DEFAULT_TIMEOUT = 30

ENV_HOST = "PROXMOX_HOST"
ENV_PORT = "PROXMOX_PORT"
ENV_TOKEN_ID = "PROXMOX_TOKEN_ID"
ENV_TOKEN_SECRET = "PROXMOX_TOKEN_SECRET"
ENV_VERIFY_SSL = "PROXMOX_VERIFY_SSL"
ENV_TIMEOUT = "PROXMOX_TIMEOUT"


class ConfigError(Exception):
    """Raised when configuration is invalid or incomplete."""


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    host: Optional[str] = None
    port: int = DEFAULT_PORT
    token_id: Optional[str] = None  # full form: user@realm!tokenname
    token_secret: Optional[str] = None
    verify_ssl: bool = False  # default OFF: homelab Proxmox uses self-signed certs
    timeout: int = DEFAULT_TIMEOUT

    @property
    def user(self) -> Optional[str]:
        """The ``user@realm`` portion of the token id."""
        if not self.token_id:
            return None
        return self.token_id.split("!", 1)[0]

    @property
    def token_name(self) -> Optional[str]:
        """The token-name portion of the token id (after ``!``)."""
        if not self.token_id or "!" not in self.token_id:
            return None
        return self.token_id.split("!", 1)[1]

    def validate(self) -> "Settings":
        """Ensure the settings are complete enough to connect. Raises ConfigError otherwise."""
        missing = []
        if not self.host:
            missing.append(ENV_HOST)
        if not self.token_id:
            missing.append(ENV_TOKEN_ID)
        if not self.token_secret:
            missing.append(ENV_TOKEN_SECRET)
        if missing:
            raise ConfigError(
                "Missing required configuration: "
                + ", ".join(missing)
                + ". Provide them via environment variables, a TOML config file, or CLI flags. "
                + "See .env.example."
            )
        if "!" not in (self.token_id or ""):
            raise ConfigError(
                f"token_id must look like 'user@realm!tokenname' (got {self.token_id!r})."
            )
        return self


def default_config_path() -> Path:
    override = os.environ.get("PMOX_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".config" / "pmox" / "config.toml"


def _load_config_file(path: Path) -> dict:
    if not path.exists():
        return {}
    if tomllib is None:  # pragma: no cover
        raise ConfigError("tomllib is unavailable on this Python; cannot read a config file.")
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    # Accept either top-level keys or a [proxmox] table.
    if isinstance(data.get("proxmox"), dict):
        return data["proxmox"]
    return data


def _coerce(source: dict, *, keys: dict) -> dict:
    """Pull recognised keys out of ``source`` applying the given coercion callables."""
    out: dict = {}
    for dest, (src_key, coerce) in keys.items():
        if src_key in source and source[src_key] not in (None, ""):
            out[dest] = coerce(source[src_key])
    return out


def _from_file(data: dict) -> dict:
    return _coerce(
        data,
        keys={
            "host": ("host", str),
            "port": ("port", int),
            "token_id": ("token_id", str),
            "token_secret": ("token_secret", str),
            "verify_ssl": ("verify_ssl", _parse_bool),
            "timeout": ("timeout", int),
        },
    )


def _from_env(env: dict) -> dict:
    out = _coerce(
        env,
        keys={
            "host": (ENV_HOST, str),
            "port": (ENV_PORT, int),
            "token_id": (ENV_TOKEN_ID, str),
            "token_secret": (ENV_TOKEN_SECRET, str),
            "timeout": (ENV_TIMEOUT, int),
        },
    )
    # verify_ssl is a special case: an explicit "false" must be honoured.
    if ENV_VERIFY_SSL in env and env[ENV_VERIFY_SSL] != "":
        out["verify_ssl"] = _parse_bool(env[ENV_VERIFY_SSL])
    return out


def load_settings(
    env: Optional[dict] = None,
    config_path: Optional[Path] = None,
    overrides: Optional[dict] = None,
) -> Settings:
    """Build :class:`Settings` by merging config file, environment, and overrides.

    Does NOT validate; call :meth:`Settings.validate` before connecting.
    """
    env = dict(os.environ if env is None else env)
    if config_path is None:
        config_path = default_config_path()

    merged: dict = {}
    merged.update(_from_file(_load_config_file(config_path)))
    merged.update(_from_env(env))
    if overrides:
        merged.update({k: v for k, v in overrides.items() if v is not None})

    return Settings(**merged)
