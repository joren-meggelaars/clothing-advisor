"""Wires the services together once at startup."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .catalog import CatalogWorker
from .config import Config
from .db import Database
from .llm import LlmClient
from .notify import Notifier
from .stylist import DEFAULT_STYLE_PROFILE, Stylist
from .taste import Taste
from .usage import CostTracker
from .weather import Weather


@dataclass
class Ctx:
    cfg: Config
    db: Database
    notifier: Notifier
    tracker: CostTracker
    llm: LlmClient
    weather: Weather
    stylist: Stylist
    worker: CatalogWorker
    taste: Taste


def build_ctx(cfg: Config, client: Any | None = None) -> Ctx:
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    if not db.setting_exists("style_profile"):
        db.set_setting("style_profile", DEFAULT_STYLE_PROFILE)
    notifier = Notifier(cfg, db)
    tracker = CostTracker(cfg, db, notifier)
    llm = LlmClient(tracker, client)
    weather = Weather(cfg, db)
    return Ctx(cfg, db, notifier, tracker, llm, weather, Stylist(cfg, db, llm, weather), CatalogWorker(cfg, db, llm),
               Taste(cfg, db, llm))
