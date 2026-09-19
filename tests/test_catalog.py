import json

import pytest

from clothing_advisor import catalog
from clothing_advisor.imaging import ImageError

from .conftest import ATTRS, png_bytes


def test_ingest_dedupes_and_downsizes(ctx):
    raw = png_bytes(size=(3000, 2000))
    item_id, state = catalog.ingest_bytes(ctx.cfg, ctx.db, raw)
    assert state == "queued" and item_id
    again, state2 = catalog.ingest_bytes(ctx.cfg, ctx.db, raw)
    assert again is None and state2 == "duplicate"
    from PIL import Image
    item = ctx.db.get_item(item_id)
    with Image.open(ctx.cfg.photos_dir / item["photo"]) as im:
        assert max(im.size) == 1024
    with Image.open(ctx.cfg.thumbs_dir / item["thumb"]) as im:
        assert max(im.size) == 360


def test_ingest_rejects_non_images(ctx):
    with pytest.raises(ImageError):
        catalog.ingest_bytes(ctx.cfg, ctx.db, b"definitely not an image")


def test_catalogue_item_uses_vision_structured_output_and_logs_cost(ctx, fake):
    item_id, _ = catalog.ingest_bytes(ctx.cfg, ctx.db, png_bytes())
    fake.queue(ATTRS)
    item = ctx.db.claim_next_queued()
    catalog.catalogue_item(ctx.cfg, ctx.db, ctx.llm, item)

    stored = ctx.db.get_item(item_id)
    assert stored["category"] == "top" and stored["colors"] == ["navy"] and stored["ai_state"] == "done"
    assert stored["reviewed"] is False  # always needs the user's approval
    call = fake.calls[0]
    assert call["model"] == "claude-sonnet-5"
    assert call["thinking"] == {"type": "disabled"}
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["messages"][0]["content"][0]["type"] == "image"
    assert ctx.tracker.spend() > 0


def test_recatalogue_keeps_manual_edits(ctx, fake):
    item_id, _ = catalog.ingest_bytes(ctx.cfg, ctx.db, png_bytes())
    ctx.db.update_item(item_id, {"subtype": "my own name", "user_fields": ["subtype"]})
    fake.queue({**ATTRS, "subtype": "AI name", "colors": ["green"]})
    catalog.catalogue_item(ctx.cfg, ctx.db, ctx.llm, ctx.db.get_item(item_id))
    stored = ctx.db.get_item(item_id)
    assert stored["subtype"] == "my own name"  # protected
    assert stored["colors"] == ["green"]       # not edited by the user, so updated


def test_worker_pauses_instead_of_failing_when_budget_is_used_up(ctx, fake):
    item_id, _ = catalog.ingest_bytes(ctx.cfg, ctx.db, png_bytes())
    ctx.db.set_setting("budget_eur", "0.01")
    ctx.tracker.record("advice", "claude-sonnet-5", 0, 10_000)
    ctx.worker.process(ctx.db.claim_next_queued())
    assert ctx.db.get_item(item_id)["ai_state"] == "queued"
    assert "budget" in ctx.worker.paused_reason.lower()
    assert fake.calls == []


def test_inbox_import_moves_files(ctx):
    (ctx.cfg.inbox_dir / "a.png").write_bytes(png_bytes((1, 2, 3)))
    (ctx.cfg.inbox_dir / "b.png").write_bytes(png_bytes((9, 9, 9)))
    (ctx.cfg.inbox_dir / "notes.txt").write_text("ignore me")
    result = catalog.import_inbox(ctx.cfg, ctx.db)
    assert result == {"queued": 2, "duplicate": 0, "failed": 0}
    assert not (ctx.cfg.inbox_dir / "a.png").exists()
    assert (ctx.cfg.inbox_dir / "notes.txt").exists()


def test_estimate_uses_measured_average_after_five_calls(ctx):
    per, basis = catalog.estimate_per_photo_eur(ctx.cfg, ctx.db)
    assert "estimate" in basis and 0.001 < per < 0.05
    for _ in range(5):
        ctx.tracker.record("catalogue", "claude-sonnet-5", 2000, 300)
    per2, basis2 = catalog.estimate_per_photo_eur(ctx.cfg, ctx.db)
    assert "last 5" in basis2 and per2 == pytest.approx(ctx.db.avg_cost("catalogue")[0])
