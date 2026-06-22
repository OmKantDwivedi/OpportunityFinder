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
        self._logged_sample = False

    # ------------------------------------------------------------------ public
    def fetch_products(
        self, min_commission: float = 0.0, name_query: str = "", scan_all: bool = False
    ) -> list[dict]:
        """Return normalized products that earn >= min_commission $/sale.

        Normal mode: commission filter applied per page, pagination bounded by
        the page-range setting, capped at config.MAX_PRODUCTS (highest first).

        Name-search mode (name_query set, scan_all=True): walks the ENTIRE
        catalog and returns every in-stock product whose title matches all the
        query words AND meets the commission floor - no page-range or
        MAX_PRODUCTS cap (only the safety NAME_SEARCH_MAX_MATCHES limit).
        """
        terms = [t for t in name_query.lower().split() if t]
        if config.MOCK_MODE:
            products = [
                p for p in _mock_products()
                if p["commission"] >= min_commission
                and (not terms or _title_matches(p["name"], terms))
            ]
            products.sort(key=lambda p: p["commission"], reverse=True)
            return products if scan_all else products[: config.MAX_PRODUCTS]

        bases = [self.base_url] + [b for b in FALLBACK_BASES if b != self.base_url]
        last_404 = None
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0)) as client:
            for base in bases:
                try:
                    products = self._fetch_filtered(
                        client, base, min_commission, name_query=name_query, scan_all=scan_all
                    )
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
    def _get_with_retry(self, client, url, params, attempts: int = 4):
        """GET with retry on transient network errors (timeouts, conn resets).
        Levanta catalogs are large; a single slow page shouldn't kill the run."""
        import time as _t
        delay = 2.0
        for i in range(1, attempts + 1):
            try:
                resp = client.get(url, headers=self.headers, params=params)
                resp.raise_for_status()
                return resp
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if i == attempts:
                    raise RuntimeError(
                        f"Levanta request failed after {attempts} attempts ({exc!r}). "
                        "This is usually a transient network/API issue - try the run again, "
                        "or narrow the scan with Brand IDs or a product name search."
                    ) from exc
                self.log(f"Levanta: network hiccup ({type(exc).__name__}), retrying in {delay:g}s "
                         f"(attempt {i}/{attempts})...")
                if self.should_cancel():
                    raise
                _t.sleep(delay)
                delay *= 2
        return None  # unreachable

    def _fetch_filtered(
        self,
        client: httpx.Client,
        base: str,
        min_commission: float,
        name_query: str = "",
        scan_all: bool = False,
    ) -> list[dict]:
        """Fetch + filter products.

        name_query: if set, keep only products whose title contains ALL of the
                    query's words (case-insensitive). Levanta's API has no
                    title-search param, so this is done client-side.
        scan_all:   if True, ignore the page-range cap and walk the ENTIRE
                    catalog (used for name search - "the page should be all").
                    The MAX_PRODUCTS cap is also lifted in this mode.
        """
        kept: list[dict] = []
        scanned = 0
        rej_oos = rej_comm = rej_title = 0   # rejection reason counters
        cursor: str | None = None
        page = 0
        terms = [t for t in name_query.lower().split() if t]
        start_page, end_page = (1, 10**9) if scan_all else rt.get_page_range()

        if terms:
            self.log(f"Levanta: searching ALL pages for products matching {name_query!r}")
        elif start_page > 1:
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
                "limit": 500,  # API max - fewer round-trips
            }
            if config.LEVANTA_ACCESS_ONLY:
                params["access"] = "true"
            brand_ids = rt.get("LEVANTA_BRAND_IDS")
            if brand_ids:
                params["brand_ids"] = brand_ids
            if cursor:
                params["cursor"] = cursor

            resp = self._get_with_retry(client, f"{base}/products", params)
            payload = resp.json()
            items = payload.get("products") or []

            # Diagnostic: dump the raw shape of the first product we ever see, so
            # field-name mismatches (commission/title/price) are obvious in the log.
            if page == 1 and items and not self._logged_sample:
                self._logged_sample = True
                sample = items[0]
                self.log(
                    "Levanta: sample product keys = " + ", ".join(sorted(sample.keys()))
                )
                self.log(
                    f"Levanta: sample title={sample.get('title')!r} "
                    f"price={sample.get('price')!r} commission={sample.get('commission')!r} "
                    f"availability={sample.get('availability')!r} access={sample.get('access')!r}"
                )

            in_range = start_page <= page <= end_page
            if in_range:
                scanned += len(items)
                for item in items:
                    if item.get("availability") == "OUT_OF_STOCK":
                        rej_oos += 1
                        continue
                    p = _normalize(item)
                    if p["commission"] < min_commission:
                        rej_comm += 1
                        continue
                    if terms and not _title_matches(p["name"], terms):
                        rej_title += 1
                        continue
                    kept.append(p)

            if in_range and (page % 5 == 0 or not payload.get("cursor") or not items):
                self.log(
                    f"Levanta: scanned {scanned:,} products (page {page}) - "
                    f"{len(kept)} match"
                    + (f" (rejected: {rej_comm:,} low-commission, "
                       f"{rej_title:,} wrong-title, {rej_oos:,} out-of-stock)"
                       if (rej_comm or rej_title or rej_oos) else "")
                )

            cursor = payload.get("cursor")
            if not cursor or not items:
                self.log(f"Levanta: end of catalog reached ({scanned:,} products scanned)")
                break
            if not scan_all and page >= end_page:
                self.log(
                    f"Levanta: reached end of page range ({start_page}-{end_page}); "
                    f"{scanned:,} products scanned. Increase 'Catalog pages to scan' in "
                    "Settings, or use product name search to scan the whole catalog."
                )
                break
            # In name-search mode, allow an early stop once we have plenty of matches
            if scan_all and len(kept) >= config.NAME_SEARCH_MAX_MATCHES:
                self.log(
                    f"Levanta: collected {len(kept)} matching products "
                    f"(NAME_SEARCH_MAX_MATCHES cap) - stopping scan"
                )
                break

        kept.sort(key=lambda p: p["commission"], reverse=True)
        # MAX_PRODUCTS cap applies only to a normal full-catalog run, not name search
        if not scan_all and len(kept) > config.MAX_PRODUCTS:
            self.log(
                f"Levanta: {len(kept)} products qualify; keeping the top "
                f"{config.MAX_PRODUCTS} by commission (raise MAX_PRODUCTS in .env to keep more)"
            )
            kept = kept[: config.MAX_PRODUCTS]
        return kept


