"""FastAPI application - REST API + serves the dashboard frontend.

Run from the backend/ directory:
    uvicorn app:app --reload --port 8000
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import config
import database
from services import pipeline
from services import settings as rt

app = FastAPI(title="Reddit Affiliate Opportunity Finder")
database.init_db()
pipeline.recover_interrupted_runs()

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

BASE_QUERY = """
SELECT o.id, o.keyword, o.position, o.relevance, o.score, o.status, o.created_at,
       p.id AS product_id, p.name AS product, p.brand, p.category, p.price, p.commission,
       t.url, t.title, t.subreddit, t.traffic
FROM opportunities o
JOIN products p ON p.id = o.product_id
JOIN threads  t ON t.id = o.thread_id
"""


class StatusUpdate(BaseModel):
    status: str


# ----------------------------------------------------------------- settings
@app.get("/api/settings")
def get_settings():
    return rt.get_all()


@app.put("/api/settings")
def update_settings(body: dict):
    if pipeline.PIPELINE_STATE["running"]:
        raise HTTPException(409, "Settings can't be changed while a pipeline is running")
    try:
        effective = rt.set_many(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"saved": True, "settings": effective}


# ----------------------------------------------------------------- pipeline
@app.post("/api/pipeline/run")
def run_pipeline(body: dict | None = None):
    name_query = ((body or {}).get("product_name") or "").strip()
    started, err = pipeline.start_pipeline(name_query=name_query)
    if not started:
        raise HTTPException(409 if err == "Pipeline already running" else 400, err)
    return {"started": True, "mock_mode": config.MOCK_MODE, "product_name": name_query}


@app.post("/api/pipeline/cancel")
def cancel_pipeline():
    if not pipeline.request_cancel():
        raise HTTPException(409, "No pipeline is running")
    return {"cancelling": True}


@app.get("/api/pipeline/status")
def pipeline_status():
    state = {k: v for k, v in pipeline.PIPELINE_STATE.items() if k != "log"}
    state["log"] = list(pipeline.PIPELINE_STATE["log"])[-40:]
    state["mock_mode"] = config.MOCK_MODE
    state["credentials_error"] = pipeline.validate_credentials()
    return state


# ------------------------------------------------------------ opportunities
def _filters(search: str | None, status: str | None, product_id: str | None,
             min_traffic: int, min_score: float) -> tuple[str, list]:
    clauses, params = [], []
    if search:
        like = f"%{search}%"
        clauses.append("(p.name LIKE ? OR o.keyword LIKE ? OR t.url LIKE ? OR t.subreddit LIKE ? OR t.title LIKE ?)")
        params += [like] * 5
    if status:
        clauses.append("o.status = ?")
        params.append(status)
    if product_id:
        clauses.append("p.id = ?")
        params.append(product_id)
    if min_traffic > 0:
        clauses.append("t.traffic >= ?")
        params.append(min_traffic)
    if min_score > 0:
        clauses.append("o.score >= ?")
        params.append(min_score)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


SORTABLE = {"score": "o.score", "traffic": "t.traffic", "position": "o.position",
            "commission": "p.commission", "product": "p.name", "created_at": "o.created_at"}


@app.get("/api/opportunities")
def list_opportunities(
    search: str | None = None,
    status: str | None = None,
    product_id: str | None = None,
    min_traffic: int = 0,
    min_score: float = 0,
    sort: str = Query("score", pattern="^[a-z_]+$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(200, le=1000),
    offset: int = 0,
):
    where, params = _filters(search, status, product_id, min_traffic, min_score)
    sort_col = SORTABLE.get(sort, "o.score")
    with database.get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM ({BASE_QUERY}{where})", params).fetchone()["c"]
        rows = conn.execute(
            f"{BASE_QUERY}{where} ORDER BY {sort_col} {order.upper()} LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return {"total": total, "items": [dict(r) for r in rows]}


@app.patch("/api/opportunities/{opp_id}/status")
def update_status(opp_id: int, body: StatusUpdate):
    if body.status not in database.VALID_STATUSES:
        raise HTTPException(400, f"Status must be one of {sorted(database.VALID_STATUSES)}")
    with database.get_conn() as conn:
        cur = conn.execute("UPDATE opportunities SET status=? WHERE id=?", (body.status, opp_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Opportunity not found")
    return {"id": opp_id, "status": body.status}


@app.get("/api/products")
def list_products():
    with database.get_conn() as conn:
        rows = conn.execute("SELECT * FROM products ORDER BY commission DESC").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/stats")
def stats():
    with database.get_conn() as conn:
        s = {
            "products": conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"],
            "keywords": conn.execute("SELECT COUNT(*) c FROM keywords").fetchone()["c"],
            "threads": conn.execute("SELECT COUNT(*) c FROM threads").fetchone()["c"],
            "opportunities": conn.execute("SELECT COUNT(*) c FROM opportunities").fetchone()["c"],
            "total_traffic": conn.execute("SELECT COALESCE(SUM(traffic),0) s FROM threads").fetchone()["s"],
        }
        s["by_status"] = {
            r["status"]: r["c"]
            for r in conn.execute("SELECT status, COUNT(*) c FROM opportunities GROUP BY status")
        }
    return s


# ----------------------------------------------------------------- CSV export
@app.get("/api/export.csv")
def export_csv(
    search: str | None = None,
    status: str | None = None,
    product_id: str | None = None,
    min_traffic: int = 0,
    min_score: float = 0,
):
    where, params = _filters(search, status, product_id, min_traffic, min_score)
    with database.get_conn() as conn:
        rows = conn.execute(f"{BASE_QUERY}{where} ORDER BY o.score DESC", params).fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Product", "Brand", "Category", "Price", "Commission", "Keyword",
                     "Reddit URL", "Thread Title", "Subreddit", "Position", "Traffic",
                     "Relevance", "Opportunity Score", "Status", "Created At"])
    for r in rows:
        writer.writerow([r["product"], r["brand"], r["category"], r["price"], r["commission"],
                         r["keyword"], r["url"], r["title"], r["subreddit"], r["position"],
                         r["traffic"], r["relevance"], r["score"], r["status"], r["created_at"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=reddit_opportunities.csv"},
    )


# ----------------------------------------------------------------- frontend
_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html", headers=_NO_CACHE)


@app.get("/static/{filename}")
def static_file(filename: str):
    # Serve only known frontend assets, with no-cache so updates always load.
    safe = (FRONTEND_DIR / filename).resolve()
    if FRONTEND_DIR.resolve() not in safe.parents or not safe.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(safe, headers=_NO_CACHE)
