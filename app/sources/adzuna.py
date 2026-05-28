"""Adzuna job-search API adapter.

Free key: https://developer.adzuna.com/  (create an app -> get app_id + app_key)

We use Adzuna for India-based listings because Naukri/Indeed expose no clean public
API. Adzuna returns structured JSON, so Phase 1 needs no scraping / anti-bot handling.
"""
from __future__ import annotations

import httpx

from app.config import settings

BASE = "https://api.adzuna.com/v1/api/jobs"


def fetch_adzuna(
    query: str,
    country: str = "in",
    page: int = 1,
    results_per_page: int = 50,
) -> list[dict]:
    """Return raw Adzuna results for one search query (one page)."""
    if not (settings.adzuna_app_id and settings.adzuna_app_key):
        raise RuntimeError("ADZUNA_APP_ID / ADZUNA_APP_KEY missing in .env")

    url = f"{BASE}/{country}/search/{page}"
    params = {
        "app_id": settings.adzuna_app_id,
        "app_key": settings.adzuna_app_key,
        "what": query,
        "results_per_page": results_per_page,
        "content-type": "application/json",
    }
    resp = httpx.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("results", [])


def to_job_row(result: dict) -> dict:
    """Map one Adzuna result onto a `jobs` table row.

    Note: Adzuna's search `description` is truncated. That's fine for Phase 2 scoring;
    if we need the full JD later we can follow `redirect_url`.
    """
    return {
        "source": "adzuna",
        "url": result.get("redirect_url"),
        "title": result.get("title") or "Untitled",
        "company": (result.get("company") or {}).get("display_name"),
        "location": (result.get("location") or {}).get("display_name"),
        "description": result.get("description"),
        "status": "discovered",
        "raw": result,
    }


def fetch_jobs(queries: list[str], country: str = "in") -> list[dict]:
    """Unified source interface: one Adzuna search per query, return ready-to-upsert rows."""
    out: list[dict] = []
    for q in queries:
        try:
            for r in fetch_adzuna(q, country=country):
                out.append(to_job_row(r))
        except Exception:
            continue
    return out
