"""Photo processing, outfit collages and signed image URLs."""
from __future__ import annotations

import hashlib
import hmac
import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import Config

Image.MAX_IMAGE_PIXELS = 60_000_000
PHOTO_MAX = 1024
THUMB_MAX = 360
TILE = 320
PAD = 12
BG = (244, 241, 236)
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class ImageError(Exception):
    pass


def _flatten(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB")


def process_upload(cfg: Config, raw: bytes) -> dict[str, str]:
    """Validate, orient, downscale and store a photo plus thumbnail. Returns file names and content hash."""
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ImageError("Photo is larger than 25 MB")
    sha = hashlib.sha256(raw).hexdigest()
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise ImageError("Not a readable image (JPEG/PNG/WebP expected)") from e
    img = _flatten(ImageOps.exif_transpose(img))

    name = f"{sha[:20]}.jpg"
    photo = img.copy()
    photo.thumbnail((PHOTO_MAX, PHOTO_MAX))
    photo.save(cfg.photos_dir / name, "JPEG", quality=82, optimize=True)
    thumb = img.copy()
    thumb.thumbnail((THUMB_MAX, THUMB_MAX))
    thumb.save(cfg.thumbs_dir / name, "JPEG", quality=78, optimize=True)
    return {"sha": sha, "photo": name, "thumb": name}


def make_collage(cfg: Config, outfit_id: int, thumbs: list[str]) -> Path:
    """Compose the item thumbnails of one outfit into a single JPEG (cached on disk)."""
    path = cfg.collages_dir / f"o{outfit_id}.jpg"
    if path.exists():
        return path
    n = max(len(thumbs), 1)
    cols = n if n <= 3 else 3
    rows = -(-n // cols)
    canvas = Image.new("RGB", (cols * TILE + (cols + 1) * PAD, rows * TILE + (rows + 1) * PAD), BG)
    for i, name in enumerate(thumbs):
        try:
            tile = Image.open(cfg.photos_dir / name).convert("RGB")
        except (OSError, UnidentifiedImageError):
            continue
        tile.thumbnail((TILE, TILE))
        r, c = divmod(i, cols)
        x = PAD + c * (TILE + PAD) + (TILE - tile.width) // 2
        y = PAD + r * (TILE + PAD) + (TILE - tile.height) // 2
        canvas.paste(tile, (x, y))
    canvas.save(path, "JPEG", quality=80, optimize=True)
    return path


def _key(cfg: Config) -> bytes:
    return hashlib.sha256(b"img:" + cfg.access_token.encode()).digest()


def sign(cfg: Config, kind: str, name: str) -> str:
    return hmac.new(_key(cfg), f"{kind}/{name}".encode(), hashlib.sha256).hexdigest()[:32]


def verify(cfg: Config, kind: str, name: str, sig: str | None) -> bool:
    return bool(sig) and hmac.compare_digest(sign(cfg, kind, name), sig)


def resolve(cfg: Config, kind: str, name: str) -> Path | None:
    base: Any = {"photos": cfg.photos_dir, "thumbs": cfg.thumbs_dir, "collages": cfg.collages_dir}.get(kind)
    if base is None or not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    p = (base / name).resolve()
    return p if p.parent == base.resolve() and p.is_file() else None
