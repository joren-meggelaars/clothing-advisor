"""Shopping advice: what to buy so more smart-casual / casual-chic outfits become possible.

Two Claude calls, kept separate so the expensive one is not repeated on every request:

1. `ensure_brief` -- a web-search research pass ("what's on trend this season, at which stores, for roughly
   what price") that is reused for BRIEF_MAX_AGE_DAYS or until the season changes. This is the only call that
   costs a web_search fee on top of tokens.
2. `advise` -- a plain structured-output call (no search) that turns the brief, the current wardrobe, a
   from-scratch gap analysis, the cost profile and past feedback into 3-4 outfits, each a mix of pieces he
   already owns and pieces to buy. No product links or prices are invented beyond what the brief or Claude's
   general knowledge supports; `price_low`/`price_high` are estimates, not quotes.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .config import Config
from .db import Database
from .llm import LlmClient, LlmError
from .schema import ShopAdviceOut
from .stylist import catalogue_line, season_of, style_profile, today_date, usable_items
from .usage import BudgetExceeded

BRIEF_MAX_AGE_DAYS = 30
BRIEF_MAX_SEARCHES = 6
BRIEF_MODEL_DEFAULT = "claude-sonnet-5"
ADVICE_MODEL_DEFAULT = "claude-sonnet-5"
SMART_CASUAL_FORMALITY = 3          # items at/above this level count as smart-casual / casual-chic capable
OUTFIT_COUNT = 4

COST_PROFILES: dict[str, dict[str, Any]] = {
    "budget": {
        "label": "Budget-friendly",
        "ranges": {"top": (15, 35), "bottom": (30, 55), "footwear": (40, 75), "outerwear": (50, 95), "accessory": (10, 25)},
    },
    "mid": {
        "label": "Mid-range",
        "ranges": {"top": (30, 60), "bottom": (50, 85), "footwear": (70, 120), "outerwear": (90, 160), "accessory": (20, 40)},
    },
}
DEFAULT_COST_PROFILE = "budget"
DEFAULT_BUDGET_EUR = 300.0
DEFAULT_STORES = "Jack & Jones, Only & Sons, Pull & Bear, Zara, H&M, WE Fashion, Sting (its more casual/smart-casual lines)"
DEFAULT_STORES_NOTE = (
    "Usually shops at Trendstore Uden, but its brands run slightly too expensive for his taste; open to shopping "
    "more at newer, more affordable brands instead. Zalando, OMODA, About You and Van Tilburg Mode are useful for "
    "browsing many brands at once, not specific brands themselves."
)
DEFAULT_SIZES = "Tops: M. Trousers: waist 30, 31 or 32 depending on the cut/fit; length 30, or 32 if 30 is not available."
DEFAULT_STYLE_SOURCES = (
    "For trend and outfit-building context (not stores): Effortless Gent (effortlessgent.com), Permanent Style "
    "(permanentstyle.com) and Real Men Real Style (realmenrealstyle.com)."
)

BRIEF_SYSTEM = """You research current men's smart-casual / casual-chic fashion for one man in the Netherlands, using web search.

He wants concrete, current information he can act on, not generic style advice:
- 4-6 specific pieces or combinations that are genuinely on trend right now for smart casual / casual chic menswear (silhouettes, colours, fabrics), each with a one-line reason.
- Which of his usual/likely stores (see the request) currently carry pieces matching this, with a realistic EUR price range per piece at that store, for the given cost profile.
- Keep it grounded in what you actually find; do not invent specific product names or exact prices you have not seen. A price range is fine when you have not seen an exact figure.
Write in English, as a few short paragraphs or a tight bullet list (your choice, whichever fits the material better). No headings needed."""

ADVICE_SYSTEM = """You are a shopping advisor helping one man build 3-4 smart-casual / casual-chic outfits for a new job that puts him in front of clients and in the office more often.

