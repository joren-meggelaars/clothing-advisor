"""Shopping advice: gap analysis (no Claude), the season brief (web search) and the outfit advice (structured)."""
import pytest

from clothing_advisor import shop
from clothing_advisor.llm import LlmError
from clothing_advisor.usage import BudgetExceeded

from .conftest import add_item
from .test_llm import FakeSearchAnthropic, block, usage


# ------------------------------------------------------------------ gap analysis (pure, no Claude)
def test_gap_analysis_on_an_empty_wardrobe_flags_everything(ctx):
    gap = shop.gap_analysis(ctx.db)
    assert gap["total_smart_casual_pieces"] == 0
    assert any("top" in g for g in gap["gaps"])
    assert any("bottom" in g for g in gap["gaps"])
    assert any("footwear" in g for g in gap["gaps"])
    assert any("outerwear" in g for g in gap["gaps"])


def test_gap_analysis_only_counts_clean_smart_casual_items(ctx):
    add_item(ctx, 1, "top", "polo", formality=3)
    add_item(ctx, 2, "top", "t-shirt", formality=1)                      # too casual: not counted
    add_item(ctx, 3, "bottom", "chinos", formality=3, status="laundry")  # not clean: not counted
    gap = shop.gap_analysis(ctx.db)
    assert gap["counts"].get("top") == 1 and gap["counts"].get("bottom", 0) == 0


def test_gap_analysis_says_no_gap_once_the_wardrobe_is_full(ctx):
    add_item(ctx, 1, "top", "polo", formality=3)
    add_item(ctx, 2, "top", "shirt jacket", formality=3)
    add_item(ctx, 3, "top", "fine knit", formality=3)
    add_item(ctx, 4, "bottom", "chinos", formality=3)
    add_item(ctx, 5, "bottom", "dark jeans", formality=3)
    add_item(ctx, 6, "footwear", "loafers", formality=3)
    add_item(ctx, 7, "outerwear", "overshirt", formality=3)
    gap = shop.gap_analysis(ctx.db)
    assert gap["gaps"] == ["No obvious gap: he already has enough smart-casual pieces to combine into outfits."]


# ------------------------------------------------------------------ settings helpers
def test_settings_fall_back_to_sensible_defaults(ctx):
    assert shop.cost_profile_key(ctx.db) == shop.DEFAULT_COST_PROFILE
    assert shop.budget_eur(ctx.db) == shop.DEFAULT_BUDGET_EUR
    assert shop.preferred_stores(ctx.db) == shop.DEFAULT_STORES
    assert shop.stores_note(ctx.db) == shop.DEFAULT_STORES_NOTE
    assert shop.sizes(ctx.db) == shop.DEFAULT_SIZES


def test_settings_use_the_saved_values(ctx):
    ctx.db.set_setting("shop_cost_profile", "mid")
    ctx.db.set_setting("shop_budget_eur", "450")
    assert shop.cost_profile_key(ctx.db) == "mid" and shop.budget_eur(ctx.db) == 450.0


def test_bad_saved_budget_falls_back_to_the_default(ctx):
    ctx.db.set_setting("shop_budget_eur", "not-a-number")
    assert shop.budget_eur(ctx.db) == shop.DEFAULT_BUDGET_EUR
    ctx.db.set_setting("shop_cost_profile", "premium-typo")
    assert shop.cost_profile_key(ctx.db) == shop.DEFAULT_COST_PROFILE


# ------------------------------------------------------------------ the season brief (web search)
def _queue_search(fake, text="Camel overshirts and dark denim are on trend.", url="https://zalando.nl/x"):
    fake.queue([
        block(type="server_tool_use", id="t1", name="web_search", input={"query": "smart casual trends"}),
        block(type="web_search_tool_result", tool_use_id="t1", content=[block(url=url, title="Zalando trends")]),
        block(type="text", text=text),
    ], usage_=usage(searches=2))


@pytest.fixture
def search_ctx(cfg):
    from clothing_advisor.context import build_ctx
    fake = FakeSearchAnthropic()
    _queue_search(fake)
    return build_ctx(cfg, fake), fake


