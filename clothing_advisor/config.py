"""Environment-driven configuration. Runtime-editable settings live in the database."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _get(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


@dataclass(frozen=True)
class Config:
    data_dir: Path
    access_token: str
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

    @classmethod
    def from_env(cls) -> "Config":
        token = _get("CA_ACCESS_TOKEN")
        if len(token) < 16:
            raise RuntimeError("CA_ACCESS_TOKEN must be set to a random string of at least 16 characters")
        pcts = tuple(sorted(int(p) for p in _get("BUDGET_ALERT_PCTS", "80,100").split(",") if p.strip()))
        return cls(
            data_dir=Path(_get("DATA_DIR", "./data")),
            access_token=token,
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
