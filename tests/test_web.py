import re

import pytest
from fastapi.testclient import TestClient

from clothing_advisor import stylist
from clothing_advisor.imaging import sign
from clothing_advisor.web import create_app

from .conftest import ATTRS, TOKEN, add_item, png_bytes


@pytest.fixture
def client(cfg, fake, monkeypatch):
    from datetime import date
    monkeypatch.setattr(stylist, "today_date", lambda cfg: date(2026, 7, 15))
    app = create_app(cfg, fake, start_worker=False)
    with TestClient(app, base_url="http://testserver") as c:
        c.ctx = app.state.ctx
        yield c


def test_requires_token(client):
    assert client.get("/app").status_code == 401
    assert client.get("/api/tv").status_code == 401
    assert client.get("/healthz").status_code == 200
    assert client.get("/app?t=wrong").status_code == 401


def test_token_in_query_sets_cookie_and_keeps_links_working_without_cookies(client):
    r = client.get(f"/app?t={TOKEN}")
    assert r.status_code == 200 and "ca_auth" in r.cookies
    assert f"t={TOKEN}" in r.text                    # links carry the token (cookie may be blocked in an iframe)
    r2 = client.get("/app")                           # cookie now present
    assert r2.status_code == 200 and f"t={TOKEN}" not in r2.text
    assert client.get("/api/queue", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_upload_estimate_and_queue(client):
    h = {"Authorization": f"Bearer {TOKEN}"}
    r = client.post("/api/upload", files={"file": ("a.png", png_bytes(), "image/png")}, headers=h)
    assert r.json()["state"] == "queued"
    assert client.post("/api/upload", files={"file": ("a.png", png_bytes(), "image/png")}, headers=h).json()["state"] == "duplicate"
    assert client.post("/api/upload", files={"file": ("x.txt", b"nope", "text/plain")}, headers=h).status_code == 400
    assert client.get("/api/estimate?n=10", headers=h).json()["total_eur"] > 0
    assert client.get("/api/queue", headers=h).json()["queued"] == 1


def test_review_edit_and_approve_flow(client):
    h = {"Authorization": f"Bearer {TOKEN}"}
    client.post("/api/upload", files={"file": ("a.png", png_bytes(), "image/png")}, headers=h)
    item = client.ctx.db.claim_next_queued()
    client.ctx.llm._client.queue(ATTRS)
    from clothing_advisor import catalog
    catalog.catalogue_item(client.ctx.cfg, client.ctx.db, client.ctx.llm, item)

    page = client.get("/app/review", headers=h)
    assert page.status_code == 200 and "crew-neck t-shirt" in page.text
    r = client.post(f"/app/item/{item['id']}/update", headers=h, follow_redirects=False,
                    data={"category": "top", "subtype": "navy tee", "colors": "Navy, white", "pattern": "solid",
                          "formality": "2", "warmth": "1", "layer": "base", "seasons": ["summer"], "description": "d",
                          "action": "approve", "back": "/app/review"})
    assert r.status_code == 303
    stored = client.ctx.db.get_item(item["id"])
    assert stored["reviewed"] and stored["subtype"] == "navy tee" and stored["colors"] == ["navy", "white"]
    assert set(stored["user_fields"]) == {"subtype", "colors", "seasons", "description"}   # what the user changed
    assert client.get(f"/app/item/{item['id']}", headers=h).status_code == 200
    assert client.get("/app/wardrobe", headers=h).status_code == 200


def _wardrobe(ctx):
    from PIL import Image
    ids = {}
    for n, (cat, sub, layer, warmth) in enumerate([("top", "tee", "base", 1), ("bottom", "chinos", "none", 2),
                                                   ("footwear", "sneakers", "none", 3)], start=1):
        ids[cat] = add_item(ctx, n, cat, sub, layer=layer, warmth=warmth)
        Image.new("RGB", (200, 200), (n * 50, 90, 90)).save(ctx.cfg.photos_dir / f"p{n}.jpg")
        Image.new("RGB", (100, 100), (n * 50, 90, 90)).save(ctx.cfg.thumbs_dir / f"p{n}.jpg")
    return ids


def test_advice_choose_tv_feed_and_signed_images(client, fake):
    h = {"Authorization": f"Bearer {TOKEN}"}
    w = _wardrobe(client.ctx)
    fake.queue({"reply": "Simple and fresh.", "outfits": [
        {"name": "Tee & chinos", "item_ids": [w["top"], w["bottom"], w["footwear"]], "rationale": "easy"}],
        "exclude_item_ids": []})
    r = client.post("/api/advice", json={"message": "something casual", "new_session": True}, headers=h)
    assert r.status_code == 200
    state = r.json()
    assert state["reply"] == "Simple and fresh." and len(state["outfits"]) == 1
    outfit = state["outfits"][0]
    assert len(outfit["items"]) == 3 and outfit["image"].startswith("/img/collages/o")
    assert client.get(outfit["image"], headers=h).status_code == 200

    tv = client.get("/api/tv", headers=h).json()
    assert tv["state"] == "1 suggestions" and tv["outfits"][0]["image"].startswith("http://testserver/img/collages/")
    assert tv["budget_eur"] == 10.0 and tv["request"] == "something casual"
    signed = tv["outfits"][0]["image"].replace("http://testserver", "")
    assert client.get(signed).status_code == 200                                   # signature, no token
    assert client.get(signed.split("?")[0]).status_code == 401                     # no signature
    assert client.get(signed.split("?")[0] + "?sig=" + sign(client.ctx.cfg, "collages", "o999.jpg")).status_code == 401

    chosen = client.post(f"/api/outfits/{outfit['id']}/choose", headers=h).json()
    assert chosen["today"]["name"] == "Tee & chinos"
    assert client.get("/api/tv", headers=h).json()["state"] == "Today: Tee & chinos"
    worn = client.post(f"/api/outfits/{outfit['id']}/worn", headers=h).json()
    assert worn["today"]["status"] == "worn"
    assert client.ctx.db.get_item(w["top"])["status"] == "laundry"                 # tee: 1 wear


def test_budget_exceeded_returns_402_and_page_shows_banner(client, fake):
    h = {"Authorization": f"Bearer {TOKEN}"}
    _wardrobe(client.ctx)
    client.ctx.db.set_setting("budget_eur", "0.01")
    client.ctx.tracker.record("advice", "claude-sonnet-5", 0, 10_000)
    r = client.post("/api/advice", json={"message": "casual"}, headers=h)
    assert r.status_code == 402 and "budget" in r.json()["detail"].lower()
    assert fake.calls == []
    assert "budget used up" in client.get("/app", headers=h).text
    client.post("/app/costs/budget", data={"budget": "20"}, headers=h, follow_redirects=False)
    assert "budget used up" not in client.get("/app", headers=h).text


def test_all_pages_render(client):
    h = {"Authorization": f"Bearer {TOKEN}"}
    for path in ("/app", "/app/wardrobe", "/app/add", "/app/review", "/app/laundry", "/app/costs", "/app/settings"):
        r = client.get(path, headers=h)
        assert r.status_code == 200, path
    assert re.search(r"Estimated cost per photo", client.get("/app/add", headers=h).text)
