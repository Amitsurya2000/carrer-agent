"""Remotive remote-jobs API adapter.

Public JSON, no key required. Docs: https://remotive.com/api-documentation
"""
from __future__ import annotations

import re

import httpx

_UA = "ai-career-agent/1.0 (personal job search)"
_BASE = "https://remotive.com/api/remote-jobs"


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "")


def fetch_jobs(queries: list[str]) -> list[dict]:
    """One Remotive search per query; returns rows ready for the jobs table."""
    out: list[dict] = []
    for q in queries:
        try:
            r = httpx.get(_BASE, params={"search": q, "limit": 50}, timeout=30,
                          headers={"User-Agent": _UA})
            r.raise_for_status()
            jobs = r.json().get("jobs", [])
        except Exception:
            continue
        for j in jobs:
            url = j.get("url")
            if not url:
                continue
            out.append({
                "source": "remotive",
                "url": url,
                "title": j.get("title") or "Untitled",
                "company": j.get("company_name"),
                "location": j.get("candidate_required_location") or "Remote",
                "description": _strip_html(j.get("description") or "")[:8000],
                "status": "discovered",
                "raw": j,
            })
    return out
