"""Outfit advice: weather/season pre-filter, prompt building, server-side validation, wear & laundry rules."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

from .config import Config
from .db import Database
from .llm import LlmClient
from .schema import AdviceOut
from .taste import disliked_sets, feedback_text
from .weather import Weather

log = logging.getLogger(__name__)

DEFAULT_STYLE_PROFILE = (
    "Primarily casual; sometimes a bit smarter (casual chic / smart casual). Never suits, never a dress shirt "
    "combined with suit trousers, and never classic formal men's dress shoes (oxfords, derbies, brogues). "
    "Prefer relaxed, modern, well-fitting pieces in a coherent palette."
)
SESSION_MAX_AGE = timedelta(hours=12)
DEFAULT_REQUEST = "Suggest my outfit for today, fitting the weather."
REFINE_REQUEST = "Give me different options."
DEFAULT_WORK_DAYS = (0, 1, 2, 3, 4)          # Monday to Friday (date.weekday())
DEFAULT_WORK_DRESS = ("Workday: smart casual / casual chic (formality about 3): a clean polo, fine knit or shirt jacket "
                      "with chinos or dark jeans and clean sneakers or boots. Nothing sporty or loungewear.")
EXTRA_FORMAL_NOTE = ("Extra formal requested: go one notch smarter than usual (formality 3-4 pieces, sharp and well matched). "
                     "The style profile still wins: no suit, no dress shirt with suit trousers, no formal dress shoes.")
HISTORY_TURNS = 6

# Wears before an item goes to the wash. 0 = never automatically.
WASH_AFTER = {("top", "base"): 1, ("top", "mid"): 3, ("top", None): 2, ("bottom", None): 4,
              ("outerwear", None): 20, ("footwear", None): 0, ("accessory", None): 0}

SYSTEM_RULES = """You are the personal stylist of one man. You build complete, well-matched outfits from his wardrobe catalogue and nothing else.

Rules:
- Use only item ids that appear in the catalogue below. Never invent items.
- Composition of one outfit: exactly one bottom, exactly one footwear, one or two tops (a base layer, optionally a mid layer over it; never two base layers or two mid layers), at most one outerwear piece, and 0-3 accessories only when they add something.
- Colour: keep the palette coherent (neutrals plus at most one accent colour); avoid clashing patterns; avoid two very similar tones that nearly match.
- Formality: all pieces of an outfit sit within about one level of the requested formality (1 sport ... 3 smart casual / casual chic ... 5 formal). Never mix sportswear with smart pieces.
- Day type: the request says whether it is a workday or a day off. On workdays aim for smart casual / casual chic (nothing sporty); on days off relaxed casual is fine. If extra formal is requested, go one notch smarter than usual. The style profile's no-gos (suits, formal dress shoes) always win over all of this.
- Weather: choose warmth and layers to fit the conditions given; add rain protection when precipitation is likely. The catalogue is already filtered for the weather.
- Give real variety between the outfits (different bottoms and different overall feel where the catalogue allows it).
- Respect the style profile. Never propose anything listed as unavailable or excluded, and never repeat a blocked outfit.
- Learn from his feedback (the learned taste and the recent ratings): favour the colour combinations, pieces and formality of well-rated outfits and avoid the traits of poorly rated ones. Feedback is guidance about taste; it never overrides the composition rules or the unavailable/blocked lists.
- If the user asks to avoid or replace a specific piece ("not those trousers", "without the grey sweater"), put its id in exclude_item_ids. Only include ids the user actually wants avoided; otherwise return an empty list.
- rationale: at most two concrete sentences (colours, formality, weather). reply: one short sentence to the user. Write in English.
- If the catalogue cannot support the requested number of good outfits, return fewer and say why in reply."""


# --------------------------------------------------------------------------- weather / season filter
def season_of(d: date) -> str:
    return {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer"}.get(d.month, "autumn")


def _too_warm(item: dict[str, Any], t_high: float) -> bool:
    w, cat, layer = item["warmth"] or 3, item["category"], item["layer"]
    if w >= 5 and t_high >= 12:
        return True
    if w >= 4 and t_high >= 18:
        return True
    if w >= 3 and t_high >= 25 and (cat == "outerwear" or (cat == "top" and layer == "mid")):
        return True
    return cat == "footwear" and w >= 4 and t_high >= 20


def _too_light(item: dict[str, Any], t_high: float) -> bool:
    w, cat, layer = item["warmth"] or 3, item["category"], item["layer"]
    if w > 1:
        return False
    if cat == "bottom":
        return t_high < 16  # shorts
    if cat == "footwear":
        return t_high < 15  # sandals
    return cat == "top" and layer != "base" and t_high < 10


def weather_filter(items: list[dict[str, Any]], weather: dict[str, Any] | None, today: date) -> list[dict[str, Any]]:
    """Drop items that make no sense today. Uses the day's high when known, otherwise the season."""
    t_high = weather.get("t_high") if weather else None
    if t_high is not None:
        kept = [i for i in items if not _too_warm(i, t_high) and not _too_light(i, t_high)]
    else:
        season = season_of(today)
        kept = [i for i in items if not i["seasons"] or season in i["seasons"]]
    # Safety net: never filter away a whole slot (e.g. a wardrobe with no warm-weather shoes).
    for cat in ("top", "bottom", "footwear"):
        if not any(i["category"] == cat for i in kept):
            kept += [i for i in items if i["category"] == cat and i not in kept]
    return sorted(kept, key=lambda i: i["id"])


