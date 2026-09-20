from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from clothing_advisor import stylist
from clothing_advisor.imaging import make_collage
from clothing_advisor.weather import Weather

from .conftest import add_item

TODAY = date(2026, 7, 15)


def at(h, m=0):
    return datetime(2026, 7, 15, h, m, tzinfo=ZoneInfo("Europe/Amsterdam"))


@pytest.fixture
def closet(ctx, monkeypatch):
    monkeypatch.setattr(stylist, "today_date", lambda cfg: TODAY)
    return dict(
        tee=add_item(ctx, 1, "top", "t-shirt", layer="base", warmth=1, seasons=("spring", "summer")),
        chinos=add_item(ctx, 2, "bottom", "chinos", warmth=2),
        shoes=add_item(ctx, 3, "footwear", "sneakers"),
    )


def outfit_reply(closet, name="Simple tee"):
    return {"reply": "Morning!", "outfits": [{"name": name, "item_ids": [closet["tee"], closet["chinos"], closet["shoes"]],
                                               "rationale": "easy"}], "exclude_item_ids": []}


# ------------------------------------------------------------------ when the job runs
def test_runs_once_after_the_set_time_and_prepares_a_session(ctx, fake, closet):
    fake.queue(outfit_reply(closet))
    assert ctx.auto.run_if_due(at(6, 0)) == "outside window"            # default time is 06:30
    assert fake.calls == []
    assert ctx.auto.run_if_due(at(6, 31)) == "done"
    assert len(fake.calls) == 1 and "Suggest my outfit for today." in fake.calls[0]["messages"][-1]["content"]
    session = stylist.current_session(ctx.db, ctx.cfg)
    assert [o["name"] for o in ctx.db.list_outfits(session_id=session["id"], statuses=("proposed",))] == ["Simple tee"]
    assert ctx.auto.run_if_due(at(7, 0)) == "done" and len(fake.calls) == 1     # once per day
    assert "done" in ctx.auto.status()


def test_custom_time_request_and_disabled(ctx, fake, closet):
    ctx.db.set_setting("auto_advice_time", "07:15")
    ctx.db.set_setting("auto_advice_request", "Something smart casual for the office.")
    assert ctx.auto.run_if_due(at(7, 0)) == "outside window"
    fake.queue(outfit_reply(closet))
    assert ctx.auto.run_if_due(at(7, 16)) == "done"
    assert "smart casual for the office" in fake.calls[0]["messages"][-1]["content"]

    ctx.db.set_setting("auto_advice", "0")
    assert ctx.auto.run_if_due(at(9, 0)) == "disabled"


def test_a_missed_run_is_not_made_up_late_in_the_day(ctx, fake, closet):
    assert ctx.auto.run_if_due(at(19, 0)) == "outside window" and fake.calls == []


def test_skipped_when_you_already_asked_or_picked_an_outfit(ctx, fake, closet):
    ctx.db.set_setting("last_user_advice", TODAY.isoformat())
    assert ctx.auto.run_if_due(at(7, 0)) == "skipped" and fake.calls == []

    ctx.db.set_setting("last_user_advice", "2026-07-14")               # yesterday does not count
    ctx.db.set_setting("auto_advice_state", "")
    sid = ctx.db.new_session()
    oid = ctx.db.add_outfit(sid, "Picked", [closet["tee"], closet["chinos"], closet["shoes"]], "", "")
    ctx.db.update_outfit(oid, status="chosen", chosen_on=TODAY.isoformat())
    assert ctx.auto.run_if_due(at(7, 0)) == "skipped" and fake.calls == []


def test_failures_are_retried_later_and_give_up_after_three(ctx, fake, closet):
    fake.texts.append("not json")
    assert ctx.auto.run_if_due(at(6, 31)) == "failed" and "failed" in ctx.auto.status()
    assert ctx.auto.run_if_due(at(6, 40)) == "waiting to retry"         # 15 minutes between attempts
    fake.texts.append("not json")
    assert ctx.auto.run_if_due(at(6, 50)) == "failed"
    fake.texts.append("not json")
    assert ctx.auto.run_if_due(at(7, 10)) == "failed"
    assert ctx.auto.run_if_due(at(8, 0)) == "gave up" and len(fake.calls) == 3

    ctx.db.set_setting("auto_advice_state", "")                         # next day starts fresh
    fake.queue(outfit_reply(closet))
    assert ctx.auto.run_now() == "done"


