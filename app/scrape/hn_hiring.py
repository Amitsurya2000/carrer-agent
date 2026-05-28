"""Playwright scraper: Hacker News 'Ask HN: Who is hiring?' monthly thread.

A real, scrape-friendly job source (no bot detection, no slider). Each top-level
comment in the latest "Who is hiring?" thread is one job post. We pull them all,
parse company/role/location heuristically, score against the active résumé, and
upsert into the `jobs` table (source='hn').

Visible Chromium — you watch every step. Run:  python -m app.scrape.hn_hiring
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from app.db import get_supabase
from app.profile_store import get_active
from app.score import keyword_score

ROOT = Path(__file__).resolve().parents[2]
USER_DATA = ROOT / ".browser_hn"
SHOTS = ROOT / "shots"
SHOTS.mkdir(exist_ok=True)

HN = "https://news.ycombinator.com"

# Filters can be passed by the web app via env vars (set in the subprocess call).
FILTER_LOCATION = os.environ.get("FILTER_LOCATION", "").strip()
FILTER_EXPERIENCE = os.environ.get("FILTER_EXPERIENCE", "").strip().lower()

_EXP_KEYWORDS = {
    "fresher": ("fresher", "entry-level", "entry level", "0-1 year", "0-2 year",
                "junior", "graduate", "intern", "new grad"),
    "mid":     ("mid-level", "mid level", "2-5 year", "3-5 year", "intermediate",
                "2+ year", "3+ year"),
    "senior":  ("senior", "lead ", "principal", "staff", "5+ year", "7+ year",
                "10+ year"),
}


def matches_filters(text: str) -> bool:
    """Return True if a job post passes the user's filters from the upload form."""
    if not text:
        return False
    low = text.lower()
    if FILTER_LOCATION:
        loc = FILTER_LOCATION.lower()
        # Pass if it mentions the location OR if it's a Remote post (still applicable).
        if loc not in low and "remote" not in low:
            return False
    if FILTER_EXPERIENCE:
        kws = _EXP_KEYWORDS.get(FILTER_EXPERIENCE, ())
        if kws and not any(k in low for k in kws):
            return False
    return True

# JS extractor — runs in the page, returns top-level comments + post timestamps in one call.
EXTRACT_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('tr.athing.comtr').forEach(tr => {
    const indImg = tr.querySelector('td.ind img');
    const indent = indImg ? parseInt(indImg.getAttribute('width') || '0', 10) : -1;
    if (indent !== 0) return;            // top-level only
    const textEl = tr.querySelector('div.commtext, span.commtext');
    if (!textEl) return;
    const ageEl = tr.querySelector('span.age');
    const ageTitle = ageEl ? (ageEl.getAttribute('title') || '') : '';
    const postedISO = ageTitle.split(' ')[0];   // HN puts ISO date in title=...
    const postedText = ageEl ? (ageEl.innerText || '').trim() : '';
    out.push({
      id: tr.id,
      text: textEl.innerText.trim(),
      posted_iso: postedISO,
      posted_text: postedText
    });
  });
  return out;
}
"""


def parse_title(text: str) -> str:
    first = text.split("\n", 1)[0].strip()
    return first[:200] or "Untitled"


def parse_company(text: str) -> str | None:
    first = text.split("\n", 1)[0].strip()
    for sep in (" | ", " - ", " – ", " · ", " / "):
        if sep in first:
            return first.split(sep, 1)[0].strip()[:120]
    return None


def parse_location(text: str) -> str | None:
    low = text.lower()
    if "remote" in low and "no remote" not in low:
        return "Remote"
    for city in ("bangalore", "bengaluru", "mumbai", "pune", "hyderabad", "chennai",
                 "delhi", "noida", "gurgaon", "san francisco", "new york", "london",
                 "berlin", "amsterdam", "singapore"):
        if city in low:
            return city.title()
    return None


# --- Steps ------------------------------------------------------------------
def open_whoishiring_submissions(page: Page) -> None:
    page.goto(f"{HN}/submitted?id=whoishiring", wait_until="domcontentloaded", timeout=30000)
    time.sleep(1)


def find_latest_thread_url(page: Page) -> str | None:
    """Find the first 'Who is hiring?' post on whoishiring's submissions page."""
    links = page.locator("span.titleline > a").all()
    for a in links:
        title = (a.inner_text() or "").strip()
        if title.lower().startswith("ask hn: who is hiring"):
            href = a.get_attribute("href") or ""
            if href.startswith("item?id="):
                return f"{HN}/{href}"
    return None


