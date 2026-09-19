import json
from datetime import date, timedelta

import pytest

from clothing_advisor import stylist
from clothing_advisor.stylist import blocked_outfits, season_of, validate_outfit, weather_filter

from .conftest import add_item

TODAY = date(2026, 7, 15)


def item(i, category, subtype, **kw):
    base = dict(id=i, category=category, subtype=subtype, layer="none", warmth=3, seasons=["spring", "summer", "autumn", "winter"])
    base.update(kw)
    return base


def ids(items):
    return {i["id"] for i in items}


# ------------------------------------------------------------------ weather / season pre-filter
def test_winter_coat_dropped_in_summer_and_shorts_dropped_in_winter():
    coat = item(1, "outerwear", "puffer", layer="outer", warmth=5, seasons=["winter"])
    shorts = item(2, "bottom", "shorts", warmth=1, seasons=["summer"])
    jeans = item(3, "bottom", "jeans")
    tee = item(4, "top", "tee", layer="base", warmth=1)
    shoes = item(5, "footwear", "sneakers")
    everything = [coat, shorts, jeans, tee, shoes]

    hot = weather_filter(everything, {"t_high": 27}, TODAY)
    assert 1 not in ids(hot) and {2, 3, 4, 5} <= ids(hot)

    cold = weather_filter(everything, {"t_high": 4}, TODAY)
    assert 2 not in ids(cold)                      # shorts make no sense
    assert {1, 3, 4, 5} <= ids(cold)               # t-shirt stays as a base layer under a sweater


def test_mid_layer_warm_jacket_thresholds():
    parka = item(1, "outerwear", "parka", layer="outer", warmth=4)
    assert 1 in ids(weather_filter([parka, item(2, "top", "t", layer="base"), item(3, "bottom", "b"),
                                    item(4, "footwear", "f")], {"t_high": 14}, TODAY))
    assert 1 not in ids(weather_filter([parka, item(2, "top", "t", layer="base"), item(3, "bottom", "b"),
                                        item(4, "footwear", "f")], {"t_high": 19}, TODAY))


def test_season_fallback_without_weather():
    assert season_of(date(2026, 1, 5)) == "winter" and season_of(TODAY) == "summer"
    coat = item(1, "outerwear", "puffer", layer="outer", warmth=5, seasons=["winter"])
    tee = item(2, "top", "tee", layer="base", seasons=["spring", "summer"])
    rest = [item(3, "bottom", "b"), item(4, "footwear", "f")]
    assert ids(weather_filter([coat, tee, *rest], None, TODAY)) == {2, 3, 4}


def test_filter_never_empties_a_required_slot():
    only_shorts = item(1, "bottom", "shorts", warmth=1, seasons=["summer"])
    rest = [item(2, "top", "t", layer="base"), item(3, "footwear", "f")]
    assert 1 in ids(weather_filter([only_shorts, *rest], {"t_high": 3}, TODAY))


# ------------------------------------------------------------------ validation
def pool(*specs):
    return {i["id"]: i for i in (item(n, c, "x", layer=l) for n, c, l in specs)}


def test_validate_outfit_rules():
    p = pool((1, "top", "base"), (2, "top", "mid"), (3, "top", "base"), (4, "bottom", "none"), (5, "footwear", "none"),
             (6, "outerwear", "outer"), (7, "outerwear", "outer"), (8, "bottom", "none"))
    assert validate_outfit([1, 4, 5], p, []) is None
    assert validate_outfit([1, 2, 4, 5, 6], p, []) is None
    assert "bottom" in validate_outfit([1, 5], p, [])
    assert "bottom" in validate_outfit([1, 4, 8, 5], p, [])
    assert "footwear" in validate_outfit([1, 4], p, [])
    assert "two tops" in validate_outfit([1, 3, 4, 5], p, [])
    assert "outerwear" in validate_outfit([1, 4, 5, 6, 7], p, [])
    assert "not available" in validate_outfit([1, 4, 5, 99], p, [])
    assert "duplicate" in validate_outfit([1, 1, 4, 5], p, [])
    assert "recently" in validate_outfit([1, 4, 5], p, [frozenset({1, 4, 5})])


