"""Learning from ratings: recent feedback for the prompt, a distilled taste profile, and outfits to never repeat."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel

from .config import Config
from .db import Database
from .llm import LlmClient, LlmError
from .usage import BudgetExceeded

log = logging.getLogger(__name__)

RATING_REASONS = ("colours", "too warm", "too cold", "too formal", "too casual", "not my style", "uncomfortable")
DISTILL_EVERY = 10          # new ratings before the taste profile is refreshed
RECENT_IN_PROMPT = 15       # rated outfits sent verbatim with every advice request
RECENT_FOR_DISTILL = 40
DISLIKED_STARS = 2          # outfits rated this low or lower are never proposed again ...
DISLIKED_DAYS = 90          # ... for this many days

DISTILL_SYSTEM = """You maintain a short taste profile for one man's outfit advisor, based on how he rated outfits.

You get the current profile (it may contain lines he wrote or edited himself: keep them unless several recent ratings clearly contradict them), his fixed style profile (do not restate it) and recent rated outfits, most recent first. 'worn' ratings are real experience and count more than 'suggestion' ratings, which are first impressions. Comments may be in Dutch or English.

Return the updated profile: at most 10 lines, each starting with "- ", each concrete and useful for choosing outfits (colour combinations he likes or dislikes, specific pieces such as "the grey sweater", warmth, formality or fit remarks). Do not invent preferences without support in at least one rating or comment; mark weak signals with "(tentative)". Drop lines that newer ratings contradict. Write in English."""


class TasteOut(BaseModel):
    profile: str


def _describe(items: dict[int, dict[str, Any]], item_ids: list[int]) -> str:
    parts = []
    for i in item_ids:
        it = items.get(i)
        if it:
            parts.append(f"#{i} {' '.join(it['colors'][:1])} {it['subtype']}".replace("  ", " "))
    return " + ".join(parts) or "(items no longer in the wardrobe)"


def format_ratings(db: Database, rows: list[dict[str, Any]]) -> str:
    """One line per rated outfit, newest first, for the prompt."""
    ids = sorted({i for r in rows for i in r["item_ids"]})
    items = db.items_by_ids(ids)
    lines = []
    for r in rows:
        extra = []
        if r["reasons"]:
            extra.append("reasons: " + ", ".join(r["reasons"]))
        if r["comment"]:
            extra.append(f"comment: \"{r['comment'][:300]}\"")
        lines.append(f"- [{r['context']}, {r['stars']}/5{'; ' + '; '.join(extra) if extra else ''}] "
                     f"\"{r['outfit_name']}\": {_describe(items, r['item_ids'])}")
    return "\n".join(lines)


def feedback_text(db: Database, limit: int = RECENT_IN_PROMPT) -> str:
    rows = db.recent_ratings(limit)
    return format_ratings(db, rows) if rows else ""


def disliked_sets(db: Database, now: datetime | None = None) -> list[frozenset[int]]:
    """Outfit combinations he rated poorly recently: never propose them again."""
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=DISLIKED_DAYS)).isoformat()
    return [frozenset(r["item_ids"]) for r in db.recent_ratings(500)
            if r["stars"] <= DISLIKED_STARS and r["created_at"] >= cutoff]


class Taste:
    def __init__(self, cfg: Config, db: Database, llm: LlmClient):
        self.cfg, self.db, self.llm = cfg, db, llm
        self._lock = threading.Lock()

    def profile(self) -> str:
        return self.db.get_setting("taste_profile").strip()

    def _watermark(self) -> int:
        raw = self.db.get_setting("taste_watermark")
        return int(raw) if raw.isdigit() else 0

    def status(self) -> dict[str, Any]:
        new = len(self.db.ratings_after(self._watermark()))
        return {"total": self.db.rating_count(), "new": new, "next_in": max(DISTILL_EVERY - new, 0),
                "updated": self.db.get_setting("taste_updated"), "recent": self.db.recent_ratings(10)}

    def reset(self) -> None:
        self.db.set_setting("taste_profile", "")
        self.db.set_setting("taste_watermark", str(self.db.max_rating_id()))
        self.db.set_setting("taste_updated", "")

    def distill(self, force: bool = False) -> bool:
        """Refresh the profile from new ratings. Returns True when it was updated."""
        with self._lock:
            new = self.db.ratings_after(self._watermark())
            if not new or (not force and len(new) < DISTILL_EVERY):
                return False
            from .stylist import style_profile  # local import: stylist imports this module
            recent = self.db.recent_ratings(RECENT_FOR_DISTILL)
            content = (f"Current taste profile:\n{self.profile() or '(empty)'}\n\n"
                       f"Fixed style profile (context only):\n{style_profile(self.db)}\n\n"
                       f"Rated outfits, most recent first:\n{format_ratings(self.db, recent)}")
            out = self.llm.structured(
                purpose="taste", model=self.cfg.stylist_model, system=DISTILL_SYSTEM,
                messages=[{"role": "user", "content": content}], out_model=TasteOut,
                max_tokens=4000, thinking="adaptive", effort="medium",
            )
            self.db.set_setting("taste_profile", out.profile.strip()[:2000])
            self.db.set_setting("taste_watermark", str(max(r["id"] for r in new)))
            self.db.set_setting("taste_updated", datetime.now(timezone.utc).isoformat(timespec="seconds"))
            return True

    def maybe_distill(self) -> None:
        """Background hook after a rating. Failures are logged; the next rating retries."""
        try:
            self.distill()
        except (LlmError, BudgetExceeded) as e:
            log.warning("taste profile update skipped: %s", e)
        except Exception:
            log.exception("taste profile update failed")