# --------------------------------------------------------------------------- workdays / formality
def work_days(db: Database) -> set[int]:
    """Weekdays (Monday=0) that count as workdays; editable in Settings."""
    if not db.setting_exists("work_days"):
        return set(DEFAULT_WORK_DAYS)
    return {int(d) for d in db.get_setting("work_days").split(",") if d.strip().isdigit() and 0 <= int(d) <= 6}


def work_dress(db: Database) -> str:
    return db.get_setting("work_dress").strip() or DEFAULT_WORK_DRESS


def day_context(db: Database, today: date, ignore_workdays: bool = False) -> dict[str, Any]:
    """Is today a workday, and what does that mean for the outfit? (Days off are relaxed casual.)"""
    name = today.strftime("%A")
    scheduled = today.weekday() in work_days(db)
    workday = scheduled and not ignore_workdays
    if workday:
        line = f"Day type: workday ({name}). {work_dress(db)}"
    elif scheduled:
        line = (f"Day type: day off. It is a {name}, but he ticked 'ignore workdays', so treat it as a day off: "
                "relaxed casual is fine, comfort first.")
    else:
        line = f"Day type: day off ({name}): relaxed casual is fine, comfort first."
    return {"workday": workday, "scheduled": scheduled, "name": name, "line": line}


def formality_filter(items: list[dict[str, Any]], minimum: int) -> list[dict[str, Any]]:
    """Keep items at or above a formality level (accessories always stay). Never empties a required slot: if nothing
    is left of tops, bottoms or footwear, that slot steps down one level at a time until something is."""
    kept = [i for i in items if (i["formality"] or 3) >= minimum or i["category"] == "accessory"]
    for cat in ("top", "bottom", "footwear"):
        level = minimum
        while not any(i["category"] == cat for i in kept) and level > 1:
            level -= 1
            kept += [i for i in items if i["category"] == cat and (i["formality"] or 3) >= level and i not in kept]
    return sorted(kept, key=lambda i: i["id"])


# --------------------------------------------------------------------------- catalogue / repeat rules
def usable_items(db: Database) -> list[dict[str, Any]]:
    return [i for i in db.list_items(reviewed=True) if i["status"] != "retired" and i["category"]]


SLOTS = (("top", "tops"), ("bottom", "bottoms"), ("footwear", "shoes"))