def _title_matches(title: str, terms: list[str]) -> bool:
    """True if the product title contains every search term (case-insensitive)."""
    t = title.lower()
    return all(term in t for term in terms)


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_price(item: dict) -> float:
    price_field = item.get("price")
    if isinstance(price_field, dict):
        return _to_float(price_field.get("value") or price_field.get("amount"))
    if price_field is not None:
        return _to_float(price_field)
    # Other observed keys
    for k in ("priceValue", "listPrice", "buyBoxPrice"):
        if item.get(k) is not None:
            return _to_float(item[k])
    return 0.0


def _rate_to_fraction(rate: float) -> float:
    """Levanta returns commission rates as DECIMAL FRACTIONS (0.060 = 6%, 0.170
    = 17%). Some tiers/older APIs use whole-number percents (6, 17). Normalize
    both to a fraction: values <= 1 are already fractions; larger are percents."""
    if rate <= 1.0:
        return rate
    return rate / 100.0


def _extract_commission(item: dict, price: float) -> float:
    """Return dollars-per-sale, handling the shapes Levanta/its tiers use.

    Verified shape (Creator API v2): commission rate is a decimal FRACTION
    string, e.g. {"totalCommission": "0.170"} means 17%. Dollars per sale =
    price * fraction. Also handles whole-number percents and direct dollars.
    """
    comm = item.get("commission")

    if isinstance(comm, dict):
        # Dollar amount provided directly?
        for k in ("amount", "value", "estimatedCommission", "payout"):
            if comm.get(k) is not None:
                return round(_to_float(comm[k]), 2)
        # Otherwise it's a rate (fraction or percent)
        rate = _to_float(comm.get("totalCommission"))
        if rate == 0.0:
            rate = _to_float(comm.get("sellerCommission")) + _to_float(
                comm.get("marketplaceCommission")
            )
        if rate:
            return round(price * _rate_to_fraction(rate), 2)

    elif comm is not None:
        v = _to_float(comm)
        # Could be a fraction (0.17), a percent (17), or a dollar amount (54.40).
        if 0 < v <= 1 and price > 0:
            return round(price * v, 2)            # fraction
        if 1 < v <= 100 and price > 0:
            return round(price * v / 100.0, 2)    # whole-number percent
        return round(v, 2)                         # dollar amount

    # Top-level rate fields (fraction or percent)
    for k in ("commission_rate", "commissionRate", "attributionCommission"):
        if item.get(k) is not None:
            return round(price * _rate_to_fraction(_to_float(item[k])), 2)
    # Top-level dollar fields
    for k in ("commissionAmount", "estimatedCommission", "payout", "earnings"):
        if item.get(k) is not None:
            return round(_to_float(item[k]), 2)
    return 0.0


def _extract_title(item: dict) -> str:
    return (item.get("title") or item.get("name") or item.get("productTitle")
            or item.get("productName") or "")


def _normalize(item: dict) -> dict:
    price = _extract_price(item)
    commission_usd = _extract_commission(item, price)
    return {
        "id": str(item.get("primaryId") or item.get("id") or item.get("asin")
                  or item.get("sku") or _extract_title(item)),
        "name": _extract_title(item),
        "category": item.get("category") or item.get("category_name") or "",
        "brand": item.get("brandName") or item.get("brand") or item.get("brand_name") or "",
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
