"""Photo intake and AI cataloguing (vision + structured output), including the background worker."""
from __future__ import annotations

import asyncio
import base64
import logging
import shutil
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database
from .imaging import ImageError, process_upload
from .llm import LlmClient, LlmError
from .schema import ItemAttributes
from .usage import BudgetExceeded, cost_usd

log = logging.getLogger(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
WORKERS = 2

CATALOG_SYSTEM = """You catalogue one item of a man's personal wardrobe from a single photo (a garment, a pair of shoes or an accessory). Describe only what is visible; do not guess brand or size.

Fields:
- category: top (t-shirt, shirt, polo, knit, sweater, hoodie, cardigan), bottom (jeans, chinos, trousers, shorts, joggers), outerwear (jacket, coat, gilet worn as outer layer), footwear, accessory (belt, scarf, cap, gloves, bag, watch, ...).
- subtype: short specific name, e.g. "crew-neck t-shirt", "slim chinos", "chelsea boots", "puffer jacket".
- colors: main colour first, at most 3, plain English names (black, white, grey, navy, blue, light blue, green, olive, khaki, beige, cream, brown, tan, red, burgundy, orange, yellow, pink, purple, denim).
- pattern: solid, striped, checked, printed, textured, colorblock or other.
- formality: 1 = sport/loungewear, 2 = everyday casual (t-shirt, sweatshirt, jeans, sneakers), 3 = smart casual / casual chic (polo, fine knit, chinos, clean leather sneakers, suede boots), 4 = business casual (unstructured blazer level), 5 = formal (suit, dress shirt, dress trousers, dress shoes).
- warmth: 1 = very light (summer tee, shorts, sandals), 2 = light (long-sleeve thin, light jacket, light chinos), 3 = medium (sweater, standard jeans, mid-weight jacket, sneakers/boots), 4 = warm (thick knit, padded or lined jacket, lined boots), 5 = very warm (insulated winter coat).
- seasons: every season the item is sensible to wear in (spring, summer, autumn, winter).
- layer: base = worn as the first/only top layer (t-shirt, shirt, polo); mid = worn over a base layer (knit, sweater, hoodie, cardigan); outer = jacket or coat; none = bottoms, footwear, accessories.
- description: at most 15 words of concrete visible detail (fit, neckline, texture, details).
- confidence: how sure you are of the attributes given photo quality.
- issue: empty string if the photo is fine; otherwise one short sentence (e.g. "several garments in the photo", "item is folded, colour hard to judge", "not a clothing item")."""


def _load(item: dict[str, Any], cfg: Config) -> bytes:
    return (cfg.photos_dir / item["photo"]).read_bytes()


def catalogue_item(cfg: Config, db: Database, llm: LlmClient, item: dict[str, Any]) -> ItemAttributes:
    b64 = base64.standard_b64encode(_load(item, cfg)).decode()
    attrs = llm.structured(
        purpose="catalogue",
        model=cfg.catalog_model,
        system=CATALOG_SYSTEM,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": "Catalogue this item."},
        ]}],
        out_model=ItemAttributes,
        max_tokens=800,
        thinking="off",  # plain extraction; reasoning tokens would only add cost
    )
    apply_attributes(db, item, attrs)
    return attrs


def apply_attributes(db: Database, item: dict[str, Any], attrs: ItemAttributes) -> None:
    """Store AI output, but never overwrite a field the user corrected by hand."""
    protected = set(item.get("user_fields", []))
    values: dict[str, Any] = {
        "category": attrs.category, "subtype": attrs.subtype.strip(),
        "colors": [c.strip().lower() for c in attrs.colors][:3], "pattern": attrs.pattern,
        "formality": int(attrs.formality), "warmth": int(attrs.warmth),
        "seasons": list(dict.fromkeys(attrs.seasons)), "layer": attrs.layer,
        "description": attrs.description.strip(),
    }
    fields = {k: v for k, v in values.items() if k not in protected}
    fields.update(ai_state="done", ai_error="", ai_issue=attrs.issue.strip(), ai_confidence=attrs.confidence)
    db.update_item(item["id"], fields)


def ingest_bytes(cfg: Config, db: Database, raw: bytes) -> tuple[int | None, str]:
    """Store one photo and queue it for cataloguing. Returns (item_id, 'queued' | 'duplicate')."""
    saved = process_upload(cfg, raw)
    item_id = db.add_item(saved["sha"], saved["photo"], saved["thumb"])
    return (item_id, "queued") if item_id else (None, "duplicate")


def inbox_files(cfg: Config) -> list[Path]:
    if not cfg.inbox_dir.is_dir():
        return []
    return sorted(p for p in cfg.inbox_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def import_inbox(cfg: Config, db: Database) -> dict[str, int]:
    done_dir = cfg.inbox_dir / "_imported"
    done_dir.mkdir(exist_ok=True)
    result = {"queued": 0, "duplicate": 0, "failed": 0}
    for path in inbox_files(cfg):
        try:
            _, state = ingest_bytes(cfg, db, path.read_bytes())
            result[state] += 1
            shutil.move(str(path), done_dir / path.name)
        except (ImageError, OSError) as e:
            log.warning("inbox file %s skipped: %s", path.name, e)
            result["failed"] += 1
    return result


def estimate_per_photo_eur(cfg: Config, db: Database) -> tuple[float, str]:
    """Measured average when we have data, otherwise a token-based estimate."""
    avg, n = db.avg_cost("catalogue")
    if n >= 5:
        return avg, f"average of the last {n} catalogued photos"
    # ~1,900 input tokens (1024px image + prompt) and ~300 output tokens per photo.
    return cost_usd(cfg.catalog_model, 1900, 300) * cfg.usd_eur, "token-based estimate"


class CatalogWorker:
    """Processes queued photos in the background, a couple at a time."""

    def __init__(self, cfg: Config, db: Database, llm: LlmClient):
        self.cfg, self.db, self.llm = cfg, db, llm
        self.paused_reason = ""
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self.db.reset_processing()
        self._tasks = [asyncio.create_task(self._loop()) for _ in range(WORKERS)]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            item = await asyncio.to_thread(self.db.claim_next_queued)
            if item is None:
                await asyncio.sleep(2)
                continue
            await asyncio.to_thread(self.process, item)
            if self.paused_reason:
                await asyncio.sleep(60)

    def process(self, item: dict[str, Any]) -> None:
        try:
            catalogue_item(self.cfg, self.db, self.llm, item)
            self.paused_reason = ""
        except BudgetExceeded as e:
            self.db.update_item(item["id"], {"ai_state": "queued"})
            self.paused_reason = str(e)
        except LlmError as e:
            log.warning("cataloguing item %s failed: %s", item["id"], e)
            self.db.update_item(item["id"], {"ai_state": "error", "ai_error": str(e)})
        except Exception as e:  # never let one bad photo kill the worker
            log.exception("unexpected cataloguing error for item %s", item["id"])
            self.db.update_item(item["id"], {"ai_state": "error", "ai_error": f"unexpected error: {e}"})
