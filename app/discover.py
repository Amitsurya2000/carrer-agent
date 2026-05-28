"""Phase 1 / multi-source discovery.

Pulls jobs from every legitimate JSON API we know — Adzuna (the aggregator backbone)
plus Remotive and RemoteOK — dedupes by URL, and upserts into `jobs`. NO scraping,
no anti-bot fights. To add sites that block bots (Wellfound/LinkedIn/Naukri), the user
pastes URLs through the web UI's "Add a job" form.

Run:  python -m app.discover
Or via the API:  POST /discover  (or use the "Refresh matches" button in the UI)
"""
from __future__ import annotations

from app.db import get_supabase
from app.sources import adzuna, remoteok, remotive

# Default résumé-derived queries (override per call from upload/refresh).
QUERIES = [
    "computer vision",
    "machine learning engineer",
    "ai engineer",
    "llm",
    "data scientist",
    "data analyst",
]
COUNTRY = "in"  # Adzuna country code; other sources are remote-global


def discover(queries: list[str] | None = None, country: str = COUNTRY) -> dict:
    """Fetch from every source, dedupe by url, insert only jobs we haven't seen.

    Returns a per-source breakdown so the user can see what came from where.
    """
    queries = queries or QUERIES
    rows: dict[str, dict] = {}                 # url -> row (dedupes across sources)
    breakdown: dict[str, int] = {}             # source name -> count

    SOURCES = [
        ("adzuna",   lambda qs: adzuna.fetch_jobs(qs, country=country)),
        ("remotive", remotive.fetch_jobs),
        ("remoteok", remoteok.fetch_jobs),
    ]

    for name, fn in SOURCES:
        try:
            jobs = fn(queries)
        except Exception as e:
            print(f"  {name}: ERROR {e}")
            breakdown[name] = 0
            continue
        breakdown[name] = len(jobs)
        for row in jobs:
            url = row.get("url")
            if url and url not in rows:
                rows[url] = row
        print(f"  {name}: {len(jobs)} fetched")

    if not rows:
        return {"fetched": 0, "inserted": 0, "by_source": breakdown}

    sb = get_supabase()
    # ON CONFLICT (url) DO NOTHING — re-running never clobbers a job's later-phase state.
    res = (sb.table("jobs")
           .upsert(list(rows.values()), on_conflict="url", ignore_duplicates=True)
           .execute())
    return {
        "fetched": len(rows),
        "inserted": len(res.data),
        "by_source": breakdown,
    }


if __name__ == "__main__":
    print(discover())