Rules:
- Each outfit mixes what he already owns (from the wardrobe catalogue below) with 0-3 new pieces to buy. Reusing owned pieces across several outfits (one new pair of trousers with several different tops, for example) is preferred over designing every outfit from scratch: it is cheaper and more practical, and he explicitly wants that.
- Every owned_item_ids entry must be an id that appears in the wardrobe catalogue below. Never invent ids.
- Every "buy" item needs a category, a specific but realistic description (colour, fabric, silhouette), a EUR price_low/price_high within the cost profile's range for that category, a store_suggestion (one or more of his stores, or "any of his usual stores" if any fit), and a one-line reason.
- Total of all buy items across all outfits together should stay close to his total budget for this round; it is fine to go a little under, avoid going over.
- Respect his style profile's no-gos (no suits, no dress shirt with suit trousers, no formal dress shoes) and his sizes.
- Use the season research below for what is actually on trend; do not repeat generic advice it does not support.
- Learn from his past feedback on suggestions (if any): favour what he liked, avoid what he disliked.
- rationale: at most two sentences. reply: one short sentence to him. Write in English."""


def gap_analysis(db: Database) -> dict[str, Any]:
    """What is missing for more smart-casual / casual-chic outfits, computed from the wardrobe alone (no Claude call)."""
    items = [i for i in usable_items(db) if i["status"] == "clean"]
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for i in items:
        by_cat.setdefault(i["category"], []).append(i)
    gaps = []
    smart = {cat: [i for i in lst if (i["formality"] or 0) >= SMART_CASUAL_FORMALITY] for cat, lst in by_cat.items()}
    if len(smart.get("top", [])) < 3:
        gaps.append(f"Only {len(smart.get('top', []))} smart-casual top(s) (aim for 3+ for real rotation).")
    if len(smart.get("bottom", [])) < 2:
        gaps.append(f"Only {len(smart.get('bottom', []))} smart-casual bottom(s) (aim for 2+).")
    if not smart.get("footwear"):
        gaps.append("No smart-casual footwear at all yet.")
    if not smart.get("outerwear"):
        gaps.append("No smart-casual outerwear/mid-layer (overshirt, fine knit, blazer-adjacent piece) yet.")
    return {
        "counts": {cat: len(lst) for cat, lst in smart.items()},
        "total_smart_casual_pieces": sum(len(lst) for lst in smart.values()),
        "gaps": gaps or ["No obvious gap: he already has enough smart-casual pieces to combine into outfits."],
    }


def format_gap(gap: dict[str, Any]) -> str:
    return "\n".join(f"- {g}" for g in gap["gaps"])


def cost_profile_key(db: Database) -> str:
    key = db.get_setting("shop_cost_profile")
    return key if key in COST_PROFILES else DEFAULT_COST_PROFILE


def cost_profile(db: Database) -> dict[str, Any]:
    return COST_PROFILES[cost_profile_key(db)]


def budget_eur(db: Database) -> float:
    raw = db.get_setting("shop_budget_eur")
    try:
        value = float(raw)
        return value if value > 0 else DEFAULT_BUDGET_EUR
    except ValueError:
        return DEFAULT_BUDGET_EUR


def preferred_stores(db: Database) -> str:
    return db.get_setting("shop_stores") or DEFAULT_STORES


def stores_note(db: Database) -> str:
    return db.get_setting("shop_stores_note") or DEFAULT_STORES_NOTE


def sizes(db: Database) -> str:
    return db.get_setting("shop_sizes") or DEFAULT_SIZES


def format_price_ranges(profile: dict[str, Any]) -> str:
    return ", ".join(f"{cat} EUR {lo}-{hi}" for cat, (lo, hi) in profile["ranges"].items())


def format_feedback(db: Database) -> str:
    rows = db.recent_shop_feedback()
    if not rows:
        return ""
    lines = []
    for r in rows:
        o = r["outfit"]
        buys = "; ".join(f"{b['description']} ({b['store_suggestion']})" for b in o.get("buy", []))
        lines.append(f"- [{r['verdict']}{': ' + r['comment'] if r['comment'] else ''}] \"{o['name']}\": {buys or 'owned pieces only'}")
    return "\n".join(lines)


def needs_new_brief(db: Database, cfg: Config) -> bool:
    brief = db.latest_shop_brief()
    if not brief:
        return True
    if brief["season"] != season_of(today_date(cfg)):
        return True
    age_days = (date.today() - date.fromisoformat(brief["created_at"][:10])).days
    return age_days > BRIEF_MAX_AGE_DAYS


def ensure_brief(cfg: Config, db: Database, llm: LlmClient, *, force: bool = False) -> dict[str, Any]:
    """The current season's brief, researched fresh only when missing, stale or the season changed."""
    if not force and not needs_new_brief(db, cfg):
        return db.latest_shop_brief()
    season = season_of(today_date(cfg))
    profile = cost_profile(db)
    prompt = (
        f"Season: {season}. Cost profile: {profile['label']} ({format_price_ranges(profile)}).\n"
        f"His usual/likely stores: {preferred_stores(db)}.\n{stores_note(db)}\n{DEFAULT_STYLE_SOURCES}\n"
        "Research current smart-casual / casual-chic menswear trends for this season and how they show up at "
        "these (or very similar) stores, within this cost profile."
    )
    outcome = llm.search(purpose="shop_brief", model=BRIEF_MODEL_DEFAULT, system=BRIEF_SYSTEM, prompt=prompt,
                         max_tokens=4000, max_uses=BRIEF_MAX_SEARCHES)
    brief_id = db.add_shop_brief(season, outcome.text, outcome.sources, outcome.searches,
                                 outcome.searches * 0.01 * cfg.usd_eur)
    return db.get_shop_brief(brief_id)


