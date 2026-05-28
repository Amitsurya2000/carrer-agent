"""FastAPI app — the orchestrator. Phase 0: health + a jobs listing endpoint."""
from fastapi import FastAPI, HTTPException

from app.config import settings
from app.db import get_supabase
from app.discover import discover as run_discovery
from app.score import score_pending, score_pending_keyword
from app.web import router as web_router

app = FastAPI(title="AI Career Agent", version="0.1.0")
app.include_router(web_router)  # serves the browser UI at "/"


@app.get("/health")
def health() -> dict:
    """Liveness check. Works even before Supabase is configured."""
    return {"status": "ok", "configured": settings.is_configured}


@app.get("/jobs")
def list_jobs(limit: int = 50):
    """Most recently discovered jobs. Requires Supabase to be configured."""
    if not settings.is_configured:
        raise HTTPException(503, "Supabase not configured yet — fill in .env")
    sb = get_supabase()
    res = (
        sb.table("jobs")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data


@app.post("/discover")
def discover_jobs():
    """Run discovery against the configured source and upsert new jobs."""
    if not settings.is_configured:
        raise HTTPException(503, "Supabase not configured yet — fill in .env")
    try:
        return run_discovery()
    except RuntimeError as e:  # e.g. Adzuna keys missing
        raise HTTPException(503, str(e))


@app.post("/score")
def score_jobs(limit: int = 25):
    """Score discovered jobs with the LLM and write back score + reason."""
    if not settings.is_configured:
        raise HTTPException(503, "Supabase not configured yet — fill in .env")
    try:
        return score_pending(limit=limit)
    except RuntimeError as e:  # e.g. Anthropic key missing or no profile row
        raise HTTPException(503, str(e))


@app.post("/score/keyword")
def score_jobs_keyword(limit: int = 1000):
    """Free keyword scoring (no LLM) using the active résumé."""
    if not settings.is_configured:
        raise HTTPException(503, "Supabase not configured yet — fill in .env")
    try:
        return score_pending_keyword(limit=limit)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
