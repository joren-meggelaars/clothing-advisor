"""Minimal Home Assistant REST client (stdlib only)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import Config


class HaError(Exception):
    pass


def ha_configured(cfg: Config) -> bool:
    return bool(cfg.ha_url and cfg.ha_token)


def ha_request(cfg: Config, method: str, path: str, body: Any = None, timeout: float = 10) -> Any:
    if not ha_configured(cfg):
        raise HaError("HA_URL / HA_TOKEN are not configured")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        cfg.ha_url + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {cfg.ha_token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise HaError(f"Home Assistant returned HTTP {e.code} for {path}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HaError(f"Cannot reach Home Assistant: {e}") from e
    return json.loads(raw) if raw else None