def _usable_ids(db: Database) -> set[int]:
    return {i["id"] for i in usable_items(db) if i["status"] == "clean"}


def _validate(advice: ShopAdviceOut, usable_ids: set[int]) -> tuple[list, list[str]]:
    good, problems = [], []
    for o in advice.outfits:
        owned = [i for i in o.owned_item_ids if i in usable_ids]
        dropped = set(o.owned_item_ids) - usable_ids
        if dropped:
            problems.append(f"\"{o.name}\" referenced unknown/unavailable item ids {sorted(dropped)}")
        if not owned and not o.buy:
            problems.append(f"\"{o.name}\" ended up empty after removing unavailable items")
            continue
        good.append({"name": o.name.strip()[:80], "owned_item_ids": owned,
                    "buy": [b.model_dump() for b in o.buy], "rationale": o.rationale.strip()})
    return good, problems


def advise(cfg: Config, db: Database, llm: LlmClient, *, request: str = "", refresh_brief: bool = False) -> dict[str, Any]:
    brief = ensure_brief(cfg, db, llm, force=refresh_brief)
    catalogue = usable_items(db)
    usable_ids = _usable_ids(db)
    gap = gap_analysis(db)
    profile = cost_profile(db)

    system = [
        {"type": "text", "text": ADVICE_SYSTEM + "\n\nStyle profile:\n" + style_profile(db)
                                 + f"\n\nSizes: {sizes(db)}"},
        {"type": "text", "text": "Wardrobe catalogue (id | category/subtype | colours | pattern | formality | "
                                 "warmth | seasons | layer | description):\n"
                                 + "\n".join(catalogue_line(i) for i in catalogue),
         "cache_control": {"type": "ephemeral"}},
    ]
    volatile = [
        f"Request: {request.strip() or 'Suggest outfits for the new season.'}",
        f"Cost profile: {profile['label']} (per-piece guide: {format_price_ranges(profile)}). "
        f"Total budget for this round: about EUR {budget_eur(db):.0f}.",
        f"His stores: {preferred_stores(db)}. {stores_note(db)}",
        f"Wardrobe gaps (from what he already owns):\n{format_gap(gap)}",
        f"Season research:\n{brief['text']}",
    ]
    feedback = format_feedback(db)
    if feedback:
        volatile.append("His feedback on earlier shopping suggestions (newest first):\n" + feedback)
    messages = [{"role": "user", "content": "\n\n".join(volatile)}]

    result = llm.structured(purpose="shop_advice", model=ADVICE_MODEL_DEFAULT, system=system, messages=messages,
                            out_model=ShopAdviceOut, max_tokens=8000, thinking="adaptive", effort="medium")
    good, problems = _validate(result, usable_ids)
    if problems:
        messages += [
            {"role": "assistant", "content": result.model_dump_json()},
            {"role": "user", "content": "These outfits had a problem: " + "; ".join(problems)
             + ". Return corrected outfits that only use ids from the catalogue."},
        ]
        result = llm.structured(purpose="shop_advice", model=ADVICE_MODEL_DEFAULT, system=system, messages=messages,
                                out_model=ShopAdviceOut, max_tokens=8000, thinking="adaptive", effort="medium")
        good, problems = _validate(result, usable_ids)

    reply = result.reply.strip() or "Here is what I'd add."
    if not good:
        reply = "I couldn't put together a valid set of outfits this time. " + reply
    advice_id = db.add_shop_advice(brief["id"], cost_profile_key(db), request.strip(), reply, good[:OUTFIT_COUNT], 0.0)
    return {"advice_id": advice_id, "brief": brief, "gap": gap}


def advice_view(db: Database, outfit_view_owned) -> dict[str, Any] | None:
    """The latest advice, with owned items expanded for display and any feedback already given.
    `outfit_view_owned(item_ids)` turns a list of owned ids into displayable item dicts (thumb, subtype, colour)."""
    a = db.latest_shop_advice()
    if not a:
        return None
    fb = db.shop_feedback(a["id"])
    outfits = []
    for idx, o in enumerate(a["outfits"]):
        outfits.append({**o, "owned": outfit_view_owned(o["owned_item_ids"]),
                        "feedback": {"verdict": fb[idx]["verdict"], "comment": fb[idx]["comment"]} if idx in fb else None})
    return {"id": a["id"], "created_at": a["created_at"], "profile": a["profile"], "request": a["request"],
           "reply": a["reply"], "outfits": outfits}
