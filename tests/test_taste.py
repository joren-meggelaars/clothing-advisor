from datetime import date, datetime, timedelta, timezone

import pytest

from clothing_advisor import stylist
from clothing_advisor.llm import LlmError
from clothing_advisor.taste import DISTILL_EVERY, disliked_sets, feedback_text, format_ratings

from .conftest import add_item

TODAY = date(2026, 7, 15)


@pytest.fixture
def closet(ctx, monkeypatch):
    monkeypatch.setattr(stylist, "today_date", lambda cfg: TODAY)
    return dict(
        polo=add_item(ctx, 1, "top", "polo", layer="base", colors=("navy",)),
        tee=add_item(ctx, 2, "top", "t-shirt", layer="base", colors=("white",)),
        chinos=add_item(ctx, 3, "bottom", "chinos", colors=("beige",)),
        jeans=add_item(ctx, 4, "bottom", "jeans", colors=("blue",)),
        shoes=add_item(ctx, 5, "footwear", "sneakers", colors=("white",)),
    )


def make_outfit(ctx, name, ids, status="proposed"):
    sid = ctx.db.latest_session()["id"] if ctx.db.latest_session() else ctx.db.new_session()
    oid = ctx.db.add_outfit(sid, name, ids, "", "")
    if status != "proposed":
        ctx.db.update_outfit(oid, status=status)
    return oid


def rate(ctx, oid, stars, context="suggestion", comment="", reasons=()):
    ctx.db.upsert_rating(oid, context, stars, comment, list(reasons))


# ------------------------------------------------------------------ storage and formatting
def test_rating_is_replaced_per_context_and_counts_as_new(ctx, closet):
    oid = make_outfit(ctx, "A", [closet["polo"], closet["chinos"], closet["shoes"]])
    rate(ctx, oid, 3)
    first_id = ctx.db.max_rating_id()
    rate(ctx, oid, 5, comment="better on second thought")
    assert ctx.db.rating_count() == 1 and ctx.db.max_rating_id() > first_id
    rate(ctx, oid, 4, context="worn")
    assert ctx.db.rating_count() == 2                                  # a worn rating is separate from the first impression
    assert {r["context"]: r["stars"] for r in ctx.db.ratings_for_outfit(oid)} == {"suggestion": 5, "worn": 4}


def test_format_ratings_describes_items_reasons_and_comment(ctx, closet):
    oid = make_outfit(ctx, "Polo & chinos", [closet["polo"], closet["chinos"], closet["shoes"]])
    rate(ctx, oid, 2, comment="te warm voor binnen", reasons=["too warm"])
    text = feedback_text(ctx.db)
    assert "[suggestion, 2/5; reasons: too warm; comment: \"te warm voor binnen\"]" in text
    assert "\"Polo & chinos\"" in text and "navy polo" in text and "beige chinos" in text
    ctx.db.delete_item(closet["polo"])                                 # deleted items must not crash the prompt
    assert "navy polo" not in feedback_text(ctx.db) and "beige chinos" in feedback_text(ctx.db)


def test_disliked_outfits_expire_and_only_low_ratings_count(ctx, closet):
    a, b, c = ([closet["polo"], closet["chinos"], closet["shoes"]], [closet["tee"], closet["jeans"], closet["shoes"]],
               [closet["tee"], closet["chinos"], closet["shoes"]])
    rate(ctx, make_outfit(ctx, "A", a), 1)
    rate(ctx, make_outfit(ctx, "B", b), 2)
    rate(ctx, make_outfit(ctx, "C", c), 4)
    assert set(disliked_sets(ctx.db)) == {frozenset(a), frozenset(b)}
    later = datetime.now(timezone.utc) + timedelta(days=120)
    assert disliked_sets(ctx.db, now=later) == []