# ------------------------------------------------------------------ repeat rule and laundry
def _outfit(ctx, item_ids, status, day):
    sid = ctx.db.new_session()
    oid = ctx.db.add_outfit(sid, "o", item_ids, "", "")
    ctx.db.update_outfit(oid, status=status, **({"worn_on": day} if status == "worn" else {"chosen_on": day}))
    return oid


def test_repeat_rule_allows_only_a_second_consecutive_day(ctx):
    a = [add_item(ctx, 1, "top", "t", layer="base"), add_item(ctx, 2, "bottom", "b"), add_item(ctx, 3, "footwear", "f")]
    fixed = TODAY.isoformat()
    y, d3 = (TODAY - timedelta(days=1)).isoformat(), (TODAY - timedelta(days=3)).isoformat()

    _outfit(ctx, a, "worn", y)
    blocked, repeatable = blocked_outfits(ctx.db, ctx.cfg, TODAY)
    assert blocked == [] and repeatable == [frozenset(a)]      # yesterday only: may go again

    _outfit(ctx, a, "worn", d3)
    blocked, repeatable = blocked_outfits(ctx.db, ctx.cfg, TODAY)
    assert blocked == [frozenset(a)] and repeatable == []      # also worn 3 days ago: blocked

    b = [a[0], add_item(ctx, 4, "bottom", "b2"), a[2]]
    _outfit(ctx, b, "chosen", fixed)                          # chosen today counts as worn today
    blocked, _ = blocked_outfits(ctx.db, ctx.cfg, TODAY)
    assert frozenset(b) in blocked

    old = [add_item(ctx, 5, "top", "t2", layer="base"), a[1], a[2]]
    _outfit(ctx, old, "worn", (TODAY - timedelta(days=9)).isoformat())
    blocked, repeatable = blocked_outfits(ctx.db, ctx.cfg, TODAY)
    assert frozenset(old) not in blocked and frozenset(old) not in repeatable   # outside the 7-day window


def test_wearing_sends_items_to_the_wash_after_their_limit(ctx):
    tee = add_item(ctx, 1, "top", "tee", layer="base")
    sweater = add_item(ctx, 2, "top", "sweater", layer="mid")
    jeans = add_item(ctx, 3, "bottom", "jeans")
    shoes = add_item(ctx, 4, "footwear", "sneakers")
    for day in range(1, 4):
        oid = _outfit(ctx, [tee, sweater, jeans, shoes], "proposed", None)
        stylist.wear_outfit(ctx.db, oid, f"2026-07-0{day}")
    status = {i: ctx.db.get_item(i)["status"] for i in (tee, sweater, jeans, shoes)}
    assert status == {tee: "laundry", sweater: "laundry", jeans: "clean", shoes: "clean"}   # 1x, 3x, 4x, never
    stylist.mark_clean(ctx.db, [tee])
    assert ctx.db.get_item(tee)["status"] == "clean" and ctx.db.get_item(tee)["wears_since_wash"] == 0


def test_chosen_outfit_from_yesterday_counts_as_worn(ctx):
    tee, jeans, shoes = (add_item(ctx, 1, "top", "t", layer="base"), add_item(ctx, 2, "bottom", "j"),
                         add_item(ctx, 3, "footwear", "s"))
    oid = _outfit(ctx, [tee, jeans, shoes], "chosen", "2000-01-01")
    stylist.sync_worn(ctx.db, ctx.cfg)
    assert ctx.db.get_outfit(oid)["status"] == "worn" and ctx.db.get_outfit(oid)["worn_on"] == "2000-01-01"
    assert ctx.db.get_item(tee)["status"] == "laundry"


