import copy
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from clothing_advisor.config import Config
from clothing_advisor.context import build_ctx

TOKEN = "test-token-0123456789abcdef"


class FakeAnthropic:
    """Stands in for anthropic.Anthropic: returns queued JSON texts and records every request."""

    def __init__(self):
        self.calls: list[dict] = []
        self.texts: list = []
        self.usage = dict(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        self.messages = SimpleNamespace(create=self._create)

    def queue(self, *texts):
        self.texts.extend(t if isinstance(t, str) else json.dumps(t) for t in texts)

    def _create(self, **kw):
        self.calls.append(copy.deepcopy(kw))   # the app mutates its message list after the call
        text = self.texts.pop(0) if self.texts else "{}"
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                               usage=SimpleNamespace(**self.usage))


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("CA_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    for k in ("HA_URL", "HA_TOKEN", "HA_WEATHER_ENTITY", "HA_NOTIFY_SERVICE", "SMTP_HOST", "PUBLIC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


@pytest.fixture
def cfg(env):
    return Config.from_env()


@pytest.fixture
def fake():
    return FakeAnthropic()


@pytest.fixture
def ctx(cfg, fake):
    return build_ctx(cfg, fake)


def png_bytes(color=(120, 30, 30), size=(300, 400)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


ATTRS = dict(category="top", subtype="crew-neck t-shirt", colors=["navy"], pattern="solid", formality=2, warmth=1,
             seasons=["spring", "summer"], layer="base", description="plain cotton tee", confidence="high", issue="")


def add_item(ctx, n, category, subtype, *, layer="none", warmth=3, formality=2, seasons=("spring", "summer", "autumn", "winter"),
             colors=("navy",), status="clean", reviewed=True):
    """Insert a reviewed item straight into the database (no photo needed for stylist tests)."""
    item_id = ctx.db.add_item(f"sha{n}", f"p{n}.jpg", f"p{n}.jpg")
    ctx.db.update_item(item_id, {"category": category, "subtype": subtype, "colors": list(colors), "pattern": "solid",
                                 "formality": formality, "warmth": warmth, "seasons": list(seasons), "layer": layer,
                                 "description": "", "status": status, "reviewed": int(reviewed), "ai_state": "done"})
    return item_id
