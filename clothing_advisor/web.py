"""FastAPI app: mobile web UI (server-rendered), JSON API and the Home Assistant feed."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import BackgroundTasks, Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import catalog, imaging, stylist
from .config import Config
from .context import Ctx, build_ctx
from .db import EDITABLE_ITEM_FIELDS
from .llm import LlmError
from .schema import CATEGORIES, LAYERS, PATTERNS, SEASONS
from .taste import DISTILL_EVERY, RATING_REASONS
from .usage import BudgetExceeded
from .weather import Weather

log = logging.getLogger("clothing_advisor")
BASE = Path(__file__).parent
SLOT_ORDER = {"outerwear": 0, "top": 1, "bottom": 2, "footwear": 3, "accessory": 4}
PUBLIC_PREFIXES = ("/healthz", "/static/", "/img/", "/app/login")
# What the tile token may do: show and act on the suggestions, nothing else (no upload, wardrobe, settings, costs).
TILE_ROUTES = (
    ("GET", re.compile(r"^/app/tile$")),
    ("GET", re.compile(r"^/api/advice/current$")),
    ("POST", re.compile(r"^/api/advice$")),
    ("POST", re.compile(r"^/api/outfits/\d+/(choose|worn|rate)$")),
    ("GET", re.compile(r"^/img/(collages|thumbs)/[A-Za-z0-9_.-]+$")),
)


# The view token is read-only: it shows the suggestions and cannot change anything. It also feeds the Home Assistant sensor.
VIEW_ROUTES = (
    ("GET", re.compile(r"^/app/tile$")),
    ("GET", re.compile(r"^/api/advice/current$")),
    ("GET", re.compile(r"^/api/tv$")),
    ("GET", re.compile(r"^/img/(collages|thumbs)/[A-Za-z0-9_.-]+$")),
)


def route_allowed(routes, method: str, path: str) -> bool:
    return any(m == method and rx.match(path) for m, rx in routes)


NAV = (("Advice", "/app"), ("Wardrobe", "/app/wardrobe"), ("Add", "/app/add"), ("Review", "/app/review"),
       ("Laundry", "/app/laundry"), ("Costs", "/app/costs"), ("Settings", "/app/settings"))


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def create_app(cfg: Config | None = None, client: Any | None = None, start_worker: bool = True) -> FastAPI:
    cfg = cfg or Config.from_env()
    ctx: Ctx = build_ctx(cfg, client)
    https = cfg.public_base_url.startswith("https")

    async def auto_loop() -> None:
        while True:
            try:
                await asyncio.to_thread(ctx.auto.run_if_due)
            except Exception:  # keep the loop alive whatever happens
                log.exception("morning suggestion loop error")
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        auto_task = None
        if start_worker:
            ctx.worker.start()
            auto_task = asyncio.create_task(auto_loop())
        yield
        if start_worker:
            auto_task.cancel()
            await asyncio.gather(auto_task, return_exceptions=True)
            await ctx.worker.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    templates = Jinja2Templates(directory=str(BASE / "templates"))
    templates.env.globals.update(CATEGORIES=CATEGORIES, PATTERNS=PATTERNS, SEASONS=SEASONS, LAYERS=LAYERS)
    # Version stamp of the static files: changes whenever one of them changes, so browsers (and the Home Assistant app)
    # fetch the new script instead of reusing an old cached copy.
    digest = hashlib.sha1()
    for f in sorted((BASE / "static").glob("*")):
        digest.update(f.name.encode() + f.read_bytes())
    asset_version = digest.hexdigest()[:10]
    templates.env.globals["asset"] = lambda name: f"/static/{name}?v={asset_version}"

    # ------------------------------------------------------------------ auth & helpers
    def auth_source(request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer ") and _eq(header[7:].strip(), cfg.access_token):
            return "header"
        cookie = request.cookies.get("ca_auth")
        if cookie and _eq(cookie, cfg.access_token):
            return "cookie"
        q = request.query_params.get("t")
        if q and _eq(q, cfg.access_token):
            return "query"
        return None

    def url(request: Request, path: str) -> str:
        """Internal link; carries the token when the browser did not accept our cookie (iframe/Safari)."""
        sep = "&" if "?" in path else "?"
        if getattr(request.state, "via_query", False):
            return f"{path}{sep}t={quote(cfg.access_token)}"
        if getattr(request.state, "via_tile", False):
            return f"{path}{sep}k={quote(cfg.tile_token)}"
        if getattr(request.state, "via_view", False):
            return f"{path}{sep}v={quote(cfg.view_token)}"
        return path

    def scoped_token(request: Request):
        """Which scope-limited token (if any) the request carries: (name, allowed routes)."""
        header = request.headers.get("authorization", "")
        bearer = header[7:].strip() if header.lower().startswith("bearer ") else ""
        for name, token, routes, param in (("tile", cfg.tile_token, TILE_ROUTES, "k"), ("view", cfg.view_token, VIEW_ROUTES, "v")):
            if not token:
                continue
            given = [request.query_params.get(param) or ""]
            if name == "view":
                given.append(bearer)          # the HA sensor sends it as a Bearer header
            if any(g and _eq(g, token) for g in given):
                return name, routes
        return None

    def set_auth_cookie(response) -> None:
        response.set_cookie("ca_auth", cfg.access_token, max_age=365 * 86400, httponly=True,
                            secure=https, samesite="none" if https else "lax")

    failed_logins: list[float] = []      # timestamps of wrong tokens: crude brute-force brake (the token is 192 bits anyway)

    def login_blocked() -> bool:
        cutoff = time.time() - 600
        failed_logins[:] = [t for t in failed_logins if t > cutoff]
        return len(failed_logins) >= 20

    def safe_next(value: str) -> str:
        """Only paths inside the app; never an external address."""
        return value if value.startswith("/app") and not value.startswith("//") and "\\" not in value else "/app"

    def login_page(request: Request, error: str = "", nxt: str = "/app", status_code: int = 200):
        return templates.TemplateResponse(request, "login.html", {"error": error, "next": nxt}, status_code=status_code)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        source = auth_source(request)
        path = request.url.path
        request.state.via_tile = request.state.via_view = False
        if source is None:
            scope = scoped_token(request)
            if scope:
                name, routes = scope
                if not route_allowed(routes, request.method, path):
                    if path.startswith("/api/"):
                        return JSONResponse({"detail": "This token is not allowed to do that"}, status_code=403)
                    return templates.TemplateResponse(request, "unauthorized.html", {}, status_code=403)
                source = name
                request.state.via_tile, request.state.via_view = name == "tile", name == "view"
        request.state.authed = source is not None
        request.state.via_query = source == "query"
        if source is None and not path.startswith(PUBLIC_PREFIXES):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            if request.method == "GET" and path.startswith("/app") and path != "/app/tile":
                return RedirectResponse(f"/app/login?next={quote(path)}", status_code=303)   # a sign-in form, not a dead end
            return templates.TemplateResponse(request, "unauthorized.html", {}, status_code=401)
        response = await call_next(request)
        if source == "query" and response.status_code < 400:
            set_auth_cookie(response)
        if path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"      # always revalidate (cheap: ETag)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if cfg.ha_origin:
            response.headers["Content-Security-Policy"] = f"frame-ancestors 'self' {cfg.ha_origin}"
        return response

    def render(request: Request, name: str, status_code: int = 200, **kw: Any):
        kw.update(u=lambda p: url(request, p), token=cfg.access_token if request.state.via_query else "",
                  tile_key=cfg.tile_token if request.state.via_tile else "",
                  view_key=cfg.view_token if request.state.via_view else "", readonly=request.state.via_view,
                  full_url="/app" if (request.state.via_tile or request.state.via_view) else url(request, "/app"), nav=NAV, cost=ctx.tracker.summary(), msg=request.query_params.get("msg", ""))
        return templates.TemplateResponse(request, name, kw, status_code=status_code)

    def redirect(request: Request, path: str, msg: str = "") -> RedirectResponse:
        if msg:
            path += f"{'&' if '?' in path else '?'}msg={quote(msg)}"
        return RedirectResponse(url(request, path), status_code=303)

    def today() -> str:
        return stylist.today_date(cfg).isoformat()

    def rating_of(o: dict[str, Any]) -> dict[str, Any] | None:
        """The rating that belongs to the outfit's current stage: worn outfits show the 'worn' rating."""
        wanted = "worn" if o["status"] == "worn" else "suggestion"
        for r in ctx.db.ratings_for_outfit(o["id"]):
            if r["context"] == wanted:
                return {"stars": r["stars"], "comment": r["comment"], "reasons": r["reasons"]}
        return None

    def outfit_view(request: Request, o: dict[str, Any], *, tv: bool = False) -> dict[str, Any]:
        items = sorted(ctx.db.items_by_ids(o["item_ids"]).values(),
                       key=lambda i: (SLOT_ORDER.get(i["category"], 9), i["id"]))
        imaging.make_collage(cfg, o["id"], [i["photo"] for i in items])
        cname = f"o{o['id']}.jpg"
        if tv:
            base = cfg.public_base_url or str(request.base_url).rstrip("/")
            image = f"{base}/img/collages/{cname}?sig={imaging.sign(cfg, 'collages', cname)}"
        else:
            image = url(request, f"/img/collages/{cname}")
        view = {"id": o["id"], "name": o["name"], "reason": o["reason"], "status": o["status"], "image": image,
                "items_text": " · ".join(f"{' '.join(i['colors'][:1])} {i['subtype']}".strip() for i in items)}
        if not tv:
            imaging.make_collage(cfg, o["id"], [i["photo"] for i in items], square=True)
            view["image_square"] = url(request, f"/img/collages/s{o['id']}.jpg")
            view["rating"] = rating_of(o)
            view["items"] = [{"id": i["id"], "subtype": i["subtype"], "thumb": url(request, f"/img/thumbs/{i['thumb']}")}
                             for i in items]
        return view

    def today_outfit(request: Request, tv: bool = False) -> dict[str, Any] | None:
        for o in reversed(ctx.db.list_outfits(statuses=("chosen", "worn"), since=today())):
            if (o["worn_on"] or o["chosen_on"]) == today():
                return outfit_view(request, o, tv=tv)
        return None

    def advice_state(request: Request) -> dict[str, Any]:
        stylist.sync_worn(ctx.db, cfg)
        session = stylist.current_session(ctx.db, cfg)
        outfits, reply = [], ""
        if session:
            outfits = [outfit_view(request, o) for o in
                       ctx.db.list_outfits(session_id=session["id"], statuses=("proposed", "chosen"))]
            last = [m for m in ctx.db.get_messages(session["id"]) if m["role"] == "assistant"]
            reply = json.loads(last[-1]["content"]).get("reply", "") if last else ""
        prepared = ""
        if session:
            prepared = datetime.fromisoformat(session["created_at"]).astimezone(cfg.tz).strftime("%H:%M")
        return {"session_id": session["id"] if session else None, "reply": reply, "outfits": outfits,
                "today": today_outfit(request), "weather_short": Weather.short(ctx.weather.get()),
                "prepared_at": prepared, "auto": ctx.auto.enabled()}

    # ------------------------------------------------------------------ health, images
    @app.get("/healthz")
    def healthz():
        ctx.db.get_setting("x")
        return {"ok": True}

    @app.get("/img/{kind}/{name}")
    def image(kind: str, name: str, request: Request, sig: str | None = None):
        if not (request.state.authed or imaging.verify(cfg, kind, name, sig)):
            raise HTTPException(401, "Unauthorized")
        path = imaging.resolve(cfg, kind, name)
        if path is None:
            raise HTTPException(404, "Not found")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    # ------------------------------------------------------------------ pages
    @app.get("/")
    def root(request: Request):
        return redirect(request, "/app")

    @app.get("/app/login")
    def login_form(request: Request, next: str = "/app"):
        nxt = safe_next(next)
        if request.state.authed:
            return RedirectResponse(nxt, status_code=303)
        return login_page(request, nxt=nxt)

    @app.post("/app/login")
    def login_submit(request: Request, token: str = Form(""), next: str = Form("/app")):
        nxt = safe_next(next)
        if login_blocked():
            return login_page(request, "Too many wrong attempts. Try again in a few minutes.", nxt, 429)
        if token and _eq(token.strip(), cfg.access_token):
            response = RedirectResponse(nxt, status_code=303)
            set_auth_cookie(response)
            return response
        failed_logins.append(time.time())
        time.sleep(1)                                                       # slows guessing down
        return login_page(request, "That is not the right access token.", nxt, 401)

    @app.get("/app")
    def page_advice(request: Request):
        ready, not_ready_reason = stylist.wardrobe_readiness(ctx.db)
        day = stylist.day_context(ctx.db, stylist.today_date(cfg))
        return render(request, "advice.html", active="/app", ready=ready, not_ready_reason=not_ready_reason,
                      weather_on=bool(ctx.weather.entity()), n=stylist.outfit_count(cfg, ctx.db),
                      day_name=day["name"], workday=day["workday"])

    @app.get("/app/tile")
    def page_tile(request: Request):
        """Compact, self-scaling view for the Home Assistant tile (no menu, advice is already prepared)."""
        return render(request, "tile.html", auto_on=ctx.auto.enabled(), auto_time=ctx.auto.at().strftime("%H:%M"),
                      auto_request=ctx.auto.request())

    @app.get("/app/wardrobe")
    def page_wardrobe(request: Request, category: str = "", status: str = ""):
        items = [i for i in ctx.db.list_items(category=category or None, status=status or None) if i["ai_state"] == "done"]
        return render(request, "wardrobe.html", active="/app/wardrobe", items=items, category=category, status=status)

    @app.get("/app/item/{item_id}")
    def page_item(item_id: int, request: Request):
        item = ctx.db.get_item(item_id)
        if not item:
            raise HTTPException(404, "No such item")
        per_photo, _ = catalog.estimate_per_photo_eur(cfg, ctx.db)
        return render(request, "item.html", active="/app/wardrobe", item=item, per_photo=per_photo)

    def _parse_item_form(existing: dict[str, Any], form: dict[str, Any]) -> dict[str, Any]:
        def num(v: str, default: int) -> int:
            return int(v) if v.isdigit() and 1 <= int(v) <= 5 else default
        colors = [c.strip().lower() for c in form["colors"].split(",") if c.strip()][:3]
        seasons = [s for s in form["seasons"] if s in SEASONS]
        fields = {
            "category": form["category"] if form["category"] in CATEGORIES else existing["category"],
            "subtype": form["subtype"].strip()[:80],
            "colors": colors,
            "pattern": form["pattern"] if form["pattern"] in PATTERNS else existing["pattern"],
            "formality": num(form["formality"], existing["formality"] or 3),
            "warmth": num(form["warmth"], existing["warmth"] or 3),
            "seasons": seasons,
            "layer": form["layer"] if form["layer"] in LAYERS else existing["layer"],
            "description": form["description"].strip()[:200],
        }
        return fields

    @app.post("/app/item/{item_id}/update")
    def item_update(item_id: int, request: Request, category: str = Form(""), subtype: str = Form(""),
                    colors: str = Form(""), pattern: str = Form(""), formality: str = Form(""),
                    warmth: str = Form(""), seasons: list[str] = Form(default=[]), layer: str = Form(""),
                    description: str = Form(""), action: str = Form("save"), back: str = Form("/app/wardrobe")):
        item = ctx.db.get_item(item_id)
        if not item:
            raise HTTPException(404, "No such item")
        fields = _parse_item_form(item, dict(category=category, subtype=subtype, colors=colors, pattern=pattern,
                                             formality=formality, warmth=warmth, seasons=seasons, layer=layer,
                                             description=description))
        changed = [k for k in EDITABLE_ITEM_FIELDS if fields[k] != item[k]]
        if changed:
            fields["user_fields"] = sorted(set(item["user_fields"]) | set(changed))
        if action == "approve":
            fields["reviewed"] = 1
        ctx.db.update_item(item_id, fields)
        return redirect(request, back if back.startswith("/app") else "/app/wardrobe",
                        "Approved" if action == "approve" else "Saved")

    @app.post("/app/item/{item_id}/status")
    def item_status(item_id: int, request: Request, status: str = Form(...)):
        if status not in ("clean", "laundry", "retired"):
            raise HTTPException(400, "Bad status")
        fields: dict[str, Any] = {"status": status}
        if status == "clean":
            fields["wears_since_wash"] = 0
        ctx.db.update_item(item_id, fields)
        return redirect(request, f"/app/item/{item_id}", f"Marked {status}")

    @app.post("/app/item/{item_id}/delete")
    def item_delete(item_id: int, request: Request):
        item = ctx.db.delete_item(item_id)
        if item:
            for d, name in ((cfg.photos_dir, item["photo"]), (cfg.thumbs_dir, item["thumb"])):
                (d / name).unlink(missing_ok=True)
        return redirect(request, "/app/wardrobe", "Item deleted")

    @app.post("/app/item/{item_id}/recatalogue")
    def item_recatalogue(item_id: int, request: Request):
        ctx.db.update_item(item_id, {"ai_state": "queued", "ai_error": ""})
        return redirect(request, f"/app/item/{item_id}", "Queued for re-cataloguing (your manual edits are kept)")

    @app.get("/app/add")
    def page_add(request: Request):
        per_photo, basis = catalog.estimate_per_photo_eur(cfg, ctx.db)
        return render(request, "add.html", active="/app/add", per_photo=per_photo, basis=basis,
                      inbox=len(catalog.inbox_files(cfg)), counts=ctx.db.counts())

    @app.get("/app/review")
    def page_review(request: Request):
        pending = [i for i in ctx.db.list_items(reviewed=False, ai_state="done")]
        other = [i for i in ctx.db.list_items(reviewed=False) if i["ai_state"] != "done"]
        return render(request, "review.html", active="/app/review", items=pending[:20], total=len(pending), other=other)

    @app.post("/app/review/approve-all")
    def review_approve_all(request: Request):
        n = 0
        for i in ctx.db.list_items(reviewed=False, ai_state="done"):
            ctx.db.update_item(i["id"], {"reviewed": 1})
            n += 1
        return redirect(request, "/app/review", f"Approved {n} items")

    @app.get("/app/laundry")
    def page_laundry(request: Request):
        return render(request, "laundry.html", active="/app/laundry", items=ctx.db.list_items(status="laundry"))

    @app.post("/app/laundry/clean")
    def laundry_clean(request: Request, ids: list[int] = Form(default=[]), all_items: str = Form("")):
        targets = [i["id"] for i in ctx.db.list_items(status="laundry")] if all_items else ids
        stylist.mark_clean(ctx.db, targets)
        return redirect(request, "/app/laundry", f"{len(targets)} items are clean again")

    @app.get("/app/costs")
    def page_costs(request: Request):
        ym = ctx.tracker.ym()
        cat_avg, cat_n = ctx.db.avg_cost("catalogue")
        adv_avg, adv_n = ctx.db.avg_cost("advice")
        return render(request, "costs.html", active="/app/costs", by_purpose=ctx.db.usage_by_purpose(ym),
                      recent=ctx.db.recent_usage(15), cat_avg=cat_avg, cat_n=cat_n, adv_avg=adv_avg, adv_n=adv_n,
                      cfg=cfg)

    @app.post("/app/costs/budget")
    def costs_budget(request: Request, budget: float = Form(...)):
        if not 0 < budget <= 1000:
            raise HTTPException(400, "Budget must be between 0 and 1000")
        ctx.db.set_setting("budget_eur", f"{budget:.2f}")
        return redirect(request, "/app/costs", f"Monthly budget set to EUR {budget:.2f}")

    @app.post("/app/costs/test-notify")
    def costs_test_notify(request: Request):
        if not ctx.notifier.channels():
            return redirect(request, "/app/costs", "No notification channel is configured (see .env)")
        ok, errors = ctx.notifier.send("Clothing Advisor test", "Budget alerts will arrive like this.")
        return redirect(request, "/app/costs", ("Sent via " + ", ".join(ok) if ok else "") + (" | " + "; ".join(errors) if errors else ""))

    @app.get("/app/settings")
    def page_settings(request: Request):
        return render(request, "settings.html", active="/app/settings", profile=stylist.style_profile(ctx.db),
                      weather_entity=ctx.weather.entity(), notify_service=ctx.notifier.ha_service(),
                      outfit_n=stylist.outfit_count(cfg, ctx.db), ha_ok=bool(cfg.ha_url and cfg.ha_token),
                      channels=ctx.notifier.channels(), taste_profile=ctx.taste.profile(),
                      taste=ctx.taste.status(), distill_every=DISTILL_EVERY, auto_on=ctx.auto.enabled(),
                      auto_time=ctx.auto.at().strftime("%H:%M"), auto_request=ctx.auto.request(),
                      auto_status=ctx.auto.status(), work_days=stylist.work_days(ctx.db),
                      work_dress=stylist.work_dress(ctx.db), default_work_dress=stylist.DEFAULT_WORK_DRESS)

    @app.post("/app/settings/work")
    def settings_work(request: Request, days: list[str] = Form(default=[]), work_dress: str = Form("")):
        valid = sorted({int(d) for d in days if d.isdigit() and 0 <= int(d) <= 6})
        ctx.db.set_setting("work_days", ",".join(str(d) for d in valid))     # may be empty: no workdays at all
        text = work_dress.strip()[:500]
        ctx.db.set_setting("work_dress", "" if text == stylist.DEFAULT_WORK_DRESS else text)
        return redirect(request, "/app/settings", "Workdays saved")

    @app.post("/app/settings")
    def settings_save(request: Request, profile: str = Form(""), weather_entity: str = Form(""),
                      notify_service: str = Form(""), outfit_n: int = Form(3)):
        ctx.db.set_setting("style_profile", profile.strip()[:2000] or stylist.DEFAULT_STYLE_PROFILE)
        ctx.db.set_setting("weather_entity", weather_entity.strip())
        ctx.db.set_setting("notify_service", notify_service.strip())
        ctx.db.set_setting("outfit_count", str(min(max(outfit_n, 1), 6)))
        return redirect(request, "/app/settings", "Settings saved")

    @app.post("/app/settings/auto")
    def settings_auto(request: Request, auto_on: str = Form(""), auto_time: str = Form("06:30"),
                      auto_request: str = Form("")):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", auto_time):
            raise HTTPException(400, "Time must look like 06:30")
        ctx.db.set_setting("auto_advice", "1" if auto_on else "0")
        ctx.db.set_setting("auto_advice_time", auto_time)
        ctx.db.set_setting("auto_advice_request", auto_request.strip()[:300])
        return redirect(request, "/app/settings", "Morning suggestion saved")

    @app.post("/app/settings/auto-run")
    def settings_auto_run(request: Request):
        ctx.auto.run_now()
        return redirect(request, "/app/settings", f"Morning suggestion: {ctx.auto.status()}")

    @app.post("/app/settings/taste")
    def settings_taste(request: Request, taste_profile: str = Form(""), action: str = Form("save")):
        if action == "reset":
            ctx.taste.reset()
            return redirect(request, "/app/settings", "Taste profile cleared")
        if action == "update":
            try:
                done = ctx.taste.distill(force=True)
            except (LlmError, BudgetExceeded) as e:
                return redirect(request, "/app/settings", f"Could not update: {e}")
            return redirect(request, "/app/settings",
                            "Taste profile updated from your ratings" if done else "No new ratings since the last update")
        ctx.db.set_setting("taste_profile", taste_profile.strip()[:2000])
        return redirect(request, "/app/settings", "Taste profile saved")

    @app.post("/app/settings/test-weather")
    def settings_test_weather(request: Request):
        w = ctx.weather.get(refresh=True)
        msg = w["text"] if w and w.get("text") else f"No weather: {ctx.weather.last_error or 'entity/HA not configured'}"
        return redirect(request, "/app/settings", msg)

    # ------------------------------------------------------------------ API
    @app.post("/api/upload")
    async def api_upload(file: UploadFile = File(...)):
        raw = await file.read()
        try:
            item_id, state = await asyncio.to_thread(catalog.ingest_bytes, cfg, ctx.db, raw)
        except imaging.ImageError as e:
            log.warning("upload rejected (%s, %d bytes): %s", file.filename, len(raw), e)
            raise HTTPException(400, str(e))
        return {"item_id": item_id, "state": state}

    @app.post("/api/inbox/import")
    def api_inbox_import():
        return catalog.import_inbox(cfg, ctx.db)

    @app.get("/api/estimate")
    def api_estimate(n: int = 1):
        per, basis = catalog.estimate_per_photo_eur(cfg, ctx.db)
        return {"n": n, "per_photo_eur": per, "total_eur": per * n, "basis": basis,
                "budget_left_eur": max(ctx.tracker.budget() - ctx.tracker.spend(), 0)}

    @app.get("/api/queue")
    def api_queue():
        return {**ctx.db.counts(), "paused": ctx.worker.paused_reason, "failed_items": ctx.db.failed_items()}

    @app.get("/api/advice/current")
    def api_advice_current(request: Request):
        return advice_state(request)

    @app.post("/api/advice")
    def api_advice(request: Request, payload: dict[str, Any]):
        try:
            ctx.stylist.advise(str(payload.get("message", "")), new_session=bool(payload.get("new_session")),
                               exclude_ids=[int(i) for i in payload.get("exclude_ids", [])],
                               ignore_weather=bool(payload.get("ignore_weather")),
                               ignore_workdays=bool(payload.get("ignore_workdays")),
                               extra_formal=bool(payload.get("extra_formal")))
        except ValueError as e:
            raise HTTPException(400, str(e))
        except BudgetExceeded as e:
            raise HTTPException(402, str(e))
        except LlmError as e:
            raise HTTPException(502, str(e))
        ctx.db.set_setting("last_user_advice", today())
        return advice_state(request)

    def _outfit_or_404(outfit_id: int) -> dict[str, Any]:
        o = ctx.db.get_outfit(outfit_id)
        if not o:
            raise HTTPException(404, "No such outfit")
        return o

    @app.post("/api/outfits/{outfit_id}/choose")
    def api_choose(outfit_id: int, request: Request):
        _outfit_or_404(outfit_id)
        stylist.choose_outfit(ctx.db, cfg, outfit_id)
        return advice_state(request)

    @app.post("/api/outfits/{outfit_id}/worn")
    def api_worn(outfit_id: int, request: Request):
        _outfit_or_404(outfit_id)
        stylist.wear_outfit(ctx.db, outfit_id, today())
        return advice_state(request)

    def _clean_feedback(payload: dict[str, Any] | None) -> tuple[str, list[str]]:
        payload = payload or {}
        comment = str(payload.get("comment", "")).strip()[:500]
        reasons = [r for r in payload.get("reasons", []) if r in RATING_REASONS]
        return comment, reasons

    @app.post("/api/outfits/{outfit_id}/rate")
    def api_rate(outfit_id: int, request: Request, background: BackgroundTasks,
                 payload: dict[str, Any] = Body(...)):
        o = _outfit_or_404(outfit_id)
        try:
            stars = int(payload.get("stars", 0))
        except (TypeError, ValueError):
            stars = 0
        if not 1 <= stars <= 5:
            raise HTTPException(400, "Rate between 1 and 5 stars")
        comment, reasons = _clean_feedback(payload)
        ctx.db.upsert_rating(outfit_id, "worn" if o["status"] == "worn" else "suggestion", stars, comment, reasons)
        background.add_task(ctx.taste.maybe_distill)
        return advice_state(request)

    @app.post("/api/outfits/{outfit_id}/reject")
    def api_reject(outfit_id: int, request: Request, background: BackgroundTasks,
                   payload: dict[str, Any] | None = Body(default=None)):
        _outfit_or_404(outfit_id)
        comment, reasons = _clean_feedback(payload)
        ctx.db.upsert_rating(outfit_id, "suggestion", 1, comment, reasons)   # "Not this" is a 1-star first impression
        ctx.db.update_outfit(outfit_id, status="rejected")
        background.add_task(ctx.taste.maybe_distill)
        return advice_state(request)

    @app.get("/api/tv")
    def api_tv(request: Request):
        """Feed for the Home Assistant REST sensor (Bearer auth); image URLs are signed for the TV browser."""
        stylist.sync_worn(ctx.db, cfg)
        session = stylist.current_session(ctx.db, cfg)
        outfits, last_request = [], ""
        if session:
            outfits = [outfit_view(request, o, tv=True) for o in
                       ctx.db.list_outfits(session_id=session["id"], statuses=("proposed", "chosen"))]
            users = [m for m in ctx.db.get_messages(session["id"]) if m["role"] == "user"]
            last_request = users[-1]["content"] if users else ""
        todays = today_outfit(request, tv=True)
        weather = ctx.weather.get()
        summary = ctx.tracker.summary()
        state = f"Today: {todays['name']}" if todays else (f"{len(outfits)} suggestions" if outfits else "No suggestions")
        return {"state": state, "request": last_request, "today_outfit": todays, "outfits": outfits,
                "weather": weather["text"] if weather and weather.get("text") else "",
                "updated_at": datetime.now(cfg.tz).isoformat(timespec="seconds"),
                "month_cost_eur": round(summary["spend_eur"], 2), "budget_eur": round(summary["budget_eur"], 2),
                "budget_pct": round(summary["pct"])}

    return app