# ------------------------------------------------------------------ end to end with the fake Claude
@pytest.fixture
def wardrobe(ctx, monkeypatch):
    monkeypatch.setattr(stylist, "today_date", lambda cfg: TODAY)
    w = dict(
        tee=add_item(ctx, 1, "top", "t-shirt", layer="base", warmth=1, seasons=("spring", "summer")),
        polo=add_item(ctx, 2, "top", "polo", layer="base", warmth=2, formality=3, seasons=("spring", "summer")),
        chinos=add_item(ctx, 3, "bottom", "chinos", warmth=2, formality=3),
        shorts=add_item(ctx, 4, "bottom", "shorts", warmth=1, seasons=("summer",)),
        sneakers=add_item(ctx, 5, "footwear", "sneakers"),
        parka=add_item(ctx, 6, "outerwear", "winter parka", layer="outer", warmth=5, seasons=("winter",)),
    )
    return w


def test_advice_end_to_end_prefilters_validates_and_stores(ctx, fake, wardrobe):
    w = wardrobe
    fake.queue({"reply": "Two easy summer options.",
                "outfits": [{"name": "Polo & chinos", "item_ids": [w["polo"], w["chinos"], w["sneakers"]], "rationale": "smart casual"},
                            {"name": "Tee & shorts", "item_ids": [w["tee"], w["shorts"], w["sneakers"]], "rationale": "relaxed"}],
                "exclude_item_ids": []})
    result = stylist.Stylist(ctx.cfg, ctx.db, ctx.llm, ctx.weather).advise("Something casual")

    call = fake.calls[0]
    catalogue = call["system"][1]["text"]
    assert "winter parka" not in catalogue and "shorts" in catalogue     # pre-filter by season (no weather set)
    assert call["system"][1]["cache_control"] == {"type": "ephemeral"}   # catalogue is the cached prefix
    assert call["thinking"] == {"type": "adaptive"} and call["output_config"]["effort"] == "high"
    assert call["model"] == "claude-sonnet-5"
    stored = ctx.db.list_outfits(session_id=result["session_id"])
    assert [o["name"] for o in stored] == ["Polo & chinos", "Tee & shorts"] and all(o["status"] == "proposed" for o in stored)
    assert ctx.tracker.spend() > 0


def test_invalid_outfit_triggers_one_retry_then_only_valid_are_kept(ctx, fake, wardrobe):
    w = wardrobe
    bad = {"name": "No shoes", "item_ids": [w["polo"], w["chinos"]], "rationale": "x"}
    good = {"name": "Polo & chinos", "item_ids": [w["polo"], w["chinos"], w["sneakers"]], "rationale": "x"}
    fake.queue({"reply": "a", "outfits": [bad], "exclude_item_ids": []}, {"reply": "b", "outfits": [good], "exclude_item_ids": []})
    result = stylist.Stylist(ctx.cfg, ctx.db, ctx.llm, ctx.weather).advise("casual")
    assert len(fake.calls) == 2
    assert "invalid" in fake.calls[1]["messages"][-1]["content"]
    assert len(result["outfit_ids"]) == 1


def test_refinement_keeps_session_and_persists_exclusions(ctx, fake, wardrobe):
    w = wardrobe
    st = stylist.Stylist(ctx.cfg, ctx.db, ctx.llm, ctx.weather)
    first = {"name": "A", "item_ids": [w["polo"], w["chinos"], w["sneakers"]], "rationale": "r"}
    fake.queue({"reply": "ok", "outfits": [first], "exclude_item_ids": []})
    r1 = st.advise("casual")
    fake.queue({"reply": "no chinos then", "outfits": [{"name": "B", "item_ids": [w["tee"], w["shorts"], w["sneakers"]], "rationale": "r"}],
                "exclude_item_ids": [w["chinos"]]})
    r2 = st.advise("not those trousers")
    assert r2["session_id"] == r1["session_id"]
    assert ctx.db.get_session(r1["session_id"])["excluded"] == [w["chinos"]]
    statuses = {o["name"]: o["status"] for o in ctx.db.list_outfits(session_id=r1["session_id"])}
    assert statuses == {"A": "replaced", "B": "proposed"}
    # third turn: the excluded trousers must be gone from the catalogue, and earlier turns are sent as history
    fake.queue({"reply": "x", "outfits": [], "exclude_item_ids": []})
    st.advise("something else")
    third = fake.calls[-1]
    assert "chinos" not in third["system"][1]["text"]
    assert len(third["messages"]) == 5  # 2 turns of history + new request


