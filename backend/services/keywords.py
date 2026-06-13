"""Keyword generation.

Turns a product record into a small set of search keywords using the product
"type" (extracted from name/category) plus standard modifiers:
    air purifier, best air purifier, air purifier reddit, air purifier review
"""
from __future__ import annotations

import re

import config

# Marketing fluff / spec tokens stripped from product names
_NOISE = {
    "hepa", "smart", "wifi", "wi-fi", "electric", "rechargeable", "ultrasonic",
    "cool", "mist", "memory", "foam", "cooling", "automatic", "conical", "burr",
    "noise", "cancelling", "canceling", "gan", "pro", "plus", "set", "kit",
    "pack", "starter", "system", "h13", "3-pack", "55in", "50-pint", "65l", "200w",
    "mesh", "ergonomic", "under", "sink", "pour", "over",
}

_MODIFIERS = [
    "{kw}",
    "best {kw}",
    "{kw} reddit",
    "{kw} review",
    "best {kw} reddit",
    "{kw} recommendation",
]


def product_type(product: dict) -> str:
    """Extract the generic product type, e.g.
    'PureBreeze HEPA Air Purifier H13' -> 'air purifier'."""
    name = product.get("name", "")
    brand = (product.get("brand") or "").lower()
    tokens = re.sub(r"[^a-zA-Z0-9\- ]", " ", name).lower().split()
    cleaned = [
        t for t in tokens
        if t not in _NOISE and t != brand and not re.fullmatch(r"[\d\-]+[a-z]*", t)
    ]
    # The trailing nouns of a product title are usually the type
    core = cleaned[-2:] if len(cleaned) >= 2 else cleaned
    kw = " ".join(core).strip()
    if not kw:
        # Fall back to the leaf of the category path
        kw = (product.get("category") or "").split(">")[-1].strip().lower()
    return kw


def generate_keywords(product: dict) -> list[str]:
    base = product_type(product)
    if not base:
        return []
    seen, out = set(), []
    for tpl in _MODIFIERS[: config.MAX_KEYWORDS_PER_PRODUCT]:
        kw = tpl.format(kw=base)
        if kw not in seen:
            seen.add(kw)
            out.append(kw)
    return out