def test_ensure_brief_researches_once_and_stores_it(search_ctx):
    ctx, fake = search_ctx
    brief = shop.ensure_brief(ctx.cfg, ctx.db, ctx.llm)
    assert brief["text"] == "Camel overshirts and dark denim are on trend."
    assert brief["sources"] == [{"url": "https://zalando.nl/x", "title": "Zalando trends"}]
    assert brief["searches"] == 2 and brief["cost_eur"] > 0
    assert len(fake.calls) == 1
    prompt = fake.calls[0]["messages"][0]["content"]
    assert "budget" in prompt.lower() or "cost profile" in prompt.lower()


def test_ensure_brief_is_reused_without_a_new_search(search_ctx):
    ctx, fake = search_ctx
    first = shop.ensure_brief(ctx.cfg, ctx.db, ctx.llm)
    second = shop.ensure_brief(ctx.cfg, ctx.db, ctx.llm)
    assert first["id"] == second["id"] and len(fake.calls) == 1


def test_ensure_brief_force_refreshes(search_ctx):
    ctx, fake = search_ctx
    first = shop.ensure_brief(ctx.cfg, ctx.db, ctx.llm)
    _queue_search(fake, text="Different research the second time.")
    second = shop.ensure_brief(ctx.cfg, ctx.db, ctx.llm, force=True)
    assert second["id"] != first["id"] and len(fake.calls) == 2


def test_ensure_brief_refreshes_when_the_season_changed(search_ctx, monkeypatch):
    from datetime import date

    ctx, fake = search_ctx
    monkeypatch.setattr(shop, "today_date", lambda cfg: date(2026, 1, 15))     # winter
    winter = shop.ensure_brief(ctx.cfg, ctx.db, ctx.llm)
    _queue_search(fake, text="Summer research.")
    monkeypatch.setattr(shop, "today_date", lambda cfg: date(2026, 7, 15))     # summer
    summer = shop.ensure_brief(ctx.cfg, ctx.db, ctx.llm)
    assert winter["season"] == "winter" and summer["season"] == "summer" and summer["id"] != winter["id"]


def test_ensure_brief_refreshes_when_stale(search_ctx):
    ctx, fake = search_ctx
    brief = shop.ensure_brief(ctx.cfg, ctx.db, ctx.llm)
    stale_created = "2000-01-01T00:00:00"
    with ctx.db.conn() as c:
        c.execute("UPDATE shop_briefs SET created_at=? WHERE id=?", (stale_created, brief["id"]))
    _queue_search(fake, text="Fresh research after going stale.")
    refreshed = shop.ensure_brief(ctx.cfg, ctx.db, ctx.llm)
    assert refreshed["id"] != brief["id"]


# ------------------------------------------------------------------ the outfit advice (structured)
@pytest.fixture
def wardrobe(ctx):
    return dict(
        polo=add_item(ctx, 1, "top", "polo", formality=3),
        chinos=add_item(ctx, 2, "bottom", "chinos", formality=3),
        sneakers=add_item(ctx, 3, "footwear", "clean sneakers", formality=3),
    )


def _reply(wardrobe, buy=None):
    return {
        "reply": "A few pieces get you a lot more mileage.",
        "outfits": [{
            "name": "Polo & new overshirt", "owned_item_ids": [wardrobe["polo"], wardrobe["chinos"]],
            "buy": buy if buy is not None else [{
                "category": "outerwear", "description": "camel overshirt", "price_low": 50, "price_high": 90,
                "store_suggestion": "Zara or H&M", "why": "on trend this season, layers over the polo",
            }],
            "rationale": "Smart casual, on trend, reuses what he owns.",
        }],
    }


def test_advise_stores_a_brief_and_valid_outfits(ctx, fake, wardrobe):
    ctx.db.add_shop_brief("autumn", "Research.", [], 0, 0.0)
    fake.queue(_reply(wardrobe))
    result = shop.advise(ctx.cfg, ctx.db, ctx.llm, request="focus on outerwear")

    advice = ctx.db.get_shop_advice(result["advice_id"])
    assert advice["request"] == "focus on outerwear"
    assert len(advice["outfits"]) == 1
    o = advice["outfits"][0]
    assert o["owned_item_ids"] == [wardrobe["polo"], wardrobe["chinos"]]
    assert o["buy"][0]["description"] == "camel overshirt"
    assert result["gap"]["total_smart_casual_pieces"] == 3