def test_laundry_items_are_listed_as_unavailable_and_rejected(ctx, fake, wardrobe):
    w = wardrobe
    ctx.db.update_item(w["polo"], {"status": "laundry"})
    fake.queue({"reply": "r", "outfits": [{"name": "Uses laundry", "item_ids": [w["polo"], w["chinos"], w["sneakers"]], "rationale": "x"}],
                "exclude_item_ids": []}, {"reply": "r", "outfits": [], "exclude_item_ids": []})
    result = stylist.Stylist(ctx.cfg, ctx.db, ctx.llm, ctx.weather).advise("casual")
    assert f"[{w['polo']}]" in fake.calls[0]["messages"][-1]["content"]
    assert result["outfit_ids"] == [] and "couldn't" in result["reply"]


def test_advice_needs_a_minimum_wardrobe(ctx, fake):
    with pytest.raises(ValueError):
        stylist.Stylist(ctx.cfg, ctx.db, ctx.llm, ctx.weather).advise("casual")
    assert fake.calls == []


# ------------------------------------------------------------------ readiness / error messages
def test_readiness_empty_wardrobe(ctx):
    ok, msg = stylist.wardrobe_readiness(ctx.db)
    assert not ok and "empty" in msg


def test_readiness_explains_unapproved_items_and_missing_shoes(ctx):
    for n, (cat, layer) in enumerate([("top", "base"), ("top", "base"), ("bottom", "none"), ("accessory", "none")], start=1):
        add_item(ctx, n, cat, "x", layer=layer, reviewed=False)
    ok, msg = stylist.wardrobe_readiness(ctx.db)
    assert not ok
    assert "No approved tops, bottoms or shoes" in msg
    assert "Approved: 0 of 4 items." in msg
    assert "2 top" in msg and "1 bottom" in msg and "1 accessory" in msg     # what the AI made of the photos
    assert "None of the photos waiting was recognised as shoes" in msg


def test_readiness_only_shoes_missing_and_still_processing(ctx):
    add_item(ctx, 1, "top", "t", layer="base")
    add_item(ctx, 2, "bottom", "b")
    queued = ctx.db.add_item("shaq", "q.jpg", "q.jpg")          # still queued for the AI
    ok, msg = stylist.wardrobe_readiness(ctx.db)
    assert not ok and msg.startswith("No approved shoes yet.")
    assert "Approved: 2 of 3 items (1 bottom, 1 top)." in msg
    assert "1 photos are still being catalogued" in msg
    add_item(ctx, 4, "footwear", "s")
    assert stylist.wardrobe_readiness(ctx.db) == (True, "")


def test_readiness_says_approve_when_the_waiting_items_would_fill_the_gap(ctx):
    add_item(ctx, 1, "top", "t", layer="base")
    add_item(ctx, 2, "bottom", "b")
    add_item(ctx, 3, "footwear", "s", reviewed=False)
    ok, msg = stylist.wardrobe_readiness(ctx.db)
    assert not ok and "Approve them and advice will work." in msg


def test_advice_error_uses_the_specific_message_and_costs_nothing(ctx, fake):
    add_item(ctx, 1, "top", "t", layer="base", reviewed=False)
    with pytest.raises(ValueError, match="Waiting for your approval"):
        stylist.Stylist(ctx.cfg, ctx.db, ctx.llm, ctx.weather).advise("casual")
    assert fake.calls == [] and ctx.db.latest_session() is None


def test_all_shoes_in_the_wash_fails_early_without_creating_a_session(ctx, fake, wardrobe):
    ctx.db.update_item(wardrobe["sneakers"], {"status": "laundry"})
    with pytest.raises(ValueError, match="No clean shoes"):
        stylist.Stylist(ctx.cfg, ctx.db, ctx.llm, ctx.weather).advise("casual")
    assert fake.calls == [] and ctx.db.latest_session() is None
