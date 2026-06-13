"""End-to-end research pipeline.

Steps (mirrors the spec):
 1. Pull Levanta products (filtered to >= MIN_COMMISSION_USD while paginating,
    capped at MAX_PRODUCTS best-by-commission; LEVANTA_MAX_PAGES bounds the scan)
 2. Generate keywords per product, then de-duplicate them GLOBALLY so each
    unique keyword is sent to Ahrefs exactly once (capped at MAX_SERP_KEYWORDS,
    prioritized by the best commission among the products behind each keyword)
 3. Ahrefs SERP for each unique keyword, keep top-10 Reddit threads
 4. De-duplicate threads, store keyword + URL + position
 5. Ahrefs batch analysis -> organic traffic per thread
 6. Match products to threads, compute relevance + opportunity score

The pipeline runs in a background thread and can be CANCELLED via
request_cancel(). Progress, a live activity log, and errors are exposed via
PIPELINE_STATE; each run is recorded in the `runs` table so a server restart
mid-run is detected and surfaced.
"""
from __future__ import annotations

import re
import threading
import time
import traceback
from collections import deque

import config
import database
from clients.ahrefs import AhrefsClient, is_reddit_thread
from clients.levanta import LevantaClient
from services import keywords as kw_service
from services import matching
from services import settings as rt

PIPELINE_STATE = {
    "running": False,
    "run_id": None,
    "step": "",
    "progress": 0,        # 0..100
    "detail": "",
    "log": deque(maxlen=400),
    "last_run": None,
    "error": None,
    "cancel_requested": False,
    "interrupted_run": None,  # set at startup if a previous run was killed
}
_lock = threading.Lock()


class PipelineCancelled(Exception):
    pass


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    PIPELINE_STATE["log"].append(line)
    PIPELINE_STATE["detail"] = msg


def _cancelled() -> bool:
    return PIPELINE_STATE["cancel_requested"]


def _check_cancel() -> None:
    if _cancelled():
        raise PipelineCancelled()


def _set(step: str, progress: int, msg: str | None = None) -> None:
    PIPELINE_STATE.update(step=step, progress=progress)
    if msg:
        _log(msg)
    if PIPELINE_STATE["run_id"]:
        try:
            with database.get_conn() as conn:
                conn.execute(
                    "UPDATE runs SET status='running', detail=? WHERE id=?",
                    (f"{progress}% - {msg or step}", PIPELINE_STATE["run_id"]),
                )
        except Exception:  # noqa: BLE001 - progress persistence is best-effort
            pass


def recover_interrupted_runs() -> None:
    """Called at app startup: any run still marked 'running' was killed by a
    server restart. Mark it interrupted and surface that to the UI."""
    with database.get_conn() as conn:
        rows = conn.execute("SELECT id, detail FROM runs WHERE status='running'").fetchall()
        for r in rows:
            conn.execute(
                "UPDATE runs SET status='interrupted', finished_at=datetime('now') WHERE id=?",
                (r["id"],),
            )
        if rows:
            PIPELINE_STATE["interrupted_run"] = (
                f"Previous run was interrupted at {rows[-1]['detail'] or 'an unknown step'} "
                "(the server restarted mid-run - avoid editing files / using --reload while "
                "a pipeline is running). Click Run pipeline to start again."
            )


def validate_credentials() -> str | None:
    """Return an error message if live mode is on but keys are missing."""
    if config.MOCK_MODE:
        return None
    missing = []
    if not config.LEVANTA_API_KEY:
        missing.append("LEVANTA_API_KEY")
    if not config.AHREFS_API_KEY:
        missing.append("AHREFS_API_KEY")
    if missing:
        return (
            f"Live mode is enabled but {' and '.join(missing)} is empty in .env. "
            "Add the key(s), or set MOCK_MODE=true for demo data."
        )
    return None


def start_pipeline() -> tuple[bool, str | None]:
    """Kick off a run in a background thread. Returns (started, error)."""
    err = validate_credentials()
    if err:
        return False, err
    with _lock:
        if PIPELINE_STATE["running"]:
            return False, "Pipeline already running"
        PIPELINE_STATE["log"].clear()
        PIPELINE_STATE.update(
            running=True, step="starting", progress=0, detail="", error=None,
            cancel_requested=False, interrupted_run=None,
        )
    threading.Thread(target=_run_safe, daemon=True).start()
    return True, None