def open_thread(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    time.sleep(2)


def extract_top_level(page: Page) -> list[dict]:
    return page.evaluate(EXTRACT_JS) or []


def to_job_row(c: dict) -> dict:
    text = c.get("text", "")
    raw = {
        "hn_id": c["id"],
        "posted_text": c.get("posted_text", ""),
    }
    if c.get("posted_iso"):
        # Use the same `created` key Adzuna uses so the UI's posted-date and
        # expiry detection work uniformly across sources.
        raw["created"] = c["posted_iso"]
    return {
        "source": "hn",
        "url": f"{HN}/item?id={c['id']}",
        "title": parse_title(text),
        "company": parse_company(text),
        "location": parse_location(text),
        "description": text[:8000],
        "status": "discovered",
        "raw": raw,
    }


# --- Orchestrator -----------------------------------------------------------
def run() -> dict:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA),
            headless=False, args=["--start-maximized"], no_viewport=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        print(">>> Step 1: opening HN whoishiring submissions")
        open_whoishiring_submissions(page)
        page.screenshot(path=str(SHOTS / "hn_step1_submissions.png"))

        print(">>> Step 2: finding the latest 'Who is hiring?' thread")
        thread_url = find_latest_thread_url(page)
        if not thread_url:
            print("could not find a Who is hiring thread")
            ctx.close()
            return {"comments": 0, "inserted": 0}
        print(f"    -> {thread_url}")

        print(">>> Step 3: opening the thread")
        open_thread(page, thread_url)
        page.screenshot(path=str(SHOTS / "hn_step2_thread.png"))

        print(">>> Step 4: extracting top-level comments")
        comments = extract_top_level(page)
        print(f"    found {len(comments)} top-level posts")
        if FILTER_LOCATION or FILTER_EXPERIENCE:
            print(f"    applying filters: location={FILTER_LOCATION!r}, experience={FILTER_EXPERIENCE!r}")

        # Build rows, apply filters, score against active résumé.
        active = get_active()
        skills = (active or {}).get("skills") or []
        rows = []
        skipped_by_filter = 0
        for c in comments:
            row = to_job_row(c)
            if (FILTER_LOCATION or FILTER_EXPERIENCE) and not matches_filters(row.get("description") or ""):
                skipped_by_filter += 1
                continue
            if not skills:
                row["status"] = "discovered"
            else:
                s, reason = keyword_score(skills, row)
                row["score"] = s
                row["score_reason"] = reason
                row["status"] = "scored"
            rows.append(row)
        if skipped_by_filter:
            print(f"    filtered out: {skipped_by_filter}; keeping {len(rows)}")

        if rows:
            print(">>> Step 5: upserting to Supabase")
            sb = get_supabase()
            sb.table("jobs").upsert(rows, on_conflict="url").execute()

        # Show a few top matches
        if rows and skills:
            rows.sort(key=lambda r: r.get("score") or 0, reverse=True)
            print("\nTop HN matches:")
            for r in rows[:6]:
                print(f"  {r.get('score', '-'):>3}  {r['title'][:60]}  ({r.get('score_reason','')})")

        time.sleep(4)  # let the user see the result
        ctx.close()
        return {"comments": len(comments), "inserted": len(rows)}


if __name__ == "__main__":
    print(run())
