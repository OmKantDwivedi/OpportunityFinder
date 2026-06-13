"""Levanta API client (Creator API v2).

Spec: https://api-docs.levanta.io/ - GET {base}/products
  - Base URL: https://app.levanta.io/api/creator/v2
  - Auth:     Authorization: Bearer <API key>
  - Required query param: marketplace (e.g. "amazon.com" or "all")
  - Optional: access=true (only brands you actively partner with)
  - Pagination: cursor-based - response returns {"products": [...], "cursor": ...};
    pass the cursor back until it comes back null/empty.

Response product shape (v2):
  primaryId, title, brandId, brandName, category,
  price:      {currency, value}            (value is a string, e.g. "189.99")
  commission: {sellerCommission, marketplaceCommission, totalCommission}
              (percentage RATES as strings, e.g. "12" = 12%)
  availability: IN_STOCK | OUT_OF_STOCK

Commission per sale is therefore computed as price * totalCommission%.

If the configured base URL 404s (account on a different API tier/version),
the client automatically tries known alternates and logs which one worked.

When MOCK_MODE is enabled, a realistic sample catalog is returned instead.
"""
from __future__ import annotations

from typing import Callable

import httpx

import config
from services import settings as rt

# Tried in order when the configured base URL returns 404 for /products.
FALLBACK_BASES = [
    "https://app.levanta.io/api/creator/v2",
    "https://app.levanta.io/api/creator/v2/preview",
    "https://app.levanta.io/api/creator/v1",
    "https://app.levanta.io/api/seller/v1",
]


class LevantaClient:
    def __init__(
        self,
        log: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.base_url = config.LEVANTA_BASE_URL.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {config.LEVANTA_API_KEY}",
            "Accept": "application/json",
        }
        self.log = log or (lambda m: None)
        self.should_cancel = should_cancel or (lambda: False)

    # ------------------------------------------------------------------ public
    def fetch_products(self, min_commission: float = 0.0) -> list[dict]:
        """Return normalized products that earn >= min_commission $/sale,
        keeping at most config.MAX_PRODUCTS (highest commission first).

        The commission filter is applied per page while paginating, and
        pagination stops at config.LEVANTA_MAX_PAGES, so huge catalogs
        (100k+ products) don't stall the pipeline. Narrow large catalogs
        server-side with LEVANTA_BRAND_IDS.
        """
        if config.MOCK_MODE:
            products = [p for p in _mock_products() if p["commission"] >= min_commission]
            products.sort(key=lambda p: p["commission"], reverse=True)
            return products[: config.MAX_PRODUCTS]

        bases = [self.base_url] + [b for b in FALLBACK_BASES if b != self.base_url]
        last_404 = None
        with httpx.Client(timeout=30) as client:
            for base in bases:
                try:
                    products = self._fetch_filtered(client, base, min_commission)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        last_404 = base
                        self.log(f"Levanta: {base}/products -> 404, trying next known endpoint...")
                        continue
                    if exc.response.status_code == 401:
                        raise RuntimeError(
                            "Levanta returned 401 Unauthorized - the API key is invalid or "
                            "lacks access. Check LEVANTA_API_KEY in .env (Settings -> API in "
                            "the Levanta dashboard, admin access required)."
                        ) from exc
                    raise
                if base != self.base_url:
                    self.log(
                        f"Levanta: connected via {base} - update LEVANTA_BASE_URL in .env "
                        "to this value to skip the fallback next time."
                    )
                return products
        raise RuntimeError(
            f"Levanta /products returned 404 on every known base URL (last tried: {last_404}). "
            "Your account may use a different API tier - check the interactive docs at "
            "https://api-docs.levanta.io and set LEVANTA_BASE_URL in .env accordingly."
        )

    # ---------------------------------------------------------------- internals
    def _fetch_filtered(
        self, client: httpx.Client, base: str, min_commission: float
    ) -> list[dict]:
        kept: list[dict] = []
        scanned = 0          # products on pages we actually collected from
        cursor: str | None = None
        page = 0
        start_page, end_page = rt.get_page_range()
        if start_page > 1:
            self.log(
                f"Levanta: scanning pages {start_page}-{end_page} "
                f"(walking pages 1-{start_page - 1} to reach the start, then collecting)"
            )
        while True:
            if self.should_cancel():
                self.log("Levanta: cancelled by user during catalog fetch")
                break
            page += 1
            params: dict = {
                "marketplace": config.LEVANTA_MARKETPLACE,
                "limit": 100,
            }
            if config.LEVANTA_ACCESS_ONLY:
                params["access"] = "true"
            brand_ids = rt.get("LEVANTA_BRAND_IDS")
            if brand_ids:
                params["brand_ids"] = brand_ids
            if cursor:
                params["cursor"] = cursor

            resp = client.get(f"{base}/products", headers=self.headers, params=params)
            resp.raise_for_status()
            payload = resp.json()
            items = payload.get("products") or []

            in_range = start_page <= page <= end_page
            if in_range:
                scanned += len(items)
                for item in items:
                    p = _normalize(item)
                    if p["commission"] >= min_commission:
                        kept.append(p)

            if in_range and (page % 10 == 0 or not payload.get("cursor") or not items):
                self.log(
                    f"Levanta: page {page} (collected {scanned} products from pages "
                    f"{start_page}-{page}) - {len(kept)} pass the >= ${min_commission:g}/sale filter"
                )

            cursor = payload.get("cursor")
            if not cursor or not items:
                break
            if page >= end_page:
                self.log(
                    f"Levanta: reached end of page range ({start_page}-{end_page}); "
                    f"{scanned} products collected. Adjust 'Catalog pages to scan' in "
                    "Settings to scan more, or set Brand IDs to target specific partners."
                )
                break

        kept.sort(key=lambda p: p["commission"], reverse=True)
        if len(kept) > config.MAX_PRODUCTS:
            self.log(
                f"Levanta: keeping the top {config.MAX_PRODUCTS} of {len(kept)} qualifying "
                "products by commission (MAX_PRODUCTS in .env controls this)"
            )
            kept = kept[: config.MAX_PRODUCTS]
        return kept


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize(item: dict) -> dict:
    # Price: v2 nests it as {"currency": "USD", "value": "189.99"}
    price_field = item.get("price")
    if isinstance(price_field, dict):
        price = _to_float(price_field.get("value"))
    else:
        price = _to_float(price_field)

    # Commission: v2 gives percentage rates as strings; totalCommission is the
    # full rate the creator earns. Dollars per sale = price * rate / 100.
    commission_usd = 0.0
    comm_field = item.get("commission")
    if isinstance(comm_field, dict):
        rate = _to_float(comm_field.get("totalCommission"))
        if rate == 0.0:
            rate = _to_float(comm_field.get("sellerCommission")) + _to_float(
                comm_field.get("marketplaceCommission")
            )
        commission_usd = round(price * rate / 100.0, 2)
    elif comm_field is not None:
        # Older/seller APIs may expose a flat rate field
        commission_usd = round(price * _to_float(comm_field) / 100.0, 2)

    return {
        "id": str(item.get("primaryId") or item.get("id") or item.get("asin") or item.get("title")),
        "name": item.get("title") or item.get("name") or "",
        "category": item.get("category") or "",
        "brand": item.get("brandName") or item.get("brand") or "",
        "price": price,
        "commission": commission_usd,
    }