def test_retry_succeeds_and_budget_stops_the_job_without_calling_claude(ctx, fake, closet):
    fake.texts.append("not json")
    ctx.auto.run_if_due(at(6, 31))
    fake.queue(outfit_reply(closet))
    assert ctx.auto.run_if_due(at(6, 50)) == "done"

    ctx.db.set_setting("auto_advice_state", "")
    calls = len(fake.calls)
    ctx.db.set_setting("budget_eur", "0.001")
    ctx.tracker.record("advice", "claude-sonnet-5", 0, 10_000)
    assert ctx.auto.run_if_due(at(7, 0)) == "budget" and len(fake.calls) == calls
    assert "budget" in ctx.auto.status()


def test_wardrobe_not_ready_is_reported_not_crashing(ctx, fake):
    assert ctx.auto.run_if_due(at(6, 31)) == "failed"
    assert "not possible" in ctx.auto.status() and fake.calls == []


# ------------------------------------------------------------------ the morning session stays visible all day
def test_todays_session_stays_current_beyond_twelve_hours(ctx, closet, monkeypatch):
    sid = ctx.db.new_session()
    old = (datetime.now().astimezone() - timedelta(hours=14)).isoformat(timespec="seconds")
    with ctx.db.conn() as c:
        c.execute("UPDATE sessions SET created_at=?, updated_at=? WHERE id=?", (old, old, sid))
    created_day = datetime.fromisoformat(old).astimezone(ctx.cfg.tz).date()
    monkeypatch.setattr(stylist, "today_date", lambda cfg: created_day)
    assert stylist.current_session(ctx.db, ctx.cfg)["id"] == sid          # same day: still shown
    monkeypatch.setattr(stylist, "today_date", lambda cfg: created_day + timedelta(days=30))
    assert stylist.current_session(ctx.db, ctx.cfg) is None               # another day: gone


# ------------------------------------------------------------------ small helpers
def test_weather_short_line():
    w = {"t_now": 14.2, "condition": "partlycloudy", "t_low": 9.0, "t_high": 18.4, "rain_prob": 40}
    assert Weather.short(w) == "⛅ 14° · 9-18° · 40% rain"
    assert Weather.short({"t_now": 20, "condition": "sunny", "t_low": None, "t_high": None, "rain_prob": 10}) == "☀️ 20°"
    assert Weather.short(None) == ""


def test_square_collage_is_close_to_square_and_separate_from_the_wide_one(ctx):
    from PIL import Image
    for n in range(1, 4):
        Image.new("RGB", (200, 260), (n * 60, 80, 80)).save(ctx.cfg.photos_dir / f"p{n}.jpg")
    names = ["p1.jpg", "p2.jpg", "p3.jpg"]
    wide = make_collage(ctx.cfg, 7, names)
    square = make_collage(ctx.cfg, 7, names, square=True)
    assert wide.name == "o7.jpg" and square.name == "s7.jpg"
    with Image.open(wide) as w, Image.open(square) as s:
        assert w.width / w.height > 2.5                    # 3 items in one row
        assert 0.8 < s.width / s.height < 1.25             # 2x2 grid


# ------------------------------------------------------------------ regression: a failed call must not hide the suggestions
def test_a_failed_advice_call_does_not_replace_the_current_session(ctx, fake, closet):
    from clothing_advisor.llm import LlmError
    st = stylist.Stylist(ctx.cfg, ctx.db, ctx.llm, ctx.weather)
    fake.queue(outfit_reply(closet))
    first = st.advise("casual", new_session=True)["session_id"]
    fake.texts.append("this is not json")
    with pytest.raises(LlmError):
        st.advise("something else", new_session=True)
    assert stylist.current_session(ctx.db, ctx.cfg)["id"] == first       # still the good one
    assert len(ctx.db.list_outfits(session_id=first, statuses=("proposed",))) == 1
