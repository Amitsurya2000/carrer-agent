"""Vercel entry point.

Vercel's @vercel/python builder discovers the FastAPI `app` symbol exported here
and serves it as an ASGI app. All routes defined on `app` (web UI at `/`,
`/health`, `/jobs`, `/ui/upload`, etc.) become available at the deploy URL.

Local-only features (visible Playwright scraping, subprocess spawning) are
guarded inside web.py by checking the VERCEL env var Vercel sets automatically.
"""
from app.main import app  # noqa: F401  -- imported for Vercel to discover
