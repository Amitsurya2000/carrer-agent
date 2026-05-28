"""Enrich top jobs with their FULL description using Playwright.

The Adzuna API returns only a short, truncated description. For the highest-scoring jobs,
this opens each job's actual posting page (its `redirect_url`) in a real headless browser,
grabs the full visible text, stores it, and re-scores with the richer text. This is the
right job for Playwright: getting data the API can't, on a handful of jobs you care about
— a normal browser, low volume, NOT stealth scraping a job board's search results.

Local-only (Playwright/Chromium aren't in the deployed server image).
Run:  python -m app.enrich
"""
from __future__ import annotations

import time

from playwright.sync_api import sync_playwright

from app.db import get_supabase
from app.profile_store import get_active
from app.score import keyword_score

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def enrich_top(limit: int = 8) -> dict:
    sb = get_supabase()
    jobs = (sb.table("jobs").select("id,url,title,description")
            .gt("score", 15).order("score", desc=True).limit(limit).execute().data)
    skills = (get_active() or {}).get("skills") or []
    enriched = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(user_agent=_UA).new_page()
        for job in jobs:
            url = job.get("url")
            if not url:
                continue
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(700)
                full = (page.evaluate("() => document.body ? document.body.innerText : ''") or "").strip()
            except Exception as e:
                print(f"  skip  {job['title'][:38]:38} -> {str(e)[:50]}")
                continue
            if len(full) > len(job.get("description") or ""):
                s, reason = keyword_score(skills, {"title": job["title"], "description": full})
                sb.table("jobs").update(
                    {"description": full[:8000], "score": s, "score_reason": reason}
                ).eq("id", job["id"]).execute()
                enriched += 1
                print(f"  ok    {job['title'][:38]:38} -> {len(full):>5} chars, score {s}")
            time.sleep(0.4)  # be polite
        browser.close()

    return {"considered": len(jobs), "enriched": enriched}


if __name__ == "__main__":
    print(enrich_top())
