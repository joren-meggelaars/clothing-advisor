"""Weather context from a Home Assistant weather entity (current state + daily forecast)."""
from __future__ import annotations

import logging
import time
from typing import Any

from .config import Config
from .db import Database
from .ha import HaError, ha_configured, ha_request

log = logging.getLogger(__name__)
CACHE_SECONDS = 600


def _c(value: Any, unit: str) -> float | None:
    if value is None:
        return None
    v = float(value)
    return round((v - 32) * 5 / 9, 1) if "F" in unit.upper() else round(v, 1)


class Weather:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self._cache: tuple[float, str, dict[str, Any]] | None = None
        self.last_error = ""

    def entity(self) -> str:
        return self.db.get_setting("weather_entity") or self.cfg.ha_weather_entity

    def get(self, refresh: bool = False) -> dict[str, Any] | None:
        entity = self.entity()
        if not entity or not ha_configured(self.cfg):
            return None
        if not refresh and self._cache and self._cache[1] == entity and time.time() - self._cache[0] < CACHE_SECONDS:
            return self._cache[2]
        try:
            data = self._fetch(entity)
        except (HaError, ValueError, KeyError, TypeError) as e:
            self.last_error = str(e)
            log.warning("weather fetch failed: %s", e)
            return None
        self.last_error = ""
        self._cache = (time.time(), entity, data)
        return data

    def _fetch(self, entity: str) -> dict[str, Any]:
        state = ha_request(self.cfg, "GET", f"/api/states/{entity}")
        attrs = state.get("attributes", {})
        unit = attrs.get("temperature_unit", "°C")
        out: dict[str, Any] = {
            "condition": state.get("state", ""),
            "t_now": _c(attrs.get("temperature"), unit),
            "wind": attrs.get("wind_speed"),
            "wind_unit": attrs.get("wind_speed_unit", ""),
            "humidity": attrs.get("humidity"),
            "t_high": None, "t_low": None, "rain_prob": None,
            "tomorrow": None,
        }
        try:
            resp = ha_request(self.cfg, "POST", "/api/services/weather/get_forecasts?return_response",
                              {"entity_id": entity, "type": "daily"})
            forecast = (resp.get("service_response", resp).get(entity) or {}).get("forecast") or []
        except HaError as e:
            log.info("no daily forecast available: %s", e)
            forecast = []
        if forecast:
            today = forecast[0]
            out["t_high"] = _c(today.get("temperature"), unit)
            out["t_low"] = _c(today.get("templow"), unit)
            out["rain_prob"] = today.get("precipitation_probability")
            if len(forecast) > 1:
                tm = forecast[1]
                out["tomorrow"] = {
                    "condition": tm.get("condition", ""),
                    "t_high": _c(tm.get("temperature"), unit),
                    "t_low": _c(tm.get("templow"), unit),
                    "rain_prob": tm.get("precipitation_probability"),
                }
        if out["t_high"] is None:
            out["t_high"] = out["t_now"]  # no forecast: treat the current temperature as the day's high
        out["text"] = self._describe(out)
        return out

    ICONS = {"sunny": "☀️", "clear-night": "🌙", "partlycloudy": "⛅", "cloudy": "☁️", "rainy": "🌧️", "pouring": "🌧️",
             "snowy": "❄️", "snowy-rainy": "🌨️", "hail": "🌨️", "fog": "🌫️", "windy": "💨", "windy-variant": "💨",
             "lightning": "⛈️", "lightning-rainy": "⛈️", "exceptional": "⚠️"}

    @classmethod
    def short(cls, w: dict[str, Any] | None) -> str:
        """Compact line for small screens, e.g. '⛅ 14° · 9-18° · 40% rain'."""
        if not w:
            return ""
        parts = []
        if w.get("t_now") is not None:
            parts.append(f"{cls.ICONS.get(w.get('condition', ''), '')} {w['t_now']:.0f}°".strip())
        if w.get("t_low") is not None and w.get("t_high") is not None:
            parts.append(f"{w['t_low']:.0f}-{w['t_high']:.0f}°")
        if w.get("rain_prob") is not None and w["rain_prob"] >= 20:
            parts.append(f"{w['rain_prob']}% rain")
        return " · ".join(parts)

    @staticmethod
    def _describe(w: dict[str, Any]) -> str:
        parts = []
        if w["t_now"] is not None:
            now = f"Now {w['t_now']:g}°C, {w['condition']}"
            if w.get("wind") is not None:
                now += f", wind {w['wind']} {w['wind_unit']}".rstrip()
            parts.append(now)
        if w["t_high"] is not None and w["t_low"] is not None:
            day = f"Today high {w['t_high']:g}°C / low {w['t_low']:g}°C"
            if w["rain_prob"] is not None:
                day += f", {w['rain_prob']}% chance of precipitation"
            parts.append(day)
        tm = w.get("tomorrow")
        if tm and tm["t_high"] is not None:
            t = f"Tomorrow high {tm['t_high']:g}°C"
            if tm["t_low"] is not None:
                t += f" / low {tm['t_low']:g}°C"
            t += f", {tm['condition']}"
            if tm["rain_prob"] is not None:
                t += f", {tm['rain_prob']}% precipitation"
            parts.append(t)
        return ". ".join(parts)
