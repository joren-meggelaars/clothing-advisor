"""Cost ledger, monthly budget and threshold alerts."""
from __future__ import annotations

import calendar
import logging
from datetime import datetime
from typing import Any

from .config import Config
from .db import Database
from .notify import Notifier

log = logging.getLogger(__name__)

# USD per million tokens (input, output). Anthropic price list as cached 2026-06-24; verify occasionally.
PRICES_USD: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-fable-5-1": (10.0, 50.0),
}
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25  # 5-minute cache


class BudgetExceeded(Exception):
    pass


def cost_usd(model: str, in_tok: int, out_tok: int, cache_read: int = 0, cache_write: int = 0) -> float:
    # Unknown model: assume the most expensive known one so the ledger never under-reports.
    p_in, p_out = PRICES_USD.get(model, PRICES_USD["claude-fable-5-1"])
    return (in_tok * p_in + out_tok * p_out
            + cache_read * p_in * CACHE_READ_FACTOR + cache_write * p_in * CACHE_WRITE_FACTOR) / 1_000_000


class CostTracker:
    def __init__(self, cfg: Config, db: Database, notifier: Notifier):
        self.cfg = cfg
        self.db = db
        self.notifier = notifier

    def _now(self) -> datetime:
        return datetime.now(self.cfg.tz)

    def ym(self) -> str:
        return self._now().strftime("%Y-%m")

    def budget(self) -> float:
        raw = self.db.get_setting("budget_eur")
        return float(raw) if raw else self.cfg.monthly_budget_eur

    def spend(self) -> float:
        return self.db.month_spend(self.ym())

    def pct(self) -> float:
        b = self.budget()
        return self.spend() / b * 100 if b > 0 else 0.0

    def check(self) -> None:
        """Raise before a Claude call when the budget is used up (enforce mode)."""
        if self.cfg.budget_mode == "enforce" and self.spend() >= self.budget():
            raise BudgetExceeded(
                f"Monthly Claude budget of EUR {self.budget():.2f} is used up "
                f"(EUR {self.spend():.2f}). Raise it on the Costs page to continue."
            )

    def record(self, purpose: str, model: str, in_tok: int, out_tok: int,
               cache_read: int = 0, cache_write: int = 0) -> float:
        eur = cost_usd(model, in_tok, out_tok, cache_read, cache_write) * self.cfg.usd_eur
        self.db.add_usage(self.ym(), purpose, model, in_tok, out_tok, cache_read, cache_write, eur)
        self._alert_if_needed()
        return eur

    def _alert_if_needed(self) -> None:
        pct = self.pct()
        for thr in self.cfg.alert_pcts:
            key = f"alert:{self.ym()}:{thr}"
            if pct >= thr and not self.db.setting_exists(key):
                title = f"Clothing Advisor: {thr}% of the monthly Claude budget used"
                body = (f"EUR {self.spend():.2f} of EUR {self.budget():.2f} spent this month ({pct:.0f}%)."
                        + (" New requests are now blocked until you raise the budget."
                           if thr >= 100 and self.cfg.budget_mode == "enforce" else ""))
                ok, errors = self.notifier.send(title, body)
                # Mark as sent when delivered, or when no channel exists (the UI banner is then the only signal).
                if ok or not self.notifier.channels():
                    self.db.set_setting(key, "1")
                log.warning("budget alert %s%%: sent=%s errors=%s", thr, ok, errors)

    def fired_alerts(self) -> list[int]:
        return [t for t in self.cfg.alert_pcts if self.db.setting_exists(f"alert:{self.ym()}:{t}")]

    def summary(self) -> dict[str, Any]:
        now = self._now()
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        spend = self.spend()
        return {
            "ym": self.ym(),
            "spend_eur": spend,
            "budget_eur": self.budget(),
            "pct": self.pct(),
            "projected_eur": spend / max(now.day, 1) * days_in_month,
            "mode": self.cfg.budget_mode,
            "alerts_fired": self.fired_alerts(),
            "channels": self.notifier.channels(),
        }
