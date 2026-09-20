"""Morning suggestion: prepare the day's outfits automatically so nobody has to press a button."""
from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta
from typing import Any

from .config import Config
from .db import Database
from .llm import LlmError
from . import stylist as st
from .stylist import Stylist
from .usage import BudgetExceeded, CostTracker

log = logging.getLogger(__name__)

DEFAULT_TIME = "06:30"
DEFAULT_REQUEST = "Suggest my outfit for today."
WINDOW_HOURS = 8        # a missed run is only made up within this window, later it would just waste a call
MAX_ATTEMPTS = 3
RETRY_MINUTES = 15


class AutoAdvice:
    def __init__(self, cfg: Config, db: Database, stylist: Stylist, tracker: CostTracker):
        self.cfg, self.db, self.stylist, self.tracker = cfg, db, stylist, tracker

    # ---- settings (editable in the app) -------------------------------------
    def enabled(self) -> bool:
        return self.db.get_setting("auto_advice", "1") == "1"

    def at(self) -> time:
        raw = self.db.get_setting("auto_advice_time") or DEFAULT_TIME
        try:
            h, m = raw.split(":")
            return time(int(h), int(m))
        except ValueError:
            return time.fromisoformat(DEFAULT_TIME)

    def request(self) -> str:
        return self.db.get_setting("auto_advice_request").strip() or DEFAULT_REQUEST

    # ---- state of today's run -------------------------------------------------
    def _state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.db.get_setting("auto_advice_state") or "{}")
        except ValueError:
            state = {}
        return state if state.get("date") == st.today_date(self.cfg).isoformat() else {"date": st.today_date(self.cfg).isoformat(), "attempts": 0}

    def _save(self, state: dict[str, Any]) -> None:
        self.db.set_setting("auto_advice_state", json.dumps(state))

    def status(self) -> str:
        s = self._state()
        return s.get("status", "not run yet today") + (f" ({s['last']})" if s.get("last") else "")

    # ---- the job ----------------------------------------------------------------
    def _already_handled_today(self) -> bool:
        """You asked for advice yourself today, or already picked an outfit: no need to prepare one."""
        today = st.today_date(self.cfg).isoformat()
        if self.db.get_setting("last_user_advice") == today:
            return True
        return any((o["worn_on"] or o["chosen_on"]) == today
                   for o in self.db.list_outfits(statuses=("chosen", "worn"), since=today))

    def run_if_due(self, now: datetime | None = None) -> str:
        """Called every minute. Returns a short status string (used by tests and logs)."""
        now = now or datetime.now(self.cfg.tz)
        if not self.enabled():
            return "disabled"
        start = datetime.combine(now.date(), self.at(), tzinfo=self.cfg.tz)
        if not start <= now <= start + timedelta(hours=WINDOW_HOURS):
            return "outside window"
        state = self._state()
        if state.get("status") in ("done", "skipped"):
            return state["status"]
        if self._already_handled_today():
            self._save({**state, "status": "skipped", "last": "you already asked today"})
            return "skipped"
        if state["attempts"] >= MAX_ATTEMPTS:
            return "gave up"
        last = state.get("last_try")
        if last and now - datetime.fromisoformat(last) < timedelta(minutes=RETRY_MINUTES):
            return "waiting to retry"
        return self._run(state, now)

    def run_now(self) -> str:
        """Manual trigger (Settings): ignores the clock and previous attempts."""
        return self._run(self._state(), datetime.now(self.cfg.tz), force=True)

    def _run(self, state: dict[str, Any], now: datetime, force: bool = False) -> str:
        try:
            self.tracker.check()
        except BudgetExceeded as e:
            self._save({**state, "status": "paused: budget used up"})
            log.warning("morning suggestion skipped: %s", e)
            return "budget"
        state = {**state, "attempts": state["attempts"] + 1, "last_try": now.isoformat(timespec="seconds")}
        try:
            result = self.stylist.advise(self.request(), new_session=True)
            ok = bool(result["outfit_ids"])
            state.update(status="done" if ok else "no valid outfits, will retry", last=now.strftime("%H:%M"))
            if not ok and force:
                state["status"] = "no valid outfits"
        except ValueError as e:
            state.update(status=f"not possible: {e}"[:160])
        except (LlmError, BudgetExceeded) as e:
            state.update(status=f"failed: {e}"[:160])
        except Exception as e:  # never let the loop die
            log.exception("morning suggestion crashed")
            state.update(status=f"failed: {e}"[:160])
        self._save(state)
        log.info("morning suggestion: %s", state["status"])
        return "done" if state["status"] == "done" else "failed"
