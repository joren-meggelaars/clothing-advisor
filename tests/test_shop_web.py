"""The Shop page and its API: routes, auth scoping, settings form."""
import pytest
from fastapi.testclient import TestClient

from clothing_advisor.config import Config
from clothing_advisor.web import create_app

from .conftest import TOKEN, add_item
from .test_llm import FakeSearchAnthropic, block, usage


@pytest.fixture
def client(cfg, fake):
    app = create_app(cfg, fake, start_worker=False)
    with TestClient(app, base_url="http://testserver") as c:
        c.ctx = app.state.ctx
        yield c


HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def _brief(ctx):
    return ctx.db.add_shop_brief("autumn", "Camel and navy are trending.", [{"url": "https://zalando.nl", "title": "Zalando"}], 2, 0.02)


def test_shop_page_and_nav_link(client):
    page = client.get("/app", headers=HEADERS).text
    assert 'href="/app/shop"' in page
    shop_page = client.get("/app/shop", headers=HEADERS)
    assert shop_page.status_code == 200 and "shop.js" in shop_page.text


def test_current_with_nothing_yet(client):
    state = client.get("/api/shop/current", headers=HEADERS).json()
    assert state["advice"] is None and state["brief"] is None
    assert state["cost_profile"] == "budget" and state["budget_eur"] == 300


def test_advise_end_to_end_through_the_api(client, fake):
    add_item(client.ctx, 1, "top", "polo", formality=3)
    add_item(client.ctx, 2, "bottom", "chinos", formality=3)
    from PIL import Image
    Image.new("RGB", (100, 100)).save(client.ctx.cfg.thumbs_dir / "p1.jpg")
    _brief(client.ctx)
    fake.queue({"reply": "Two pieces, one new jacket.", "outfits": [
        {"name": "Polo & jacket", "owned_item_ids": [1], "buy": [
            {"category": "outerwear", "description": "navy overshirt", "price_low": 50, "price_high": 90,
             "store_suggestion": "Zara", "why": "on trend"}],
         "rationale": "Smart casual."}]})

    r = client.post("/api/shop/advise", json={"request": "focus on outerwear"}, headers=HEADERS)
    assert r.status_code == 200
    state = r.json()
    assert state["advice"]["reply"] == "Two pieces, one new jacket."
    outfit = state["advice"]["outfits"][0]
    assert outfit["owned"][0]["id"] == 1 and outfit["owned"][0]["thumb"].startswith("/img/thumbs/")
    assert outfit["buy"][0]["store_suggestion"] == "Zara"
    assert state["gap"]["total_smart_casual_pieces"] == 2

    again = client.get("/api/shop/current", headers=HEADERS).json()
    assert again["advice"]["id"] == state["advice"]["id"]


def test_advise_reports_budget_and_llm_errors_as_the_advice_route_does(client, monkeypatch):
    from clothing_advisor.usage import BudgetExceeded
    monkeypatch.setattr(client.ctx.tracker, "check", lambda: (_ for _ in ()).throw(BudgetExceeded("no budget left")))
    r = client.post("/api/shop/advise", json={}, headers=HEADERS)
    assert r.status_code == 402 and "no budget" in r.json()["detail"]


def test_feedback_round_trip_and_bad_input(client, fake):
    _brief(client.ctx)
    fake.queue({"reply": "ok", "outfits": [{"name": "A", "owned_item_ids": [], "buy": [
        {"category": "top", "description": "tee", "price_low": 10, "price_high": 20, "store_suggestion": "H&M", "why": "basic"}],
        "rationale": "r"}]})
    advice = client.post("/api/shop/advise", json={}, headers=HEADERS).json()["advice"]

    liked = client.post("/api/shop/feedback", json={"advice_id": advice["id"], "outfit_idx": 0, "verdict": "like"}, headers=HEADERS)
    assert liked.status_code == 200 and liked.json()["advice"]["outfits"][0]["feedback"]["verdict"] == "like"

    cleared = client.post("/api/shop/feedback", json={"advice_id": advice["id"], "outfit_idx": 0, "verdict": "clear"}, headers=HEADERS)
    assert cleared.json()["advice"]["outfits"][0]["feedback"] is None

    assert client.post("/api/shop/feedback", json={"advice_id": advice["id"], "outfit_idx": 0, "verdict": "meh"}, headers=HEADERS).status_code == 400
    assert client.post("/api/shop/feedback", json={"advice_id": advice["id"], "outfit_idx": 99, "verdict": "like"}, headers=HEADERS).status_code == 404
    assert client.post("/api/shop/feedback", json={"advice_id": 999999, "outfit_idx": 0, "verdict": "like"}, headers=HEADERS).status_code == 404


def test_settings_shop_page_shows_and_saves_values(client):
    page = client.get("/app/settings", headers=HEADERS).text
    assert 'id="shop"' in page and "Budget-friendly" in page and "Mid-range" in page

    r = client.post("/app/settings/shop", headers=HEADERS, follow_redirects=False, data={
        "cost_profile": "mid", "budget_eur": "450", "stores": "Zara, WE", "stores_note": "Skip Trendstore Uden.",
        "sizes": "M, waist 31",
    })
    assert r.status_code == 303

    from clothing_advisor import shop
    assert shop.cost_profile_key(client.ctx.db) == "mid"
    assert shop.budget_eur(client.ctx.db) == 450.0
    assert shop.preferred_stores(client.ctx.db) == "Zara, WE"
    assert shop.stores_note(client.ctx.db) == "Skip Trendstore Uden."
    assert shop.sizes(client.ctx.db) == "M, waist 31"

    page2 = client.get("/app/settings", headers=HEADERS).text
    assert 'value="mid" selected' in page2 and 'value="450"' in page2


def test_settings_shop_rejects_an_unknown_profile(client):
    r = client.post("/app/settings/shop", headers=HEADERS,
                    data={"cost_profile": "premium", "budget_eur": "300", "stores": "", "stores_note": "", "sizes": ""})
    assert r.status_code == 400


def test_settings_shop_clamps_budget(client):
    from clothing_advisor import shop
    client.post("/app/settings/shop", headers=HEADERS, data={"cost_profile": "budget", "budget_eur": "5", "stores": "", "stores_note": "", "sizes": ""})
    assert shop.budget_eur(client.ctx.db) == 20.0
    client.post("/app/settings/shop", headers=HEADERS, data={"cost_profile": "budget", "budget_eur": "999999", "stores": "", "stores_note": "", "sizes": ""})
    assert shop.budget_eur(client.ctx.db) == 5000.0


# ------------------------------------------------------------------ tokens/roles never see the shop pages
def test_tile_and_view_tokens_cannot_use_shop(cfg, fake, monkeypatch):
    monkeypatch.setenv("CA_TILE_TOKEN", "tile-token-0123456789abcdef")
    monkeypatch.setenv("CA_VIEW_TOKEN", "view-token-0123456789abcdefgh")
    app = create_app(Config.from_env(), fake, start_worker=False)
    with TestClient(app, base_url="http://testserver") as c:
        assert c.get("/app/shop?k=tile-token-0123456789abcdef").status_code == 403
        assert c.get("/api/shop/current?v=view-token-0123456789abcdefgh").status_code == 403
        assert c.post("/api/shop/advise?k=tile-token-0123456789abcdef", json={}).status_code == 403