# ---------------------------------------------------------------------------
# Mock catalog for demos / development
# ---------------------------------------------------------------------------
def _mock_products() -> list[dict]:
    raw = [
        ("LV-1001", "PureBreeze HEPA Air Purifier H13", "Home & Kitchen > Air Quality", "PureBreeze", 189.99, 22.80),
        ("LV-1002", "DryWell 50-Pint Smart Dehumidifier", "Home & Kitchen > Air Quality", "DryWell", 249.00, 29.88),
        ("LV-1003", "AeroMist Ultrasonic Cool Mist Humidifier", "Home & Kitchen > Air Quality", "AeroMist", 59.99, 7.20),
        ("LV-1004", "FlexiDesk Electric Standing Desk 55in", "Office > Furniture", "FlexiDesk", 429.00, 51.48),
        ("LV-1005", "ErgoLift Mesh Ergonomic Office Chair", "Office > Furniture", "ErgoLift", 319.00, 38.28),
        ("LV-1006", "TrailBeast 65L Hiking Backpack", "Sports & Outdoors > Camping", "TrailBeast", 139.95, 16.79),
        ("LV-1007", "CampGlow Rechargeable Lantern 3-Pack", "Sports & Outdoors > Camping", "CampGlow", 44.99, 5.40),
        ("LV-1008", "BrewCraft Pour Over Coffee Maker Set", "Home & Kitchen > Coffee", "BrewCraft", 89.00, 10.68),
        ("LV-1009", "GrindPro Conical Burr Coffee Grinder", "Home & Kitchen > Coffee", "GrindPro", 159.00, 19.08),
        ("LV-1010", "SleepCloud Cooling Memory Foam Pillow", "Home & Kitchen > Bedding", "SleepCloud", 79.99, 12.00),
        ("LV-1011", "PetGuard Automatic Pet Feeder WiFi", "Pet Supplies > Feeding", "PetGuard", 119.99, 14.40),
        ("LV-1012", "AquaPure Under Sink Water Filter System", "Home & Kitchen > Water", "AquaPure", 199.00, 23.88),
        ("LV-1013", "VoltCharge 200W GaN Charging Station", "Electronics > Chargers", "VoltCharge", 99.99, 12.00),
        ("LV-1014", "SoundNest Noise Cancelling Headphones", "Electronics > Audio", "SoundNest", 279.00, 33.48),
        ("LV-1015", "GardenFlow Drip Irrigation Starter Kit", "Garden > Watering", "GardenFlow", 64.99, 7.80),
    ]
    return [
        {"id": r[0], "name": r[1], "category": r[2], "brand": r[3], "price": r[4], "commission": r[5]}
        for r in raw
    ]
