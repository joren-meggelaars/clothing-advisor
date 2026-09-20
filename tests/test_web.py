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


def test_queue_reports_why_items_failed(client):
    h = {"Authorization": f"Bearer {TOKEN}"}
    item_id = client.ctx.db.add_item("shafail", "f.jpg", "f.jpg")
    client.ctx.db.update_item(item_id, {"ai_state": "error", "ai_error": "Claude declined this request."})
    q = client.get("/api/queue", headers=h).json()
    assert q["error"] == 1 and q["failed_items"] == [{"id": item_id, "error": "Claude declined this request."}]
    bad = client.post("/api/upload", files={"file": ("notes.txt", b"nope", "text/plain")}, headers=h)
    assert bad.status_code == 400 and "readable image" in bad.json()["detail"]


# ------------------------------------------------------------------ ratings through the API and the settings page
def _proposed_outfit(client, fake):
    h = {"Authorization": f"Bearer {TOKEN}"}
    w = _wardrobe(client.ctx)
    fake.queue({"reply": "ok", "outfits": [
        {"name": "Tee & chinos", "item_ids": [w["top"], w["bottom"], w["footwear"]], "rationale": "easy"}],
        "exclude_item_ids": []})
    state = client.post("/api/advice", json={"message": "casual", "new_session": True}, headers=h).json()
    return h, state["outfits"][0]["id"]


def test_rate_endpoint_stores_stage_specific_ratings(client, fake):
    h, oid = _proposed_outfit(client, fake)
    assert client.post(f"/api/outfits/{oid}/rate", json={"stars": 0}, headers=h).status_code == 400
    assert client.post(f"/api/outfits/{oid}/rate", json={"stars": "x"}, headers=h).status_code == 400
    assert client.post("/api/outfits/999/rate", json={"stars": 3}, headers=h).status_code == 404

    state = client.post(f"/api/outfits/{oid}/rate", headers=h,
                        json={"stars": 4, "comment": "  nice  ", "reasons": ["colours", "bogus"]}).json()
    assert state["outfits"][0]["rating"] == {"stars": 4, "comment": "nice", "reasons": ["colours"]}

    client.post(f"/api/outfits/{oid}/choose", headers=h)
    worn = client.post(f"/api/outfits/{oid}/worn", headers=h).json()
    assert worn["today"]["rating"] is None                             # the first impression is not the worn rating
    worn = client.post(f"/api/outfits/{oid}/rate", json={"stars": 5}, headers=h).json()
    assert worn["today"]["rating"]["stars"] == 5
    assert {r["context"]: r["stars"] for r in client.ctx.db.ratings_for_outfit(oid)} == {"suggestion": 4, "worn": 5}


def test_not_this_records_a_one_star_rating_with_reasons(client, fake):
    h, oid = _proposed_outfit(client, fake)
    state = client.post(f"/api/outfits/{oid}/reject", json={"reasons": ["too warm"], "comment": "too heavy"}, headers=h).json()
    assert state["outfits"] == []
    [r] = client.ctx.db.ratings_for_outfit(oid)
    assert (r["stars"], r["reasons"], r["comment"], r["context"]) == (1, ["too warm"], "too heavy", "suggestion")
    assert client.post(f"/api/outfits/{oid}/reject", headers=h).status_code == 200   # body is optional


def test_tenth_rating_refreshes_the_taste_profile_in_the_background(client, fake):
    h, oid = _proposed_outfit(client, fake)
    db = client.ctx.db   # ten distinct rated outfits: nine seeded directly, the tenth through the API
    ids = [oid] + [db.add_outfit(db.latest_session()["id"], f"X{i}", db.get_outfit(oid)["item_ids"], "", "") for i in range(9)]
    for i, o in enumerate(ids[:-1]):
        db.upsert_rating(o, "suggestion", 3, "", [])
    fake.queue({"profile": "- likes simple tees"})
    client.post(f"/api/outfits/{ids[-1]}/rate", json={"stars": 5}, headers=h)   # the 10th rating triggers it
    assert client.ctx.taste.profile() == "- likes simple tees"


def test_settings_page_taste_actions(client, fake):
    h = {"Authorization": f"Bearer {TOKEN}"}
    page = client.get("/app/settings", headers=h)
    assert page.status_code == 200 and "Learned taste" in page.text
    client.post("/app/settings/taste", data={"taste_profile": "- my own line", "action": "save"}, headers=h, follow_redirects=False)
    assert client.ctx.taste.profile() == "- my own line" and "my own line" in client.get("/app/settings", headers=h).text
    r = client.post("/app/settings/taste", data={"action": "update"}, headers=h, follow_redirects=False)
    assert "No%20new%20ratings" in r.headers["location"] and fake.calls == []
    client.post("/app/settings/taste", data={"action": "reset"}, headers=h, follow_redirects=False)
    assert client.ctx.taste.profile() == ""


# ------------------------------------------------------------------ tile view and morning suggestion in the web app
def test_tile_page_and_state_for_the_tile(client, fake):
    h, oid = _proposed_outfit(client, fake)
    page = client.get("/app/tile", headers=h)
    assert page.status_code == 200 and "tile.js" in page.text
    state = client.get("/api/advice/current", headers=h).json()
    assert state["prepared_at"] and state["auto"] is True and "weather_short" in state
    assert state["outfits"][0]["image_square"].startswith("/img/collages/s")
    assert client.get(state["outfits"][0]["image_square"], headers=h).status_code == 200


def test_asking_yourself_marks_the_day_so_the_morning_job_skips(client, fake):
    _proposed_outfit(client, fake)
    from clothing_advisor import stylist as st
    assert client.ctx.db.get_setting("last_user_advice") == st.today_date(client.ctx.cfg).isoformat()


def test_settings_for_the_morning_suggestion(client, fake):
    h = {"Authorization": f"Bearer {TOKEN}"}
    assert "Morning suggestion" in client.get("/app/settings", headers=h).text
    assert client.post("/app/settings/auto", data={"auto_time": "25:99"}, headers=h).status_code == 400
    r = client.post("/app/settings/auto", headers=h, follow_redirects=False,
                    data={"auto_on": "1", "auto_time": "07:05", "auto_request": " smart casual please "})
    assert r.status_code == 303
    auto = client.ctx.auto
    assert auto.enabled() and auto.at().strftime("%H:%M") == "07:05" and auto.request() == "smart casual please"
    client.post("/app/settings/auto", data={"auto_time": "06:30"}, headers=h, follow_redirects=False)   # box unticked
    assert not auto.enabled()
    r = client.post("/app/settings/auto-run", headers=h, follow_redirects=False)
    assert r.status_code == 303 and "not possible" in auto.status()          # empty wardrobe: reported, no crash
