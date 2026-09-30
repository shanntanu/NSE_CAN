from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

ENV_PREFIX = "NSE_ALERTS__"
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


class Config:
    """Read-only view over the YAML settings with dotted lookups."""

    def __init__(self, data: dict[str, Any]):
        self.data = data

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def section(self, dotted: str) -> "Config":
        value = self.get(dotted, {})
        return Config(value if isinstance(value, dict) else {})

    def secret(self, name: str, default: str = "") -> str:
        return os.environ.get(name, default).strip()


def _apply_env_overrides(data: dict[str, Any]) -> None:
    for key, raw in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        parts = [p.lower() for p in key[len(ENV_PREFIX):].split("__") if p]
        if not parts:
            continue
        cur = data
        for part in parts[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[part] = nxt
            cur = nxt
        cur[parts[-1]] = yaml.safe_load(raw)


def load_config(path: Path | str | None = None) -> Config:
    path = Path(path or os.environ.get("NSE_ALERTS_CONFIG") or DEFAULT_PATH)
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    _apply_env_overrides(data)
    return Config(data)


class ConfigStore:
    """Hands out the current config and reloads it when the file changes."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or os.environ.get("NSE_ALERTS_CONFIG") or DEFAULT_PATH)
        self._mtime = self.path.stat().st_mtime
        self._cfg = load_config(self.path)

    @property
    def cfg(self) -> Config:
        try:
            mtime = self.path.stat().st_mtime
            if mtime != self._mtime:
                self._cfg = load_config(self.path)
                self._mtime = mtime
                log.info("config reloaded from %s", self.path)
        except Exception:
            log.exception("config reload failed, keeping the previous settings")
            self._mtime = self.path.stat().st_mtime if self.path.exists() else self._mtime
        return self._cfg
