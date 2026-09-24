"""Environment-driven configuration. Runtime-editable settings live in the database."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


def _get(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _secure_or_local(url: str) -> bool:
    """https, or plain http only to this machine (local testing): a sign-in must never travel in the clear."""
    parsed = urlparse(url)
    return parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"))


def _oidc_settings() -> dict[str, object]:
    issuer = _get("OIDC_ISSUER")
    if not issuer:
        return {}
    client_id, secret = _get("OIDC_CLIENT_ID"), _get("OIDC_CLIENT_SECRET")
    uris = tuple(u for u in _get("OIDC_REDIRECT_URIS").replace(",", " ").split() if u)
    internal = _get("OIDC_INTERNAL_URL").rstrip("/")
    missing = [n for n, v in (("OIDC_CLIENT_ID", client_id), ("OIDC_CLIENT_SECRET", secret), ("OIDC_REDIRECT_URIS", uris)) if not v]
    if missing:
        raise RuntimeError(f"OIDC_ISSUER is set, so these are required too: {', '.join(missing)}")
    if not _secure_or_local(issuer):
        raise RuntimeError("OIDC_ISSUER must be an https URL (http is only accepted for localhost)")
    for uri in uris:
        if not _secure_or_local(uri) or not uri.endswith("/app/oidc/callback"):
            raise RuntimeError(f"OIDC_REDIRECT_URIS entry {uri!r} must be an https URL ending in /app/oidc/callback")
    if internal and urlparse(internal).scheme not in ("http", "https"):
        raise RuntimeError("OIDC_INTERNAL_URL must be an http(s) URL")
    days = int(_get("OIDC_SESSION_DAYS", "7"))
    if not 1 <= days <= 90:
        raise RuntimeError("OIDC_SESSION_DAYS must be between 1 and 90")
    return {
        "oidc_issuer": issuer, "oidc_internal_url": internal, "oidc_client_id": client_id, "oidc_client_secret": secret,
        "oidc_redirect_uris": uris, "oidc_admin_group": _get("OIDC_ADMIN_GROUP", "clothing-advisor-admin"),
        "oidc_viewer_group": _get("OIDC_VIEWER_GROUP", "clothing-advisor-viewer"), "oidc_session_days": days,
    }


@dataclass(frozen=True)
class Config:
    data_dir: Path
    access_token: str
    tile_token: str  # optional, opens only the tile (see web.TILE_ROUTES)
    view_token: str  # optional, read-only: shows the suggestions, can change nothing (see web.VIEW_ROUTES)
    public_base_url: str

    catalog_model: str
    stylist_model: str
    stylist_effort: str
    outfit_count: int
    repeat_days: int
    tz: ZoneInfo

    monthly_budget_eur: float
    budget_mode: str  # "enforce" blocks Claude calls at 100%, "warn" only notifies
    usd_eur: float
    alert_pcts: tuple[int, ...]

    ha_url: str
    ha_token: str
    ha_weather_entity: str
    ha_notify_service: str
    ha_origin: str  # optional, allows framing by Home Assistant

    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_to: str
    smtp_security: str  # starttls | ssl | none

    # Optional single sign-on through Authentik (see oidc.py); empty issuer = off, only the tokens work.
    oidc_issuer: str = ""
    oidc_internal_url: str = ""  # how this container reaches Authentik when the public name is not resolvable from here
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uris: tuple[str, ...] = ()
    oidc_admin_group: str = "clothing-advisor-admin"
    oidc_viewer_group: str = "clothing-advisor-viewer"
    oidc_session_days: int = 7

    @classmethod
    def from_env(cls) -> "Config":
        token = _get("CA_ACCESS_TOKEN")
        if len(token) < 16:
            raise RuntimeError("CA_ACCESS_TOKEN must be set to a random string of at least 16 characters")
        tile, view = _get("CA_TILE_TOKEN"), _get("CA_VIEW_TOKEN")
        for name, value in (("CA_TILE_TOKEN", tile), ("CA_VIEW_TOKEN", view)):
            if value and len(value) < 16:
                raise RuntimeError(f"{name} must be at least 16 characters")
        chosen = [t for t in (token, tile, view) if t]
        if len(set(chosen)) != len(chosen):
            raise RuntimeError("CA_ACCESS_TOKEN, CA_TILE_TOKEN and CA_VIEW_TOKEN must all be different")
        pcts = tuple(sorted(int(p) for p in _get("BUDGET_ALERT_PCTS", "80,100").split(",") if p.strip()))
        oidc = _oidc_settings()
        return cls(
            data_dir=Path(_get("DATA_DIR", "./data")),
            access_token=token,
            tile_token=tile,
            view_token=view,
            public_base_url=_get("PUBLIC_BASE_URL").rstrip("/"),
            catalog_model=_get("CATALOG_MODEL", "claude-sonnet-5"),
            stylist_model=_get("STYLIST_MODEL", "claude-sonnet-5"),
            stylist_effort=_get("STYLIST_EFFORT", "high"),
            outfit_count=int(_get("OUTFIT_COUNT", "3")),
            repeat_days=int(_get("REPEAT_DAYS", "7")),
            tz=ZoneInfo(_get("TZ", "Europe/Amsterdam")),
            monthly_budget_eur=float(_get("MONTHLY_BUDGET_EUR", "10")),
            budget_mode=_get("BUDGET_MODE", "enforce").lower(),
            usd_eur=float(_get("USD_EUR_RATE", "0.92")),
            alert_pcts=pcts,
            ha_url=_get("HA_URL").rstrip("/"),
            ha_token=_get("HA_TOKEN"),
            ha_weather_entity=_get("HA_WEATHER_ENTITY"),
            ha_notify_service=_get("HA_NOTIFY_SERVICE"),
            ha_origin=_get("HA_ORIGIN"),
            smtp_host=_get("SMTP_HOST"),
            smtp_port=int(_get("SMTP_PORT", "587")),
            smtp_user=_get("SMTP_USER"),
            smtp_password=_get("SMTP_PASSWORD"),
            smtp_from=_get("SMTP_FROM"),
            smtp_to=_get("SMTP_TO"),
            smtp_security=_get("SMTP_SECURITY", "starttls").lower(),
            **oidc,
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clothing.db"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def collages_dir(self) -> Path:
        return self.data_dir / "collages"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.photos_dir, self.thumbs_dir, self.collages_dir, self.inbox_dir):
            d.mkdir(parents=True, exist_ok=True)
