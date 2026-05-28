"""Visible Playwright scraper for Naukri.com — vanilla, no stealth, no patchright.

Opens a real Chromium window on screen, visits Naukri's public search URLs derived
from the active résumé, screenshots each step, attempts to extract job cards, and
honestly reports what happened (block / login wall / actual jobs).

Run:  python -m app.scrape.naukri
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from app.db import get_supabase
from app.profile_store import get_active
from app.resume import derive_queries
from app.score import keyword_score

ROOT = Path(__file__).resolve().parents[2]
USER_DATA = ROOT / ".browser_naukri"
SHOTS = ROOT / "shots"
SHOTS.mkdir(exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# JS extractor — tries multiple selectors Naukri has used over the years.
EXTRACT_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const sel = 'article.jobTuple, div.srp-tuple, div.jobTupleHeader, '
            + '[data-job-id], div.row1, .styles_job-listing-container__-ldcN, '
            + '.list-job-card, a.title';
  document.querySelectorAll(sel).forEach(card => {
    const a = card.matches('a') ? card : card.querySelector('a.title, a[title], a.subTitle, .jobTitle a, a[class*="title"]');
    if (!a) return;
    const url = a.href || '';
    if (!url || seen.has(url)) return;
    seen.add(url);
    const title = (a.innerText || a.getAttribute('title') || '').trim();
    const com   = (card.querySelector('a.subTitle, .companyName, [class*="comp"]') || {}).innerText || '';
    const loc   = (card.querySelector('.locWdth, .location, [class*="loc"]') || {}).innerText || '';
    out.push({title, url, company: com.trim(), location: loc.trim()});
  });
  return out;
}
"""


def _slug(q: str) -> str:
    return re.sub(r"\W+", "-", q.lower()).strip("-")


def _block_indicators(page: Page) -> str | None:
    """Detect common bot-block / challenge pages. Returns a label if blocked."""
    try:
        content = page.content().lower()
    except Exception:
        return "page error"
    if "verification required" in content or "verify you are human" in content:
        return "CAPTCHA / verification"
    if "access denied" in content or "temporarily restricted" in content:
        return "access denied"
    if "request blocked" in content or "blocked by" in content:
        return "blocked"
    if "cloudflare" in content and "challenge" in content:
        return "cloudflare challenge"
    if "just a moment" in content:
        return "cloudflare 'Just a moment...'"
    return None


def run() -> dict:
    active = get_active()
    if not active:
        print("No active résumé — upload one first.")
        return {"jobs": 0}
    queries = derive_queries(active)[:3]   # cap to 3 to be polite
    print(f"Naukri search queries (from résumé): {queries}")
    skills = active.get("skills") or []

    aggregated: list[dict] = []
    per_query: list[dict] = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA),
            headless=False,
            args=["--start-maximized"],
            no_viewport=True,
            user_agent=UA,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        for i, q in enumerate(queries, start=1):
            slug = _slug(q)
            url = f"https://www.naukri.com/{slug}-jobs"
            print(f"\n>>> [{i}/{len(queries)}] Visiting: {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3500)
            except Exception as e:
                print(f"    nav failed: {e}")
                per_query.append({"query": q, "url": url, "result": f"nav error: {str(e)[:60]}", "jobs": 0})
                continue

            shot = SHOTS / f"naukri_{i}_{slug}.png"
            page.screenshot(path=str(shot))
            block = _block_indicators(page)
            if block:
                print(f"    {block.upper()} detected — page title: {page.title()!r}")
                per_query.append({"query": q, "url": url, "result": f"BLOCKED ({block})", "jobs": 0, "shot": str(shot)})
                continue

            try:
                jobs = page.evaluate(EXTRACT_JS) or []
            except Exception as e:
                jobs = []
                print(f"    extract error: {e}")

            print(f"    extracted: {len(jobs)} job cards")
            per_query.append({"query": q, "url": page.url, "result": f"ok, {len(jobs)} jobs",
                              "jobs": len(jobs), "shot": str(shot)})
            for j in jobs:
                if j.get("url"):
                    aggregated.append({
                        "source": "naukri", "url": j["url"], "title": j.get("title") or "Untitled",
                        "company": j.get("company"), "location": j.get("location"),
                        "description": "", "status": "discovered",
                        "raw": {"query": q},
                    })
            time.sleep(2)
        ctx.close()

    # Dedup + score
    by_url = {j["url"]: j for j in aggregated}
    rows = list(by_url.values())
    for r in rows:
        s, reason = keyword_score(skills, r)
        r["score"] = s
        r["score_reason"] = reason
        r["status"] = "scored"

    if rows:
        sb = get_supabase()
        sb.table("jobs").upsert(rows, on_conflict="url").execute()

    print("\n=== Per-query result ===")
    for r in per_query:
        print(f"  {r['query']:35} -> {r['result']}")
    print(f"\nTotal unique Naukri jobs saved: {len(rows)}")
    return {"jobs": len(rows), "per_query": per_query}


if __name__ == "__main__":
    print(run())