# ------------------------------------------------------------------ used in advice
def test_advice_prompt_carries_taste_profile_feedback_and_blocks_disliked_outfits(ctx, fake, closet):
    disliked = [closet["polo"], closet["chinos"], closet["shoes"]]
    rate(ctx, make_outfit(ctx, "Old favourite", disliked), 1, comment="colours clash", reasons=["colours"])
    ctx.db.set_setting("taste_profile", "- dislikes navy with beige")
    ok = [closet["tee"], closet["jeans"], closet["shoes"]]
    fake.queue(
        {"reply": "a", "outfits": [{"name": "Repeat", "item_ids": disliked, "rationale": "x"}], "exclude_item_ids": []},
        {"reply": "b", "outfits": [{"name": "Fresh", "item_ids": ok, "rationale": "x"}], "exclude_item_ids": []},
    )
    result = stylist.Stylist(ctx.cfg, ctx.db, ctx.llm, ctx.weather).advise("casual", new_session=True)

    first = fake.calls[0]
    assert "Learned taste" in first["system"][0]["text"] and "dislikes navy with beige" in first["system"][0]["text"]
    user = first["messages"][-1]["content"]
    assert "His recent feedback" in user and "colours clash" in user
    assert "Never propose these outfits again" in user
    assert len(fake.calls) == 2 and "repeats an outfit worn recently" in fake.calls[1]["messages"][-1]["content"]
    assert [o["name"] for o in ctx.db.list_outfits(session_id=result["session_id"], statuses=("proposed",))] == ["Fresh"]


def test_no_feedback_no_extra_prompt_sections(ctx, fake, closet):
    fake.queue({"reply": "a", "outfits": [], "exclude_item_ids": []}, {"reply": "a", "outfits": [], "exclude_item_ids": []})
    stylist.Stylist(ctx.cfg, ctx.db, ctx.llm, ctx.weather).advise("casual", new_session=True)
    call = fake.calls[0]
    assert "Learned taste" not in call["system"][0]["text"]
    assert "recent feedback" not in call["messages"][-1]["content"]


# ------------------------------------------------------------------ distilling the profile
def _rate_many(ctx, closet, n):
    combos = [[closet["polo"], closet["chinos"], closet["shoes"]], [closet["tee"], closet["jeans"], closet["shoes"]]]
    for i in range(n):
        rate(ctx, make_outfit(ctx, f"O{i}", combos[i % 2]), 1 + i % 5, context="suggestion" if i < 5 else "worn")


def test_profile_updates_only_after_enough_new_ratings(ctx, fake, closet):
    _rate_many(ctx, closet, DISTILL_EVERY - 1)
    ctx.taste.maybe_distill()
    assert fake.calls == [] and ctx.taste.profile() == ""

    fake.queue({"profile": "- likes white sneakers with everything"})
    _rate_many(ctx, closet, 1)
    ctx.taste.maybe_distill()
    assert ctx.taste.profile() == "- likes white sneakers with everything"
    call = fake.calls[0]
    assert call["thinking"] == {"type": "adaptive"} and "Rated outfits" in call["messages"][0]["content"]
    assert "(empty)" in call["messages"][0]["content"]
    assert ctx.db.month_spend(ctx.tracker.ym()) > 0 and ctx.taste.status()["new"] == 0

    ctx.taste.maybe_distill()                                          # nothing new -> no second call
    assert len(fake.calls) == 1


def test_update_keeps_the_current_profile_as_input_and_failure_is_retried(ctx, fake, closet):
    ctx.db.set_setting("taste_profile", "- I wrote this myself")
    _rate_many(ctx, closet, DISTILL_EVERY)
    fake.texts.append("this is not json")                              # first attempt fails validation
    ctx.taste.maybe_distill()
    assert ctx.taste.profile() == "- I wrote this myself" and ctx.taste.status()["new"] == DISTILL_EVERY

    fake.queue({"profile": "- I wrote this myself\n- also likes jeans"})
    ctx.taste.maybe_distill()                                          # the next rating/attempt retries
    assert "I wrote this myself" in fake.calls[-1]["messages"][0]["content"]
    assert ctx.taste.profile().endswith("also likes jeans")


def test_force_update_reset_and_budget_stop(ctx, fake, closet):
    _rate_many(ctx, closet, 2)
    fake.queue({"profile": "- tentative: prefers jeans (tentative)"})
    assert ctx.taste.distill(force=True) is True
    assert ctx.taste.distill(force=True) is False                      # no new ratings

    ctx.taste.reset()
    assert ctx.taste.profile() == "" and ctx.taste.status()["new"] == 0

    _rate_many(ctx, closet, DISTILL_EVERY)
    ctx.db.set_setting("budget_eur", "0.001")
    ctx.tracker.record("advice", "claude-sonnet-5", 0, 10_000)
    calls = len(fake.calls)
    ctx.taste.maybe_distill()                                          # budget used up: skipped quietly
    assert len(fake.calls) == calls and ctx.taste.profile() == ""
    with pytest.raises(Exception):
        ctx.taste.distill()
