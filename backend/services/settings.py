"""Runtime-editable settings.

A small set of pipeline knobs can be changed from the dashboard UI without
touching .env or restarting the server. Values are persisted in the
`settings` table and override the .env/config defaults. Each pipeline run
reads them at start, so changes apply to the NEXT run.
"""
from __future__ import annotations

import re
from typing import Any

import config
import database

MAX_PAGE_LIMIT = 5000


def parse_page_range(raw) -> tuple[int, int]:
    """Parse a page spec into an inclusive (start, end) page range (1-based).

    Accepts:
      "50"     -> (1, 50)     scan pages 1 through 50
      "5,10"   -> (5, 10)     scan pages 5 through 10
      "5-10"   -> (5, 10)     same, dash also allowed
      5        -> (1, 5)
    Raises ValueError on malformed / out-of-bounds input.
    """
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        parts = [str(raw[0]), str(raw[1])]
    else:
        text = str(raw).strip()
        parts = [p.strip() for p in re.split(r"[,\-]", text) if p.strip()]

    if len(parts) == 1:
        end = int(float(parts[0]))
        start = 1
    elif len(parts) == 2:
        start = int(float(parts[0]))
        end = int(float(parts[1]))
    else:
        raise ValueError("use a single number (e.g. 50) or a range (e.g. 5,10)")

    if start < 1 or end < 1:
        raise ValueError("page numbers must be 1 or higher")
    if start > end:
        raise ValueError(f"start page ({start}) can't be after end page ({end})")
    if end > MAX_PAGE_LIMIT:
        raise ValueError(f"end page can't exceed {MAX_PAGE_LIMIT}")
    return start, end


def format_page_range(rng: tuple[int, int]) -> str:
    start, end = rng
    return str(end) if start == 1 else f"{start},{end}"


def _default_page_range() -> str:
    return format_page_range((1, config.LEVANTA_MAX_PAGES))


# key -> (type, default, label, help text, min, max)
EDITABLE: dict[str, dict[str, Any]] = {
    "MIN_COMMISSION_USD": {
        "type": "float",
        "default": config.MIN_COMMISSION_USD,
        "label": "Min commission ($ per sale)",
        "help": "Products earning less than this per sale are skipped.",
        "min": 0, "max": 10000,
    },
    "LEVANTA_PAGE_RANGE": {
        "type": "page_range",
        "default": _default_page_range(),
        "label": "Catalog pages to scan",
        "help": "Each page = 100 products. Enter a depth like \"50\" (pages 1-50), "
                "or a range like \"5,10\" to scan only pages 5 through 10.",
    },
    "LEVANTA_BRAND_IDS": {
        "type": "str",
        "default": config.LEVANTA_BRAND_IDS,
        "label": "Brand IDs (comma-separated)",
        "help": "Scan only these Levanta partner brands (server-side filter). "
                "Leave empty for all partnered brands.",
    },
}


def _cast(key: str, raw: str):
    spec = EDITABLE[key]
    if spec["type"] == "float":
        return float(raw)
    if spec["type"] == "int":
        return int(float(raw))
    if spec["type"] == "page_range":
        # Validate, then return the canonical "N" or "start,end" string.
        return format_page_range(parse_page_range(raw))
    return str(raw).strip()


def get_page_range() -> tuple[int, int]:
    """Effective (start_page, end_page), 1-based inclusive."""
    return parse_page_range(get("LEVANTA_PAGE_RANGE"))


def get(key: str):
    """Effective value: DB override if present, else .env/config default."""
    spec = EDITABLE[key]
    with database.get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return spec["default"]
    try:
        return _cast(key, row["value"])
    except (ValueError, TypeError):
        return spec["default"]


def get_all() -> list[dict]:
    """All editable settings with metadata for the UI."""
    out = []
    for key, spec in EDITABLE.items():
        out.append({
            "key": key,
            "value": get(key),
            "default": spec["default"],
            "type": spec["type"],
            "label": spec["label"],
            "help": spec["help"],
            "min": spec.get("min"),
            "max": spec.get("max"),
        })
    return out


def set_many(values: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist. Returns the effective settings afterwards.
    Raises ValueError with a user-readable message on bad input."""
    cleaned: dict[str, str] = {}
    for key, raw in values.items():
        if key not in EDITABLE:
            raise ValueError(f"Unknown setting: {key}")
        spec = EDITABLE[key]
        try:
            val = _cast(key, str(raw))
        except ValueError as exc:
            if spec["type"] == "page_range":
                raise ValueError(f"{spec['label']}: {exc}")
            raise ValueError(f"{spec['label']}: '{raw}' is not a valid {spec['type']}")
        except TypeError:
            raise ValueError(f"{spec['label']}: '{raw}' is not a valid {spec['type']}")
        if spec["type"] in ("int", "float"):
            lo, hi = spec.get("min"), spec.get("max")
            if lo is not None and val < lo:
                raise ValueError(f"{spec['label']}: must be at least {lo}")
            if hi is not None and val > hi:
                raise ValueError(f"{spec['label']}: must be at most {hi}")
        if key == "LEVANTA_BRAND_IDS":
            # normalize: strip spaces around commas, drop empties
            val = ",".join(part.strip() for part in str(val).split(",") if part.strip())
        cleaned[key] = str(val)

    with database.get_conn() as conn:
        for key, val in cleaned.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, val),
            )
    return {s["key"]: s["value"] for s in get_all()}
