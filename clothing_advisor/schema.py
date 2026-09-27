"""Shared vocabularies and the structured-output models Claude fills in."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

CATEGORIES = ("top", "bottom", "outerwear", "footwear", "accessory")
PATTERNS = ("solid", "striped", "checked", "printed", "textured", "colorblock", "other")
SEASONS = ("spring", "summer", "autumn", "winter")
LAYERS = ("base", "mid", "outer", "none")
STATUSES = ("clean", "laundry", "retired")

Category = Literal["top", "bottom", "outerwear", "footwear", "accessory"]
Pattern = Literal["solid", "striped", "checked", "printed", "textured", "colorblock", "other"]
Season = Literal["spring", "summer", "autumn", "winter"]
Layer = Literal["base", "mid", "outer", "none"]
Level = Literal[1, 2, 3, 4, 5]
Confidence = Literal["low", "medium", "high"]


class ItemAttributes(BaseModel):
    """What the cataloguer extracts from one garment photo."""

    category: Category
    subtype: str
    colors: list[str]
    pattern: Pattern
    formality: Level
    warmth: Level
    seasons: list[Season]
    layer: Layer
    description: str
    confidence: Confidence
    issue: str


class OutfitOut(BaseModel):
    name: str
    item_ids: list[int]
    rationale: str


class AdviceOut(BaseModel):
    reply: str
    outfits: list[OutfitOut]
    exclude_item_ids: list[int]


class ShopBuyItem(BaseModel):
    """One new piece to buy: no product link, a description Claude is confident exists at the suggested stores."""

    category: Category
    description: str
    price_low: float
    price_high: float
    store_suggestion: str
    why: str


class ShopOutfitOut(BaseModel):
    name: str
    owned_item_ids: list[int]   # pieces from the current wardrobe, may be empty
    buy: list[ShopBuyItem]      # new pieces, may be empty (an outfit can be 100% owned or 100% new)
    rationale: str


class ShopAdviceOut(BaseModel):
    reply: str
    outfits: list[ShopOutfitOut]