def request_cancel() -> bool:
    """Ask a running pipeline to stop at the next safe point."""
    if not PIPELINE_STATE["running"]:
        return False
    PIPELINE_STATE["cancel_requested"] = True
    _log("Cancel requested - stopping at the next safe point...")
    return True


def _run_safe() -> None:
    with database.get_conn() as conn:
        cur = conn.execute("INSERT INTO runs (status) VALUES ('running')")
        PIPELINE_STATE["run_id"] = cur.lastrowid
    mode = "MOCK (sample data)" if config.MOCK_MODE else "LIVE (Levanta + Ahrefs APIs)"
    _log(f"Pipeline started in {mode} mode")
    if config.MOCK_MODE:
        _log("Note: mock Reddit URLs are simulated and will 404 - they exist for demo only")
    final_status, error = "done", None
    try:
        _run()
        _set("done", 100, "Pipeline complete")
    except PipelineCancelled:
        final_status = "cancelled"
        _set("cancelled", PIPELINE_STATE["progress"], "Pipeline cancelled by user")
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        PIPELINE_STATE["error"] = error
        _log(f"ERROR: {error}")
        PIPELINE_STATE["detail"] = traceback.format_exc(limit=3)
        final_status = "failed"
    finally:
        PIPELINE_STATE.update(
            running=False, cancel_requested=False,
            last_run=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        with database.get_conn() as conn:
            conn.execute(
                "UPDATE runs SET status=?, finished_at=datetime('now'), detail=? WHERE id=?",
                (final_status, error or final_status, PIPELINE_STATE["run_id"]),
            )
        PIPELINE_STATE["run_id"] = None


def _subreddit_of(url: str) -> str:
    m = re.search(r"reddit\.com/r/([^/]+)/", url)
    return m.group(1) if m else ""


def _run() -> None:
    levanta = LevantaClient(log=_log, should_cancel=_cancelled)
    ahrefs = AhrefsClient()
    conn = database.get_conn()

    # -------------------------------------------------- 1. Products
    min_comm = rt.get("MIN_COMMISSION_USD")
    start_page, end_page = rt.get_page_range()
    brand_ids = rt.get("LEVANTA_BRAND_IDS")
    _set("products", 3,
         f"Step 1/6 - Fetching Levanta catalog (pages {start_page}-{end_page}"
         + (f", brands: {brand_ids}" if brand_ids else "")
         + f", keeping top {config.MAX_PRODUCTS} by commission)...")
    products = levanta.fetch_products(min_commission=min_comm)
    _check_cancel()
    _set("products", 10,
         f"{len(products)} products selected (>= ${min_comm:g}/sale)")
    if not products:
        raise RuntimeError(
            "No products passed the commission filter - lower the min commission "
            f"(currently ${min_comm:g}) or raise the scan depth in Settings, or "
            "target specific partner brands with Brand IDs."
        )

    with conn:
        conn.execute("DELETE FROM keywords")
        for p in products:
            conn.execute(
                """INSERT INTO products (id, name, category, brand, price, commission)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, category=excluded.category,
                     brand=excluded.brand, price=excluded.price, commission=excluded.commission""",
                (p["id"], p["name"], p["category"], p["brand"], p["price"], p["commission"]),
            )

    # -------------------------------------------------- 2. Keywords (global dedupe)
    _set("keywords", 13, "Step 2/6 - Generating keywords...")
    products_by_id = {p["id"]: p for p in products}
    kw_to_products: dict[str, set[str]] = {}
    for p in products:
        p["type_kw"] = kw_service.product_type(p)
        for kw in kw_service.generate_keywords(p):
            kw_to_products.setdefault(kw, set()).add(p["id"])
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO keywords (product_id, keyword) VALUES (?,?)",
                    (p["id"], kw),
                )

    raw_total = sum(len(pids) for pids in kw_to_products.values())
    unique_kws = list(kw_to_products.keys())
    _log(f"{raw_total} product-keyword pairs -> {len(unique_kws)} unique keywords "
         "(each searched once)")

    # Cost cap: prioritize keywords whose best product pays the most
    if len(unique_kws) > config.MAX_SERP_KEYWORDS:
        unique_kws.sort(
            key=lambda kw: max(products_by_id[pid]["commission"] for pid in kw_to_products[kw]),
            reverse=True,
        )
        unique_kws = unique_kws[: config.MAX_SERP_KEYWORDS]
        _log(f"Keyword cap applied: top {config.MAX_SERP_KEYWORDS} keywords by product "
             "commission will be searched (MAX_SERP_KEYWORDS in .env controls this)")
    est_min = len(unique_kws) * (config.REQUEST_DELAY_SECONDS + 0.5) / 60
    _set("keywords", 18,
         f"{len(unique_kws)} Ahrefs SERP lookups queued (~{est_min:.0f} min in live mode)")

    # -------------------------------------------------- 3+4. SERP -> Reddit threads
    _set("serp", 20, f"Step 3/6 - Checking Google top {config.SERP_TOP_N} per keyword via Ahrefs...")
    serp_hits: dict[str, dict] = {}
    reddit_found = 0
    for i, kw in enumerate(unique_kws, 1):
        _check_cancel()
        _set("serp", 20 + int(45 * i / max(len(unique_kws), 1)),
             f"SERP {i}/{len(unique_kws)}: \"{kw}\"")
        try:
            results = ahrefs.serp_top_results(kw, config.SERP_TOP_N)
        except Exception as exc:  # noqa: BLE001
            _log(f"  WARNING - SERP failed for '{kw}': {exc}")
            continue
        for r in results:
            if not is_reddit_thread(r["url"]):
                continue
            reddit_found += 1
            url = r["url"].split("?")[0].rstrip("/") + "/"
            hit = serp_hits.setdefault(url, {"title": r["title"], "keywords": {}})
            prev = hit["keywords"].get(kw)
            if prev is None or r["position"] < prev:
                hit["keywords"][kw] = r["position"]
        if config.MOCK_MODE:
            time.sleep(0.06)  # tiny delay so demo progress is visible
        else:
            time.sleep(config.REQUEST_DELAY_SECONDS)

    _set("dedupe", 68,
         f"Step 4/6 - {reddit_found} Reddit results found, {len(serp_hits)} unique threads after dedupe")

    # -------------------------------------------------- 5. Traffic
    _check_cancel()
    urls = list(serp_hits.keys())
    _set("traffic", 72, f"Step 5/6 - Ahrefs batch analysis on {len(urls)} thread URLs...")
    traffic_map = ahrefs.batch_traffic(urls) if urls else {}
    _log(f"Traffic data received for {len(traffic_map)} URLs")

    with conn:
        for url, hit in serp_hits.items():
            conn.execute(
                """INSERT INTO threads (url, title, subreddit, traffic) VALUES (?,?,?,?)
                   ON CONFLICT(url) DO UPDATE SET title=excluded.title,
                     subreddit=excluded.subreddit, traffic=excluded.traffic""",
                (url, hit["title"], _subreddit_of(url), traffic_map.get(url, 0)),
            )

    # -------------------------------------------------- 6. Match & score
    _set("scoring", 85, "Step 6/6 - Matching products to threads and scoring...")
    thread_rows = conn.execute("SELECT id, url, title, subreddit, traffic FROM threads").fetchall()
    threads_by_url = {row["url"]: row for row in thread_rows}

    with conn:
        for url, hit in serp_hits.items():
            t = threads_by_url.get(url)
            if t is None:
                continue
            for kw, pos in hit["keywords"].items():
                for pid in kw_to_products.get(kw, ()):
                    p = products_by_id[pid]
                    rel = matching.relevance(p, kw, t["title"], t["subreddit"])
                    if rel < 0.3:
                        continue
                    score = matching.opportunity_score(rel, t["traffic"], pos, p["commission"])
                    conn.execute(
                        """INSERT INTO opportunities
                             (product_id, thread_id, keyword, position, relevance, score)
                           VALUES (?,?,?,?,?,?)
                           ON CONFLICT(product_id, thread_id) DO UPDATE SET
                             keyword  = CASE WHEN excluded.score > score THEN excluded.keyword  ELSE keyword  END,
                             position = CASE WHEN excluded.score > score THEN excluded.position ELSE position END,
                             relevance= CASE WHEN excluded.score > score THEN excluded.relevance ELSE relevance END,
                             score    = MAX(score, excluded.score)""",
                        (pid, t["id"], kw, pos, rel, score),
                    )

    n = conn.execute("SELECT COUNT(*) c FROM opportunities").fetchone()["c"]
    _set("scoring", 96, f"{n} opportunities scored")
    conn.close()
