from datetime import date

import pytest
from fastapi.testclient import TestClient

from clothing_advisor import stylist
from clothing_advisor.stylist import DEFAULT_WORK_DRESS, day_context, formality_filter
from clothing_advisor.web import create_app

from .conftest import TOKEN, add_item

WED = date(2026, 7, 15)     # a workday
SAT = date(2026, 7, 18)     # a day off


def at(monkeypatch, day):
    monkeypatch.setattr(stylist, "today_date", lambda cfg: day)


@pytest.fixture
def closet(ctx):
    return dict(
        polo=add_item(ctx, 1, "top", "polo", layer="base", formality=3),
        tee=add_item(ctx, 2, "top", "t-shirt", layer="base", formality=2),
        joggers=add_item(ctx, 3, "bottom", "joggers", formality=1),
        chinos=add_item(ctx, 4, "bottom", "chinos", formality=3),
        sneakers=add_item(ctx, 5, "footwear", "sneakers", formality=2),
    )


def reply(closet):
    return {"reply": "ok", "outfits": [{"name": "Polo & chinos", "item_ids": [closet["polo"], closet["chinos"], closet["sneakers"]],
                                        "rationale": "tidy"}], "exclude_item_ids": []}


def ask(ctx, fake, closet, **kw):
    fake.queue(reply(closet))
    stylist.Stylist(ctx.cfg, ctx.db, ctx.llm, ctx.weather).advise("", new_session=True, **kw)
    call = fake.calls[-1]
    return call["system"][1]["text"], call["messages"][-1]["content"]


# ------------------------------------------------------------------ what counts as a workday
def test_monday_to_friday_are_workdays_by_default(ctx):
    assert [day_context(ctx.db, date(2026, 7, 13 + i))["workday"] for i in range(7)] == [True] * 5 + [False] * 2
    ctx_day = day_context(ctx.db, WED)
    assert ctx_day["name"] == "Wednesday" and DEFAULT_WORK_DRESS in ctx_day["line"]


def test_ignore_workdays_turns_a_workday_into_a_day_off(ctx):
    ctx_day = day_context(ctx.db, WED, ignore_workdays=True)
    assert ctx_day["workday"] is False and "ignore workdays" in ctx_day["line"]
    assert day_context(ctx.db, SAT, ignore_workdays=True)["line"].startswith("Day type: day off (Saturday)")


def test_workdays_and_dress_code_can_be_changed(ctx):
    ctx.db.set_setting("work_days", "5")                               # only Saturday
    ctx.db.set_setting("work_dress", "Office: chinos and a shirt jacket.")
    assert day_context(ctx.db, SAT)["workday"] and not day_context(ctx.db, WED)["workday"]
    assert "Office: chinos" in day_context(ctx.db, SAT)["line"]
    ctx.db.set_setting("work_days", "")                                # a stored empty list means no workdays at all
    assert not day_context(ctx.db, WED)["workday"]


# ------------------------------------------------------------------ the formality filter
def item(i, cat, formality):
    return {"id": i, "category": cat, "formality": formality}


def test_formality_filter_drops_low_items_but_never_empties_a_slot():
    items = [item(1, "top", 1), item(2, "top", 3), item(3, "bottom", 2), item(4, "footwear", 2), item(5, "accessory", 1)]
    assert [i["id"] for i in formality_filter(items, 2)] == [2, 3, 4, 5]        # sportswear gone, accessory stays
    kept = [i["id"] for i in formality_filter(items, 3)]
    assert kept == [2, 3, 4, 5]                                                 # bottom and shoes step down: only formality-2 exist
    assert [i["id"] for i in formality_filter([item(1, "top", 1), item(2, "bottom", 1), item(3, "footwear", 1)], 3)] == [1, 2, 3]


# ------------------------------------------------------------------ in the advice request
def test_workday_advice_states_the_dress_code_and_leaves_out_sportswear(ctx, fake, closet, monkeypatch):
    at(monkeypatch, WED)
    catalogue, request = ask(ctx, fake, closet)
    assert "joggers" not in catalogue and "chinos" in catalogue
    assert "Day type: workday (Wednesday)" in request and "smart casual / casual chic" in request
    assert "Extra formal" not in request


def test_day_off_keeps_everything_and_asks_for_relaxed(ctx, fake, closet, monkeypatch):
    at(monkeypatch, SAT)
    catalogue, request = ask(ctx, fake, closet)
    assert "joggers" in catalogue
    assert "Day type: day off (Saturday)" in request


def test_ignore_workdays_on_a_weekday_behaves_like_a_day_off(ctx, fake, closet, monkeypatch):
    at(monkeypatch, WED)
    catalogue, request = ask(ctx, fake, closet, ignore_workdays=True)
    assert "joggers" in catalogue and "Day type: day off" in request and "ignore workdays" in request


def test_extra_formal_goes_one_notch_smarter(ctx, fake, closet, monkeypatch):
    at(monkeypatch, WED)
    catalogue, request = ask(ctx, fake, closet, extra_formal=True)
    assert "polo" in catalogue and "chinos" in catalogue
    assert "t-shirt" not in catalogue and "joggers" not in catalogue           # formality 2 and 1 are out
    assert "sneakers" in catalogue                                              # the only shoes: the slot is never emptied
    assert "Extra formal requested" in request and "no formal dress shoes" in request


# ------------------------------------------------------------------ pages and API
@pytest.fixture
def client(cfg, fake, monkeypatch):
    at(monkeypatch, WED)
    app = create_app(cfg, fake, start_worker=False)
    with TestClient(app, base_url="http://testserver") as c:
        c.ctx = app.state.ctx
        yield c


def test_advice_page_shows_the_two_ticks_and_what_today_is(client):
    page = client.get("/app", headers={"Authorization": f"Bearer {TOKEN}"}).text
    assert 'id="ignorework"' in page and 'id="formal"' in page
    assert "Today is a workday (Wednesday)" in page


def test_api_passes_the_ticks_to_the_stylist(client, fake, closet):
    h = {"Authorization": f"Bearer {TOKEN}"}
    fake.queue(reply(closet))
    r = client.post("/api/advice", headers=h, json={"message": "", "new_session": True, "ignore_workdays": True,
                                                    "extra_formal": True})
    assert r.status_code == 200
    request = fake.calls[0]["messages"][-1]["content"]
    assert "Day type: day off" in request and "Extra formal requested" in request


def test_settings_save_workdays_and_dress_code(client):
    h = {"Authorization": f"Bearer {TOKEN}"}
    assert "Workdays" in client.get("/app/settings", headers=h).text
    r = client.post("/app/settings/work", headers=h, follow_redirects=False,
                    data={"days": ["0", "1", "9", "x"], "work_dress": " Chinos and a shirt jacket. "})
    assert r.status_code == 303
    assert stylist.work_days(client.ctx.db) == {0, 1}                            # invalid values ignored
    assert stylist.work_dress(client.ctx.db) == "Chinos and a shirt jacket."
    client.post("/app/settings/work", headers=h, data={"work_dress": DEFAULT_WORK_DRESS}, follow_redirects=False)
    assert stylist.work_days(client.ctx.db) == set()                             # nothing ticked: no workdays
    assert client.ctx.db.get_setting("work_dress") == ""                         # the default is not stored as an override
