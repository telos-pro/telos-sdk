"""``~/.telos/config.json`` —— telos global user config.

A lightweight JSON config file that records:

- ``mode``                 default optimization mode (none/telos/rtk/both)
- ``gateway``              gateway listen host / port / usage_log
- ``favorite_harness``     the harness the bare ``telos`` command enters by default
- ``harness_executables``  harness name → custom executable name (overrides the
  default guess)

Design principles:
- Missing file → all defaults, no error (zero-config first use).
- Bad JSON → raise ``RuntimeError`` with a fix hint (do not silently swallow user data).
- Unknown keys are preserved round-trip (forward compatibility: an old telos
  does not drop fields written by a newer version).
- Atomic write (``.tmp`` + ``os.replace``), same as ``init/claude_code._atomic_write``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1

DEFAULT_GATEWAY_HOST = "127.0.0.1"
DEFAULT_GATEWAY_PORT = 7171
DEFAULT_MODE = "telos"


def telos_home() -> Path:
    """The ``~/.telos`` directory (can be overridden by the ``TELOS_HOME`` environment variable, handy for testing)."""
    env = os.environ.get("TELOS_HOME")
    return Path(env) if env else Path.home() / ".telos"


def config_path() -> Path:
    return telos_home() / "config.json"


def default_usage_log() -> Path:
    return telos_home() / "usage.jsonl"


@dataclass
class GatewayConfig:
    host: str = DEFAULT_GATEWAY_HOST
    port: int = DEFAULT_GATEWAY_PORT
    usage_log: str = ""  # empty string → use default_usage_log()

    def resolved_usage_log(self) -> Path:
        return Path(self.usage_log).expanduser() if self.usage_log else default_usage_log()

    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass
class TelosConfig:
    """telos global config. ``_extra`` preserves unknown keys so they are not lost on write-back."""

    mode: str = DEFAULT_MODE
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    favorite_harness: str | None = None
    harness_executables: dict[str, str] = field(default_factory=dict)
    _extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = dict(self._extra)
        data["_schema"] = _SCHEMA_VERSION
        data["mode"] = self.mode
        data["gateway"] = {
            "host": self.gateway.host,
            "port": self.gateway.port,
            "usage_log": self.gateway.usage_log,
        }
        data["favorite_harness"] = self.favorite_harness
        data["harness_executables"] = dict(self.harness_executables)
        return data


_KNOWN_KEYS = {"_schema", "mode", "gateway", "favorite_harness", "harness_executables"}


def load_config() -> TelosConfig:
    """Read ``~/.telos/config.json``; returns all defaults if missing."""
    path = config_path()
    if not path.exists():
        return TelosConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"{path} is not valid JSON ({e}). Please fix it manually, or delete the file and let telos regenerate it."
        ) from e
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} top level must be a JSON object, but is actually {type(data).__name__}")

    gw_raw = data.get("gateway") or {}
    if not isinstance(gw_raw, dict):
        gw_raw = {}
    gateway = GatewayConfig(
        host=str(gw_raw.get("host", DEFAULT_GATEWAY_HOST)),
        port=int(gw_raw.get("port", DEFAULT_GATEWAY_PORT)),
        usage_log=str(gw_raw.get("usage_log", "") or ""),
    )
    execs_raw = data.get("harness_executables") or {}
    harness_executables = (
        {str(k): str(v) for k, v in execs_raw.items()}
        if isinstance(execs_raw, dict) else {}
    )
    fav = data.get("favorite_harness")
    extra = {k: v for k, v in data.items() if k not in _KNOWN_KEYS}
    return TelosConfig(
        mode=str(data.get("mode", DEFAULT_MODE)),
        gateway=gateway,
        favorite_harness=str(fav) if fav else None,
        harness_executables=harness_executables,
        _extra=extra,
    )


def save_config(cfg: TelosConfig) -> Path:
    """Atomically write back ``~/.telos/config.json``."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


def update_config(**fields: Any) -> TelosConfig:
    """A convenient wrapper for load → modify fields → save.

    Supported fields: ``mode`` / ``favorite_harness``; for ``gateway`` subfields use
    ``gateway_host`` / ``gateway_port`` / ``gateway_usage_log``.
    """
    cfg = load_config()
    if "mode" in fields:
        cfg.mode = str(fields["mode"])
    if "favorite_harness" in fields:
        fav = fields["favorite_harness"]
        cfg.favorite_harness = str(fav) if fav else None
    if "gateway_host" in fields:
        cfg.gateway.host = str(fields["gateway_host"])
    if "gateway_port" in fields:
        cfg.gateway.port = int(fields["gateway_port"])
    if "gateway_usage_log" in fields:
        cfg.gateway.usage_log = str(fields["gateway_usage_log"] or "")
    save_config(cfg)
    return cfg