def _or(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


def _counts(items: list[dict[str, Any]]) -> str:
    seen: dict[str, int] = {}
    for i in items:
        seen[i["category"] or "unknown"] = seen.get(i["category"] or "unknown", 0) + 1
    return ", ".join(f"{n} {c}" for c, n in sorted(seen.items())) or "nothing"


def wardrobe_readiness(db: Database) -> tuple[bool, str]:
    """Is there at least one approved top, bottom and pair of shoes? If not, explain exactly what is missing and why."""
    items = db.list_items()
    approved = [i for i in items if i["reviewed"] and i["status"] != "retired" and i["category"]]
    missing = [label for cat, label in SLOTS if not any(i["category"] == cat for i in approved)]
    if not missing:
        return True, ""
    if not items:
        return False, "Your wardrobe is empty. Add photos first."

    parts = [f"No approved {_or(missing)} yet."]
    parts.append(f"Approved: {len(approved)} of {len(items)} items" + (f" ({_counts(approved)})." if approved else "."))
    waiting = [i for i in items if not i["reviewed"] and i["ai_state"] == "done"]
    if waiting:
        parts.append(f"Waiting for your approval on the Review page: {_counts(waiting)}.")
        absent = [label for cat, label in SLOTS
                  if label in missing and not any(i["category"] == cat for i in waiting)]
        if absent:
            parts.append(f"None of the photos waiting was recognised as {_or(absent)}: check how the AI "
                         "categorised them (Wardrobe page, tap an item to correct it) or add photos of them.")
        else:
            parts.append("Approve them and advice will work.")
    busy = sum(1 for i in items if i["ai_state"] in ("queued", "processing"))
    failed = sum(1 for i in items if i["ai_state"] == "error")
    retired = sum(1 for i in items if i["status"] == "retired")
    if busy:
        parts.append(f"{busy} photos are still being catalogued.")
    if failed:
        parts.append(f"{failed} photos failed to catalogue (retry them on the Review page).")
    if retired:
        parts.append(f"{retired} retired items are ignored.")
    return False, " ".join(parts)


def catalogue_line(i: dict[str, Any]) -> str:
    return (f"#{i['id']} | {i['category']}/{i['subtype']} | {','.join(i['colors'])} | {i['pattern']} | "
            f"formality {i['formality']} | warmth {i['warmth']} | {','.join(i['seasons'])} | layer {i['layer']}"
            + (f" | {i['description']}" if i["description"] else ""))


def blocked_outfits(db: Database, cfg: Config, today: date) -> tuple[list[frozenset[int]], list[frozenset[int]]]:
    """(blocked, repeatable): identical outfits worn inside the repeat window are blocked, except that
    yesterday's outfit may be worn a second day in a row (as long as it is the only wear in the window)."""
    since = (today - timedelta(days=cfg.repeat_days)).isoformat()
    by_set: dict[frozenset[int], list[str]] = {}
    for o in db.list_outfits(statuses=("worn", "chosen"), since=since):
        d = o["worn_on"] or o["chosen_on"]
        if d:
            by_set.setdefault(frozenset(o["item_ids"]), []).append(d)
    yesterday = (today - timedelta(days=1)).isoformat()
    blocked, repeatable = [], []
    for s, days in by_set.items():
        (repeatable if days == [yesterday] else blocked).append(s)
    return blocked, repeatable


def validate_outfit(ids: list[int], allowed: dict[int, dict[str, Any]], blocked: list[frozenset[int]]) -> str | None:
    """Return None when the outfit is valid, otherwise a short reason."""
    if len(set(ids)) != len(ids):
        return "contains a duplicate item"
    missing = [i for i in ids if i not in allowed]
    if missing:
        return f"uses items that are not available: {missing}"
    items = [allowed[i] for i in ids]
    count = {c: sum(1 for x in items if x["category"] == c) for c in ("top", "bottom", "outerwear", "footwear", "accessory")}
    if count["bottom"] != 1:
        return "needs exactly one bottom"
    if count["footwear"] != 1:
        return "needs exactly one pair of footwear"
    if not 1 <= count["top"] <= 2:
        return "needs one or two tops"
    if count["outerwear"] > 1:
        return "has more than one outerwear piece"
    if count["accessory"] > 3:
        return "has more than three accessories"
    layers = [x["layer"] for x in items if x["category"] == "top"]
    if len(layers) == 2 and set(layers) != {"base", "mid"}:
        return "two tops must be one base layer and one mid layer"
    if frozenset(ids) in blocked:
        return "repeats an outfit worn recently"
    return None


# --------------------------------------------------------------------------- advice
def today_date(cfg: Config) -> date:
    return datetime.now(cfg.tz).date()


def style_profile(db: Database) -> str:
    return db.get_setting("style_profile") or DEFAULT_STYLE_PROFILE


def outfit_count(cfg: Config, db: Database) -> int:
    raw = db.get_setting("outfit_count")
    return int(raw) if raw.isdigit() and 1 <= int(raw) <= 6 else cfg.outfit_count


def session_created_today(s: dict[str, Any], cfg: Config) -> bool:
    return datetime.fromisoformat(s["created_at"]).astimezone(cfg.tz).date() == today_date(cfg)


def current_session(db: Database, cfg: Config) -> dict[str, Any] | None:
    """The conversation the app and the TV show: active in the last 12 hours, or started today (so the
    morning suggestions stay visible all day)."""
    s = db.latest_session()
    if not s:
        return None
    updated = datetime.fromisoformat(s["updated_at"])
    if datetime.now(updated.tzinfo) - updated < SESSION_MAX_AGE:
        return s
    return s if session_created_today(s, cfg) else None


class Stylist:
    def __init__(self, cfg: Config, db: Database, llm: LlmClient, weather: Weather):
        self.cfg, self.db, self.llm, self.weather = cfg, db, llm, weather

    def advise(self, message: str, *, new_session: bool = False, exclude_ids: list[int] | None = None,
               ignore_weather: bool = False, ignore_workdays: bool = False, extra_formal: bool = False) -> dict[str, Any]:
        cfg, db = self.cfg, self.db
        today = today_date(cfg)
        message = message.strip()[:1000]

        ready, why_not = wardrobe_readiness(db)
        if not ready:
            raise ValueError(why_not)
        all_items = usable_items(db)

        session = None if new_session else current_session(db, cfg)
        if not message:   # nothing typed: a suggestion that fits the weather and his usual style (or other options when refining)
            message = REFINE_REQUEST if session else DEFAULT_REQUEST
        excluded = set(session["excluded"]) if session else set()
        excluded |= {int(i) for i in (exclude_ids or [])}

        weather = None if ignore_weather else self.weather.get()
        pool = all_items if ignore_weather else weather_filter(all_items, weather, today)
        day = day_context(db, today, ignore_workdays)
        minimum = 3 if extra_formal else (2 if day["workday"] else 1)      # workdays: no sportswear; extra formal: smart pieces only
        if minimum > 1:
            pool = formality_filter(pool, minimum)
        catalogue = [i for i in pool if i["id"] not in excluded]
        laundry = sorted(i["id"] for i in catalogue if i["status"] == "laundry")
        allowed = {i["id"]: i for i in catalogue if i["status"] == "clean"}
        # Fail before spending anything when a whole slot is unavailable right now.
        for cat, label in SLOTS:
            if not any(i["category"] == cat for i in allowed.values()):
                raise ValueError(f"No clean {label} are available right now: they are in the wash "
                                 "or you excluded them earlier in this chat (a new request resets that).")
        blocked, repeatable = blocked_outfits(db, cfg, today)
        disliked = [d for d in disliked_sets(db) if d not in blocked]
        blocked = blocked + disliked
        n = outfit_count(cfg, db)
        learned = db.get_setting("taste_profile").strip()
        feedback = feedback_text(db)

        system = [
            {"type": "text", "text": SYSTEM_RULES + "\n\nStyle profile:\n" + style_profile(db)
                                     + (f"\n\nLearned taste (from his ratings; guidance, not law):\n{learned}" if learned else "")},
            {"type": "text", "text": "Wardrobe catalogue (id | category/subtype | colours | pattern | formality | "
                                     "warmth | seasons | layer | description):\n"
                                     + "\n".join(catalogue_line(i) for i in catalogue),
             "cache_control": {"type": "ephemeral"}},
        ]
        volatile = [
            f"Request: {message}",
            f"Today: {today.strftime('%A %d %B %Y')} ({season_of(today)})",
            day["line"],
            f"Weather: {weather['text']}" if weather and weather.get("text")
            else "Weather: unknown (assume mild, no rain)" if not ignore_weather
            else "Weather: ignore it for this request (the user may be travelling)",
            f"Number of outfits wanted: {n}",
            f"Unavailable right now (in the wash): {laundry or 'none'}",
            f"Blocked outfits (worn in the last {cfg.repeat_days} days, do not repeat): "
            f"{[sorted(s) for s in blocked if s not in disliked] or 'none'}",
        ]
        if extra_formal:
            volatile.append(EXTRA_FORMAL_NOTE)
        if disliked:
            volatile.append("Never propose these outfits again (he rated them poorly): "
                            f"{[sorted(s) for s in disliked]}")
        if feedback:
            volatile.append("His recent feedback on earlier outfits (newest first; 'worn' = real experience, "
                            "'suggestion' = first impression):\n" + feedback)
        if repeatable:
            volatile.append("Yesterday's outfit may be worn a second day in a row: "
                            f"{[sorted(s) for s in repeatable]}")
        history = db.get_messages(session["id"])[-2 * HISTORY_TURNS:] if session else []
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": "\n".join(volatile)})

        advice = self._call(system, messages)
        good, problems = self._split(advice, allowed, blocked)
        if problems:
            messages += [
                {"role": "assistant", "content": advice.model_dump_json()},
                {"role": "user", "content": "These outfits were invalid: " + "; ".join(problems)
                 + ". Return corrected outfits that follow all rules."},
            ]
            advice = self._call(system, messages)
            good, problems = self._split(advice, allowed, blocked)

        session_id = session["id"] if session else db.new_session()   # only now: a failed call must not leave an empty session
        excluded |= {i for i in advice.exclude_item_ids if i in {x["id"] for x in all_items}}
        db.set_session_excluded(session_id, list(excluded))
        db.replace_proposals(session_id)
        weather_text = weather["text"] if weather and weather.get("text") else ""
        outfit_ids = [db.add_outfit(session_id, o.name.strip()[:80], o.item_ids, o.rationale.strip(), weather_text)
                      for o in good[:n]]
        reply = advice.reply.strip() or "Here are some options."
        if not good:
            reply = "I couldn't build a valid outfit from what is available. " + reply
        db.add_message(session_id, "user", message)
        db.add_message(session_id, "assistant", json.dumps({
            "reply": reply, "outfits": [o.model_dump() for o in good[:n]], "exclude_item_ids": sorted(excluded)}))
        return {"session_id": session_id, "reply": reply, "outfit_ids": outfit_ids,
                "pool": len(catalogue), "total": len(all_items)}

    def _call(self, system: list[dict[str, Any]], messages: list[dict[str, Any]]) -> AdviceOut:
        return self.llm.structured(
            purpose="advice", model=self.cfg.stylist_model, system=system, messages=messages,
            out_model=AdviceOut, max_tokens=16000, thinking="adaptive", effort=self.cfg.stylist_effort,
        )

    @staticmethod
    def _split(advice: AdviceOut, allowed: dict[int, dict[str, Any]], blocked: list[frozenset[int]]):
        good, problems, seen = [], [], set()
        for o in advice.outfits:
            err = validate_outfit(o.item_ids, allowed, blocked)
            if not err and frozenset(o.item_ids) in seen:
                err = "duplicates another suggested outfit"
            if err:
                problems.append(f"'{o.name}' {err}")
            else:
                good.append(o)
                seen.add(frozenset(o.item_ids))
        return good, problems


# --------------------------------------------------------------------------- choosing, wearing, laundry
def wash_after(item: dict[str, Any]) -> int:
    cat = item["category"]
    return WASH_AFTER.get((cat, item["layer"]), WASH_AFTER.get((cat, None), 0))


def choose_outfit(db: Database, cfg: Config, outfit_id: int) -> None:
    today = today_date(cfg).isoformat()
    db.update_outfit(outfit_id, status="chosen", chosen_on=today)
    db.demote_chosen(today, outfit_id)


def wear_outfit(db: Database, outfit_id: int, on: str) -> None:
    """Mark as worn: bump wear counters and send items to the wash when they hit their limit."""
    outfit = db.get_outfit(outfit_id)
    if not outfit or outfit["status"] == "worn":
        return
    for item in db.items_by_ids(outfit["item_ids"]).values():
        wears = item["wears_since_wash"] + 1
        limit = wash_after(item)
        fields: dict[str, Any] = {"wears_since_wash": wears, "last_worn": on}
        if limit and wears >= limit:
            fields["status"] = "laundry"
        db.update_item(item["id"], fields)
    db.update_outfit(outfit_id, status="worn", worn_on=on)


def sync_worn(db: Database, cfg: Config) -> None:
    """Outfits chosen on an earlier day count as worn (idempotent; runs whenever state is read)."""
    today = today_date(cfg).isoformat()
    for o in db.list_outfits(statuses=("chosen",)):
        if o["chosen_on"] and o["chosen_on"] < today:
            wear_outfit(db, o["id"], o["chosen_on"])


def mark_clean(db: Database, item_ids: list[int]) -> None:
    for i in item_ids:
        db.update_item(i, {"status": "clean", "wears_since_wash": 0})