def test_advise_drops_ids_that_do_not_exist_and_asks_claude_to_correct_it(ctx, fake, wardrobe):
    # A hallucinated id always triggers one retry (like the main advice flow does for a similar problem), even
    # when the outfit is still usable after dropping it -- so Claude is told about the mistake, not just patched
    # around silently.
    ctx.db.add_shop_brief("autumn", "Research.", [], 0, 0.0)
    bad = _reply(wardrobe)
    bad["outfits"][0]["owned_item_ids"] = [wardrobe["polo"], 999999]
    fake.queue(bad, _reply(wardrobe))

    result = shop.advise(ctx.cfg, ctx.db, ctx.llm)

    assert len(fake.calls) == 2
    assert "999999" in fake.calls[1]["messages"][-1]["content"]
    advice = ctx.db.get_shop_advice(result["advice_id"])
    assert advice["outfits"][0]["owned_item_ids"] == [wardrobe["polo"], wardrobe["chinos"]]


def test_advise_retries_once_when_an_outfit_ends_up_empty(ctx, fake, wardrobe):
    ctx.db.add_shop_brief("autumn", "Research.", [], 0, 0.0)
    bad = _reply(wardrobe, buy=[])
    bad["outfits"][0]["owned_item_ids"] = [999999]                # after filtering: nothing owned, nothing to buy
    fake.queue(bad, _reply(wardrobe))
    result = shop.advise(ctx.cfg, ctx.db, ctx.llm)
    advice = ctx.db.get_shop_advice(result["advice_id"])
    assert len(advice["outfits"]) == 1 and advice["outfits"][0]["buy"]
    assert len(fake.calls) == 2


def test_advise_an_all_buy_outfit_is_fine(ctx, fake, wardrobe):
    ctx.db.add_shop_brief("autumn", "Research.", [], 0, 0.0)
    reply = _reply(wardrobe)
    reply["outfits"][0]["owned_item_ids"] = []                    # 100% new: allowed
    fake.queue(reply)
    result = shop.advise(ctx.cfg, ctx.db, ctx.llm)
    advice = ctx.db.get_shop_advice(result["advice_id"])
    assert advice["outfits"][0]["owned_item_ids"] == [] and advice["outfits"][0]["buy"]


def test_advise_propagates_budget_and_llm_errors(ctx, fake, wardrobe, monkeypatch):
    monkeypatch.setattr(ctx.tracker, "check", lambda: (_ for _ in ()).throw(BudgetExceeded("no budget")))
    with pytest.raises(BudgetExceeded):
        shop.advise(ctx.cfg, ctx.db, ctx.llm)


def test_advise_uses_the_existing_brief_without_a_new_search(ctx, fake, wardrobe):
    ctx.db.add_shop_brief("autumn", "Existing research.", [], 0, 0.0)
    fake.queue(_reply(wardrobe))
    shop.advise(ctx.cfg, ctx.db, ctx.llm)
    assert "Existing research." in fake.calls[0]["messages"][0]["content"]


# ------------------------------------------------------------------ feedback shapes the next request
def test_feedback_is_included_in_the_next_advise_call(ctx, fake, wardrobe):
    ctx.db.add_shop_brief("autumn", "Research.", [], 0, 0.0)
    fake.queue(_reply(wardrobe))
    result = shop.advise(ctx.cfg, ctx.db, ctx.llm)
    ctx.db.set_shop_feedback(result["advice_id"], 0, "dislike", "too plain")

    fake.queue(_reply(wardrobe))
    shop.advise(ctx.cfg, ctx.db, ctx.llm)
    prompt = fake.calls[1]["messages"][0]["content"]
    assert "dislike" in prompt and "too plain" in prompt


# ------------------------------------------------------------------ advice_view (for the page)
def test_advice_view_expands_owned_items_and_carries_feedback(ctx, fake, wardrobe):
    ctx.db.add_shop_brief("autumn", "Research.", [], 0, 0.0)
    fake.queue(_reply(wardrobe))
    result = shop.advise(ctx.cfg, ctx.db, ctx.llm)
    ctx.db.set_shop_feedback(result["advice_id"], 0, "like", "")

    view = shop.advice_view(ctx.db, lambda ids: [{"id": i, "subtype": "x", "thumb": f"/img/thumbs/{i}.jpg"} for i in ids])
    assert view["id"] == result["advice_id"]
    assert len(view["outfits"][0]["owned"]) == 2
    assert view["outfits"][0]["feedback"] == {"verdict": "like", "comment": ""}


def test_advice_view_is_none_before_any_advice(ctx):
    assert shop.advice_view(ctx.db, lambda ids: []) is None
