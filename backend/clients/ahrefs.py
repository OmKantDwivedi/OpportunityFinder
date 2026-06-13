"""Ahrefs API v3 client.

Two capabilities are used:
  1. SERP overview  - top organic results for a keyword (we keep top 10).
  2. Batch analysis - organic traffic metrics for a list of URLs.

Both require an Ahrefs API v3 key (Enterprise / API add-on). Endpoints follow
the v3 docs at https://docs.ahrefs.com/ - if your plan exposes different
routes, only this file needs editing.

In MOCK_MODE deterministic sample data is generated so the pipeline can be
demoed without burning API credits.
"""
from __future__ import annotations

import hashlib
import random
import re

import httpx

import config

REDDIT_RE = re.compile(r"^https?://(www\.|old\.)?reddit\.com/r/[^/]+/comments/", re.I)


class AhrefsClient:
    def __init__(self) -> None:
        self.base_url = config.AHREFS_BASE_URL.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {config.AHREFS_API_KEY}",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------ SERP
    def serp_top_results(self, keyword: str, top_n: int = 10) -> list[dict]:
        """Return [{url, position, title}] for the top organic results."""
        if config.MOCK_MODE:
            return _mock_serp(keyword, top_n)

        with httpx.Client(timeout=30) as client:
            resp = client.get(
                f"{self.base_url}/serp-overview/serp-overview",
                headers=self.headers,
                params={
                    "keyword": keyword,
                    "country": config.SERP_COUNTRY,
                    "select": "url,position,title",
                    "top_positions": top_n,
                },
            )
            _raise_with_detail(resp)
            rows = resp.json().get("positions") or resp.json().get("serp_overview") or []
        results = []
        for row in rows:
            pos = row.get("position") or 0
            if pos and pos <= top_n and row.get("url"):
                results.append({"url": row["url"], "position": int(pos), "title": row.get("title") or ""})
        return results

    # ------------------------------------------------- Batch analysis (traffic)
    def batch_traffic(self, urls: list[str]) -> dict[str, int]:
        """Return {url: monthly organic traffic} via POST /batch-analysis.

        Spec (docs.ahrefs.com): `select` is a JSON ARRAY of field names, and
        each target needs url + mode + protocol. Results are mapped back via
        the `index` field (the position of the target in the request), which
        is more reliable than URL string matching because Ahrefs may
        normalize the returned URL.
        """
        if config.MOCK_MODE:
            return {u: _mock_traffic(u) for u in urls}

        traffic: dict[str, int] = {}
        with httpx.Client(timeout=60) as client:
            # Ahrefs batch analysis accepts up to 100 targets per request
            for i in range(0, len(urls), 100):
                chunk = urls[i : i + 100]
                resp = client.post(
                    f"{self.base_url}/batch-analysis/batch-analysis",
                    headers={**self.headers, "Content-Type": "application/json"},
                    json={
                        "select": ["index", "url", "org_traffic"],
                        "country": config.SERP_COUNTRY,
                        "volume_mode": "monthly",
                        "targets": [
                            {"url": u, "mode": "exact", "protocol": "both"} for u in chunk
                        ],
                    },
                )
                _raise_with_detail(resp)
                for row in resp.json().get("targets", []):
                    idx = row.get("index")
                    if isinstance(idx, int) and 0 <= idx < len(chunk):
                        url = chunk[idx]
                    else:
                        url = row.get("url", "")
                    traffic[url] = int(row.get("org_traffic") or 0)
        return traffic


def _raise_with_detail(resp: httpx.Response) -> None:
    """raise_for_status, but include Ahrefs' error message in the exception."""
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("error") or resp.text[:300]
        except Exception:  # noqa: BLE001
            detail = resp.text[:300]
        raise RuntimeError(
            f"Ahrefs API {resp.status_code} on {resp.request.url.path}: {detail}"
        )


def is_reddit_thread(url: str) -> bool:
    return bool(REDDIT_RE.match(url))


# ---------------------------------------------------------------------------
# Mock data: deterministic per keyword/url so reruns are stable
# ---------------------------------------------------------------------------
_SUBREDDIT_HINTS = {
    "air purifier": "AirPurifiers", "dehumidifier": "Dehumidifiers", "desk": "StandingDesks",
    "chair": "OfficeChairs", "backpack": "CampingGear", "coffee": "Coffee", "grinder": "Coffee",
    "pillow": "Sleep", "pet feeder": "PetTech", "water filter": "WaterFiltration",
    "charger": "UsbCHardware", "headphones": "HeadphoneAdvice", "humidifier": "Humidifiers",
}


def _seed(text: str) -> random.Random:
    return random.Random(int(hashlib.md5(text.encode()).hexdigest(), 16))


def _subreddit_for(keyword: str) -> str:
    kw = keyword.lower()
    for hint, sub in _SUBREDDIT_HINTS.items():
        if hint in kw:
            return sub
    return "BuyItForLife"


def _mock_serp(keyword: str, top_n: int) -> list[dict]:
    rng = _seed(keyword)
    slug = re.sub(r"[^a-z0-9]+", "_", keyword.lower()).strip("_")
    sub = _subreddit_for(keyword)
    results = []
    reddit_positions = sorted(rng.sample(range(1, top_n + 1), rng.randint(1, 3)))
    competitors = ["wirecutter.com", "goodhousekeeping.com", "rtings.com", "forbes.com",
                   "nytimes.com", "techgearlab.com", "cnet.com", "tomsguide.com", "youtube.com"]
    for pos in range(1, top_n + 1):
        if pos in reddit_positions:
            thread_id = hashlib.md5(f"{sub}{slug}{pos}".encode()).hexdigest()[:7]
            results.append({
                "url": f"https://www.reddit.com/r/{sub}/comments/{thread_id}/{slug}_recommendations/",
                "position": pos,
                "title": f"{keyword.title()} - what do you actually recommend? : r/{sub}",
            })
        else:
            dom = competitors[(pos + rng.randint(0, 8)) % len(competitors)]
            results.append({"url": f"https://{dom}/{slug}", "position": pos, "title": f"{keyword.title()} guide | {dom}"})
    return results


def _mock_traffic(url: str) -> int:
    rng = _seed(url)
    # Long-tailed distribution: most threads modest, a few big winners
    base = rng.choice([rng.randint(80, 600), rng.randint(600, 3000), rng.randint(3000, 18000)])
    return base
