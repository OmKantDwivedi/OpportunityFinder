"""Application configuration loaded from environment variables / .env file."""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (one level above /backend)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# --- API credentials -------------------------------------------------------
LEVANTA_API_KEY: str = os.getenv("LEVANTA_API_KEY", "")
LEVANTA_BASE_URL: str = os.getenv("LEVANTA_BASE_URL", "https://app.levanta.io/api/creator/v2")
# Required by the Levanta API: "all" or a specific marketplace like "amazon.com"
LEVANTA_MARKETPLACE: str = os.getenv("LEVANTA_MARKETPLACE", "all")
# Only pull products from brands you have an active partnership with
LEVANTA_ACCESS_ONLY: bool = _as_bool(os.getenv("LEVANTA_ACCESS_ONLY"), default=True)
# Optional comma-separated brand IDs to narrow huge catalogs server-side
LEVANTA_BRAND_IDS: str = os.getenv("LEVANTA_BRAND_IDS", "").strip()
# Stop paginating the catalog after this many pages (100 products per page)
LEVANTA_MAX_PAGES: int = int(os.getenv("LEVANTA_MAX_PAGES", "50"))

AHREFS_API_KEY: str = os.getenv("AHREFS_API_KEY", "")
AHREFS_BASE_URL: str = os.getenv("AHREFS_BASE_URL", "https://api.ahrefs.com/v3")

# --- Pipeline settings ------------------------------------------------------
MIN_COMMISSION_USD: float = float(os.getenv("MIN_COMMISSION_USD", "10"))
SERP_TOP_N: int = int(os.getenv("SERP_TOP_N", "10"))
SERP_COUNTRY: str = os.getenv("SERP_COUNTRY", "us")
MAX_KEYWORDS_PER_PRODUCT: int = int(os.getenv("MAX_KEYWORDS_PER_PRODUCT", "6"))

# --- Scale guards (large catalogs) -------------------------------------------
# After the commission filter, keep at most this many products (best first).
MAX_PRODUCTS: int = int(os.getenv("MAX_PRODUCTS", "200"))
# Safety cap for product-NAME search (scans whole catalog); 0 = unlimited
NAME_SEARCH_MAX_MATCHES: int = int(os.getenv("NAME_SEARCH_MAX_MATCHES", "1000")) or 10**9
# Hard cap on UNIQUE keywords sent to Ahrefs per run (each costs API units).
# Keywords from the highest-commission products are kept first.
MAX_SERP_KEYWORDS: int = int(os.getenv("MAX_SERP_KEYWORDS", "300"))
REQUEST_DELAY_SECONDS: float = float(os.getenv("REQUEST_DELAY_SECONDS", "0.4"))

# When MOCK_MODE is on, the app generates realistic sample data instead of
# calling Levanta / Ahrefs. Useful for demos and frontend development.
# Values: "auto" (or unset/blank) -> mock only when API keys are missing;
#         "true"/"false" -> force on/off.
_mock_raw = (os.getenv("MOCK_MODE") or "auto").strip().lower()
if _mock_raw in {"auto", ""}:
    MOCK_MODE: bool = not (LEVANTA_API_KEY and AHREFS_API_KEY)
else:
    MOCK_MODE = _as_bool(_mock_raw)

# --- Storage ----------------------------------------------------------------
DB_PATH: str = os.getenv("DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "app.db"))
