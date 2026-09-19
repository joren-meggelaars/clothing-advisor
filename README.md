# Clothing Advisor

An AI stylist for Home Assistant. Photograph your wardrobe once; Claude catalogues every item, then builds complete,
well-matched outfits on request ("something casual", "lighter", "not those trousers"), taking the weather from your
Home Assistant weather entity into account.

- **Phone**: the full app inside a Home Assistant dashboard (iframe card): chat, pick an outfit, add photos, laundry.
- **TV kiosk**: a read-only tile showing today's outfit or the current suggestions, fed by a REST sensor.
- **One small container** (FastAPI + SQLite + photos in a bind mount). No database, queue or scheduler service.

```
Phone (HA app) ──iframe──┐                 ┌── Anthropic API (vision + structured outputs)
TV kiosk ── REST sensor ─┼─ NPM (https) ──► clothing-advisor ──► Home Assistant API (weather, push alerts)
                         │                 └── ./data: SQLite + photos
```

## How it works

1. **Catalogue**: uploaded photos are downscaled (1024 px), queued and read by Claude (`CATALOG_MODEL`, vision +
   JSON-schema structured output): category, type, colours, pattern, formality 1-5, warmth 1-5, seasons, layer.
   Items land in a *Review* list; only approved items are used for advice. Manual corrections are never overwritten.
2. **Pre-filter**: before every request the catalogue is filtered for the day's weather (or the season when there is no
   weather): no winter coat in summer, no shorts in winter. Items in the wash are excluded.
3. **Advice** (`STYLIST_MODEL`, adaptive thinking, `STYLIST_EFFORT`): Claude returns N outfits as lists of item ids plus a
   short rationale. The server validates every outfit (one bottom, one pair of shoes, sensible layers, only available
   items, no repeat of an outfit worn in the last 7 days except a second consecutive day) and retries once on errors.
4. **Wear tracking**: choosing an outfit makes it "today's outfit"; it counts as worn at the end of the day. Items go
   to the wash after t-shirt 1x, sweater 3x, trousers 4x, outerwear 20x (shoes/accessories never).

## Cost control

Every Claude call is logged with tokens and an EUR estimate (Costs page). `MONTHLY_BUDGET_EUR` (default 10) has
alerts at `BUDGET_ALERT_PCTS` (default 80% and 100%), delivered as a Home Assistant push notification
(`HA_NOTIFY_SERVICE`) and/or e-mail (`SMTP_*`), plus a banner in the app. With `BUDGET_MODE=enforce` new Claude
calls stop at 100% until you raise the budget on the Costs page. Bulk uploads show an estimate first. Figures are
token x list price estimates; the Anthropic console is the source of truth.

## Configuration

Everything is set in `.env` (see [.env.example](.env.example)). Style profile, weather entity, notify service and number
of outfits can also be changed in the app under *Settings*.

## Development

```bash
uv sync
uv run pytest
CA_ACCESS_TOKEN=dev-token-0123456789 ANTHROPIC_API_KEY=... \
  uv run uvicorn clothing_advisor.web:create_app --factory --reload --port 8080
# open http://localhost:8080/app?t=dev-token-0123456789
```

## Deploying

See [DEPLOY.md](DEPLOY.md) and the Home Assistant examples in [ha/](ha/).
