"""Find HR / talent acquisition contact emails for each saved job's company.

After the main 108-site scrape finishes, this script:
  1. Pulls the unique (company, url) pairs from the jobs table
  2. For each company, derives a likely domain (from the source URL if it's
     a company career page, or by slugifying the company name)
  3. Headless Chromium visits up to 3 candidate URLs per company:
        https://{domain}/contact
        https://{domain}/careers
        https://{domain}/about
  4. Regex-extracts every email on each page
  5. Filters to the ones that look HR/recruiting (careers@, hr@, jobs@,
     talent@, recruit@, hiring@, people@) and prefer ones on the same domain
  6. Upserts the found emails into jobs.raw.hr_emails

Run:  python -m app.scrape.contact_finder
"""
from __future__ import annotations

import os
import re
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from app.db import get_supabase

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADLESS = os.environ.get("HEADLESS_BROWSER", "").strip() == "1"
LAUNCH_ARGS = (["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
               if HEADLESS else ["--start-maximized"])

# How many companies to enrich per run. Each company adds ~6-12s, so the cap
# keeps total runtime bounded.
MAX_COMPANIES = int(os.environ.get("CONTACT_LIMIT", "20") or 20)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", re.IGNORECASE)
HR_KEYWORDS = ("career", "hr@", "talent", "recruit", "job@", "jobs@",
               "hiring", "people@", "ta@", "joinus", "join@", "apply@")
PATH_CANDIDATES = ("contact", "careers/contact", "about/contact",
                   "careers", "about", "contact-us")

# Domains we will NEVER treat as the company's own — they're job aggregators
# that won't host the company's HR contact info.
JOB_BOARD_DOMAINS = {
    "naukri.com", "linkedin.com", "indeed.com", "wellfound.com", "foundit.in",
    "glassdoor.com", "glassdoor.co.in", "hirist.tech", "cutshort.io",
    "instahyre.com", "shine.com", "timesjobs.com", "apna.co", "internshala.com",
    "jobsforher.com", "workindia.in", "monster.com", "ycombinator.com",
    "news.ycombinator.com",
}


def _slug(s: str) -> str:
    return re.sub(r"\W+", "", s.lower())


def _domain_for(company: str, source_url: str) -> str | None:
    """Pick a domain to scrape. Prefer the source URL's host if it's NOT a
    job board, otherwise slugify the company name + .com."""
    if source_url:
        try:
            host = urlparse(source_url).netloc.lower().lstrip("www.")
            if host and not any(b in host for b in JOB_BOARD_DOMAINS):
                # Strip subdomain like "careers.tcs.com" -> "tcs.com"
                parts = host.split(".")
                if len(parts) >= 3 and parts[0] in {"careers", "jobs", "talent", "www", "apply"}:
                    host = ".".join(parts[1:])
                return host
        except Exception:
            pass
    if company:
        s = _slug(company)
        if s and len(s) >= 2:
            return f"{s}.com"
    return None


def _hr_emails(text: str, domain: str) -> list[str]:
    """Return up to 3 HR-flavoured emails found on the page.
    Prefer ones that share the company's domain."""
    seen = set()
    raw = []
    for m in EMAIL_RE.finditer(text or ""):
        e = m.group(0).lower().rstrip(".,;:)")
        if e not in seen:
            seen.add(e); raw.append(e)
    hr = [e for e in raw if any(k in e for k in HR_KEYWORDS)]
    if not hr:
        return []
    # Prefer same-domain HR emails, fall back to any HR email
    same = [e for e in hr if domain.split(".")[0] in e]
    return (same or hr)[:3]


def run() -> dict:
    sb = get_supabase()
    rows = (sb.table("jobs")
            .select("id,url,company,raw")
            .gt("score", 15)
            .order("score", desc=True)
            .limit(150).execute().data)

    # Group by company so we visit each domain ONCE.
    seen_companies: set[str] = set()
    candidates: list[tuple[str, str, list[str]]] = []  # (company, domain, [job_ids])
    by_company: dict[str, list[str]] = {}
    for r in rows:
        c = (r.get("company") or "").strip()
        if not c or c.lower() in seen_companies:
            by_company.setdefault(c, []).append(r["id"]) if c else None
            continue
        seen_companies.add(c.lower())
        domain = _domain_for(c, r.get("url"))
        if not domain:
            continue
        by_company.setdefault(c, []).append(r["id"])
        candidates.append((c, domain, by_company[c]))
        if len(candidates) >= MAX_COMPANIES:
            break

    # Make sure by_company has ALL job_ids per company (loop above might miss dupes)
    by_company.clear()
    for r in rows:
        c = (r.get("company") or "").strip()
        if c:
            by_company.setdefault(c, []).append(r["id"])

    print(f"Contact finder: {len(candidates)} companies to enrich")
    if not candidates:
        return {"updated": 0}

    found_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, args=LAUNCH_ARGS)
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()
        page.set_default_timeout(12000)

        for i, (company, domain, _ids) in enumerate(candidates, 1):
            print(f"[{i}/{len(candidates)}] {company} → {domain}")
            emails_found: list[str] = []
            for path in PATH_CANDIDATES:
                url = f"https://{domain}/{path}"
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=10000)
                    page.wait_for_timeout(1200)
                    text = page.content()
                except Exception:
                    continue
                e = _hr_emails(text, domain)
                for x in e:
                    if x not in emails_found:
                        emails_found.append(x)
                if len(emails_found) >= 3:
                    break

            if emails_found:
                found_count += 1
                print(f"   ✓ {emails_found}")
                # Apply to every job from this company
                job_ids = by_company.get(company, [])
                for jid in job_ids:
                    try:
                        cur = (sb.table("jobs").select("raw").eq("id", jid).limit(1).execute().data or [{}])[0]
                        raw = cur.get("raw") or {}
                        raw["hr_emails"] = emails_found
                        sb.table("jobs").update({"raw": raw}).eq("id", jid).execute()
                    except Exception:
                        pass
            else:
                print(f"   ✗ no HR emails found on {domain}")
            time.sleep(0.4)

        try: browser.close()
        except Exception: pass

    print(f"\nContact finder done: {found_count}/{len(candidates)} companies got emails.")
    return {"updated": found_count, "total": len(candidates)}


if __name__ == "__main__":
    print(run())
