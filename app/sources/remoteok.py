"""RemoteOK API adapter (https://remoteok.com/api).

Public JSON, no key. Filters by tag rather than free-text. We map our résumé-derived
queries to known tech tags.
"""
from __future__ import annotations

import re

import httpx

_UA = "ai-career-agent/1.0 (personal job search)"
_BASE = "https://remoteok.com/api"

# Maps query phrases we derive from résumés to RemoteOK's tag vocabulary.
_TAGS = {
    "computer vision": "computer-vision",
    "machine learning": "ml",
    "machine learning engineer": "ml",
    "deep learning": "deep-learning",
    "data scientist": "data-science",
    "data science": "data-science",
    "data analyst": "data",
    "ai engineer": "ai",
    "ai": "ai",
    "llm": "ai",
    "rag": "ai",
    "python": "python",
    "fastapi": "python",
    "backend": "backend",
    "frontend": "frontend",
    "react": "react",
    "devops": "devops",
}


def _queries_to_tags(queries: list[str]) -> list[str]:
    tags: list[str] = []
    for q in queries:
        t = _TAGS.get((q or "").lower())
        if t and t not in tags:
            tags.append(t)
    return tags or ["dev"]


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "")


def fetch_jobs(queries: list[str]) -> list[dict]:
    out: list[dict] = []
    for tag in _queries_to_tags(queries):
        try:
            r = httpx.get(_BASE, params={"tag": tag}, timeout=30,
                          headers={"User-Agent": _UA})
            r.raise_for_status()
            data = r.json()
        except Exception:
            continue
        # First element is metadata; rest are jobs.
        for j in data:
            if not isinstance(j, dict) or not j.get("position"):
                continue
            url = j.get("url") or j.get("apply_url")
            if not url:
                continue
            out.append({
                "source": "remoteok",
                "url": url,
                "title": j.get("position") or "Untitled",
                "company": j.get("company"),
                "location": j.get("location") or "Remote",
                "description": _strip_html(j.get("description") or "")[:8000],
                "status": "discovered",
                "raw": j,
            })
    return out
