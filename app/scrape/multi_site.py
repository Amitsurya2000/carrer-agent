"""Visible multi-site Playwright scraper — opens a NEW Chromium window per site.

Each site has a *category*: 'board' (general job board, covers all sizes), 'startup',
'midlevel' (IT services), or 'mnc'. The upload form's "Company size" dropdown sets the
FILTER_COMPANY_SIZE env var, which restricts which sites run. The "Location" field sets
FILTER_LOCATION, which is injected into the URL of every site that supports a location
parameter (Naukri, LinkedIn, Indeed, Foundit, Shine, Glassdoor, Google, Amazon).

Vanilla Playwright. NO stealth, NO patchright.
Run:  python -m app.scrape.multi_site
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from app.db import get_supabase
from app.profile_store import get_active
from app.resume import derive_queries
from app.score import keyword_score

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "shots"
SHOTS.mkdir(exist_ok=True)

# Live-feed state file — read by the FastAPI /api/scrape/state endpoint so
# the home page can show a real-time panel of which site Chromium is on
# right now and the screenshot it just took.
SCRAPE_STATE = ROOT / "scrape_state.json"


def _write_state(state: dict) -> None:
    try:
        import json
        with open(SCRAPE_STATE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def _clear_state() -> None:
    try:
        if SCRAPE_STATE.exists():
            SCRAPE_STATE.unlink()
    except Exception:
        pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# HEADLESS_BROWSER=1 is set by Dockerfile (Render / Railway / Fly), so cloud
# runs are automatically headless with container-safe Chromium flags. Local
# runs keep the visible window with --start-maximized.
HEADLESS = os.environ.get("HEADLESS_BROWSER", "").strip() == "1"
_LAUNCH_ARGS = (["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                if HEADLESS else ["--start-maximized"])

FILTER_LOCATION = os.environ.get("FILTER_LOCATION", "").strip()
FILTER_COMPANY_SIZE = os.environ.get("FILTER_COMPANY_SIZE", "").strip().lower()
# FILTER_ROLE — user-typed role/title from the upload form. When non-empty,
# this overrides the auto-detected role from the résumé so Chromium searches
# for EXACTLY what the user wants ("computer vision engineer", "staff nurse",
# etc.) on every platform.
FILTER_ROLE = os.environ.get("FILTER_ROLE", "").strip()
FILTER_EXPERIENCE = os.environ.get("FILTER_EXPERIENCE", "").strip().lower()
FILTER_DATE_POSTED = os.environ.get("FILTER_DATE_POSTED", "").strip()  # "1" / "3" / "7" / "30"
FILTER_WORK_MODE = os.environ.get("FILTER_WORK_MODE", "").strip().lower()  # "onsite" / "hybrid" / "remote"
FILTER_JOB_TYPE = os.environ.get("FILTER_JOB_TYPE", "").strip().lower()  # "fulltime" / etc.
# SCRAPE_LIMIT — empty / 0 means no cap (full 108-site sweep). Otherwise stop after N jobs.
try:
    SCRAPE_LIMIT = int(os.environ.get("SCRAPE_LIMIT", "").strip() or 0)
except ValueError:
    SCRAPE_LIMIT = 0


def _inject_advanced_filters(site_name: str, url: str) -> str:
    """Append per-site URL parameters for date posted / work mode / job type / experience.

    Each major board uses a different param name and value scheme — encoded here once.
    Sites without URL-level support for a filter silently keep their global feed.
    """
    sep = "&" if "?" in url else "?"
    extra: list[str] = []

    if site_name == "naukri":
        if FILTER_DATE_POSTED in {"1", "3", "7", "15", "30"}:
            extra.append(f"jobAge={FILTER_DATE_POSTED}")
        if FILTER_WORK_MODE == "remote":
            extra.append("wfhType=2")
    elif site_name == "linkedin":
        tpr_map = {"1": "r86400", "3": "r259200", "7": "r604800", "30": "r2592000"}
        if FILTER_DATE_POSTED in tpr_map:
            extra.append(f"f_TPR={tpr_map[FILTER_DATE_POSTED]}")
        wt_map = {"onsite": "1", "remote": "2", "hybrid": "3"}
        if FILTER_WORK_MODE in wt_map:
            extra.append(f"f_WT={wt_map[FILTER_WORK_MODE]}")
        jt_map = {"fulltime": "F", "parttime": "P", "contract": "C",
                  "temporary": "T", "internship": "I", "freelance": "C"}
        if FILTER_JOB_TYPE in jt_map:
            extra.append(f"f_JT={jt_map[FILTER_JOB_TYPE]}")
        exp_map = {"fresher": "1,2", "mid": "3,4", "senior": "5,6"}
        if FILTER_EXPERIENCE in exp_map:
            extra.append(f"f_E={exp_map[FILTER_EXPERIENCE]}")
    elif site_name == "indeed":
        if FILTER_DATE_POSTED in {"1", "3", "7", "14"}:
            extra.append(f"fromage={FILTER_DATE_POSTED}")
        elif FILTER_DATE_POSTED == "30":
            extra.append("fromage=14")  # Indeed caps at 14
        jt_map = {"fulltime": "fulltime", "parttime": "parttime", "contract": "contract",
                  "internship": "internship", "temporary": "temporary"}
        if FILTER_JOB_TYPE in jt_map:
            extra.append(f"jt={jt_map[FILTER_JOB_TYPE]}")
        if FILTER_WORK_MODE == "remote":
            extra.append("remotejob=1")
    elif site_name == "foundit":
        if FILTER_DATE_POSTED in {"1", "3", "7", "30"}:
            extra.append(f"freshness={FILTER_DATE_POSTED}")
    elif site_name == "glassdoor":
        if FILTER_DATE_POSTED in {"1", "3", "7", "14"}:
            extra.append(f"fromAge={FILTER_DATE_POSTED}")

    return url + (sep + "&".join(extra) if extra else "")


def _slug(q: str) -> str:
    return re.sub(r"\W+", "-", q.lower()).strip("-")


def _blocked(page: Page) -> str | None:
    try:
        text = page.content().lower()
    except Exception:
        return "page error"
    for needle, label in (
        ("verification required", "CAPTCHA"),
        ("verify you are human", "CAPTCHA"),
        ("verify you're not a robot", "CAPTCHA"),
        ("access denied", "access denied"),
        ("temporarily restricted", "IP restricted"),
        ("just a moment", "Cloudflare challenge"),
        ("attention required", "Cloudflare challenge"),
        ("/uas/login", "login wall"),
        ("sign in to continue", "login wall"),
        ("join linkedin", "login wall"),
    ):
        if needle in text:
            return label
    return None


_EXTRACT_JS = r"""
(selectors) => {
    const out = [];
    const seen = new Set();
    selectors.forEach(sel => {
        document.querySelectorAll(sel).forEach(card => {
            const a = card.matches('a') ? card : card.querySelector('a[href]');
            if (!a) return;
            const url = a.href || '';
            if (!url || seen.has(url) || url.startsWith('javascript:')) return;
            seen.add(url);
            const title = (a.innerText || a.getAttribute('title') || '').trim().split('\n')[0];
            const com = (card.querySelector('a[class*="comp"], [class*="company"], [class*="employer"], h4') || {}).innerText || '';
            const loc = (card.querySelector('[class*="loc"], [class*="city"], [class*="region"]') || {}).innerText || '';
            if (title && title.length < 200) {
                out.push({ url, title, company: com.trim(), location: loc.trim() });
            }
        });
    });
    return out;
}
"""


# Each site: name, category, build_url(q, loc), selectors, wait_ms.
# category in {"board", "startup", "midlevel", "mnc"}
SITES: list[dict] = [
    # ============ INDIAN JOB BOARDS (cover all sizes) ============
    {"name": "naukri", "category": "board",
     "build_url": lambda q, loc: f"https://www.naukri.com/{_slug(q)}-jobs" + (f"-in-{_slug(loc)}" if loc else ""),
     "selectors": ["article.jobTuple", "div.srp-tuple", "[class*='jobTupleHeader']", ".cust-job-tuple"],
     "wait_ms": 4000},
    {"name": "linkedin", "category": "board",
     "build_url": lambda q, loc: f"https://www.linkedin.com/jobs/search/?keywords={q.replace(' ', '%20')}&location={(loc or 'India').replace(' ', '%20')}",
     "selectors": ["div.base-card", "li.job-search-card", "[data-job-id]", "div.job-card-container"],
     "wait_ms": 4000},
    {"name": "wellfound", "category": "board",
     "build_url": lambda q, loc: f"https://wellfound.com/role/{_slug(q)}",
     "selectors": ["[class*='styles_jobContainer']", "[class*='JobListing']", "div.job-listing"],
     "wait_ms": 4500},
    {"name": "indeed", "category": "board",
     "build_url": lambda q, loc: f"https://in.indeed.com/jobs?q={q.replace(' ', '+')}&l={(loc or 'India').replace(' ', '+')}",
     "selectors": ["div.job_seen_beacon", "div.cardOutline", "[data-jk]"],
     "wait_ms": 4000},
    {"name": "foundit", "category": "board",
     "build_url": lambda q, loc: f"https://www.foundit.in/srp/results?query={q.replace(' ', '+')}" + (f"&locations={loc.replace(' ', '+')}" if loc else ""),
     "selectors": ["[class*='srpResultCard']", "[class*='jobTuple']", "div.cardContainer"],
     "wait_ms": 4000},
    {"name": "shine", "category": "board",
     "build_url": lambda q, loc: f"https://www.shine.com/job-search/{_slug(q)}-jobs" + (f"-in-{_slug(loc)}" if loc else ""),
     "selectors": ["div.jobCard", "[class*='job-search-card']", "div.job_card"],
     "wait_ms": 4000},
    {"name": "cutshort", "category": "board",
     "build_url": lambda q, loc: f"https://cutshort.io/search?q={q.replace(' ', '+')}",
     "selectors": ["[class*='JobCard']", "[class*='job-card']", "a[href*='/jobs/']"],
     "wait_ms": 4000},
    {"name": "instahyre", "category": "board",
     "build_url": lambda q, loc: f"https://www.instahyre.com/search/?q={q.replace(' ', '+')}",
     "selectors": ["div.job-info", "[class*='job']", "a[href*='/job/']"],
     "wait_ms": 4000},
    {"name": "hirist", "category": "board",
     "build_url": lambda q, loc: f"https://www.hirist.tech/k/{_slug(q)}-jobs",
     "selectors": ["[class*='jobCard']", "[class*='job_card']", "a[href*='/j/']"],
     "wait_ms": 4000},
    {"name": "timesjobs", "category": "board",
     "build_url": lambda q, loc: f"https://www.timesjobs.com/candidate/job-search.html?txtKeywords={q.replace(' ', '+')}",
     "selectors": ["li.job-bx", "[class*='job-bx']", "a[href*='/job-detail']"],
     "wait_ms": 4000},
    {"name": "glassdoor", "category": "board",
     "build_url": lambda q, loc: f"https://www.glassdoor.co.in/Job/jobs.htm?sc.keyword={q.replace(' ', '+')}",
     "selectors": ["[class*='job-listing']", "li.react-job-listing", "[data-test='job-link']"],
     "wait_ms": 4500},
    {"name": "apna", "category": "board",
     "build_url": lambda q, loc: f"https://apna.co/jobs/{_slug(q)}-jobs" + (f"-in-{_slug(loc)}" if loc else ""),
     "selectors": ["a[href*='/jobs/']", "[class*='job-card']", "[class*='JobCard']"], "wait_ms": 4000},
    {"name": "internshala", "category": "board",
     "build_url": lambda q, loc: f"https://internshala.com/jobs/{_slug(q)}-jobs",
     "selectors": ["a[href*='/job/']", "[class*='internship_meta']", "[class*='individual_internship']"], "wait_ms": 4000},
    {"name": "jobsforher", "category": "board",
     "build_url": lambda q, loc: f"https://www.jobsforher.com/jobs/keyword/{_slug(q)}",
     "selectors": ["a[href*='/jobs/']", "[class*='job-card']", "[class*='listing']"], "wait_ms": 4000},
    {"name": "workindia", "category": "board",
     "build_url": lambda q, loc: f"https://www.workindia.in/{_slug(q)}-jobs",
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='card']"], "wait_ms": 4000},

    # ============ STARTUPS / UNICORNS ============
    {"name": "razorpay", "category": "startup",
     "build_url": lambda q, loc: "https://razorpay.com/jobs/",
     "selectors": ["a[href*='razorpay.com/jobs/']", "div.job-listing", "li.job"], "wait_ms": 3500},
    {"name": "zomato", "category": "startup",
     "build_url": lambda q, loc: "https://www.zomato.com/careers",
     "selectors": ["a[href*='/careers/']", "a[href*='/jobs/']", "[class*='job']", "[class*='opening']"], "wait_ms": 3500},
    {"name": "phonepe", "category": "startup",
     "build_url": lambda q, loc: "https://www.phonepe.com/careers/",
     "selectors": ["a[href*='/careers/']", "a[href*='/jobs/']", "[class*='opening']", "[class*='job']"], "wait_ms": 3500},
    {"name": "cred", "category": "startup",
     "build_url": lambda q, loc: "https://careers.cred.club/",
     "selectors": ["a[href*='/jobs/']", "[class*='job-listing']", "[class*='opening']"], "wait_ms": 3500},
    {"name": "meesho", "category": "startup",
     "build_url": lambda q, loc: "https://careers.meesho.com/",
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"], "wait_ms": 3500},
    {"name": "ola", "category": "startup",
     "build_url": lambda q, loc: "https://www.olacabs.com/careers",
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"], "wait_ms": 3500},
    {"name": "paytm", "category": "startup",
     "build_url": lambda q, loc: "https://paytm.com/careers",
     "selectors": ["a[href*='/careers/']", "a[href*='/jobs/']", "[class*='job']"], "wait_ms": 3500},
    {"name": "freshworks", "category": "startup",
     "build_url": lambda q, loc: "https://www.freshworks.com/company/careers/",
     "selectors": ["a[href*='/careers/']", "a[href*='/jobs/']", "[class*='job']"], "wait_ms": 3500},
    {"name": "zerodha", "category": "startup",
     "build_url": lambda q, loc: "https://zerodha.com/careers/",
     "selectors": ["a[href*='/careers/']", "[class*='job']", "[class*='opening']"], "wait_ms": 3500},
    {"name": "groww", "category": "startup",
     "build_url": lambda q, loc: "https://groww.in/careers",
     "selectors": ["a[href*='/careers/']", "a[href*='/jobs/']", "[class*='job']"], "wait_ms": 3500},
    {"name": "postman", "category": "startup",
     "build_url": lambda q, loc: "https://www.postman.com/company/careers/",
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"], "wait_ms": 3500},
    {"name": "zoho", "category": "startup",
     "build_url": lambda q, loc: "https://www.zoho.com/careers/",
     "selectors": ["a[href*='/careers/']", "a[href*='/jobs/']", "[class*='job']"], "wait_ms": 3500},
    {"name": "swiggy", "category": "startup",
     "build_url": lambda q, loc: "https://careers.swiggy.com/",
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "div.opening"], "wait_ms": 3500},
    {"name": "flipkart", "category": "startup",
     "build_url": lambda q, loc: "https://www.flipkartcareers.com/#!/joblist",
     "selectors": ["a[href*='/jobdetail']", "div.job-listing", "[class*='job']"], "wait_ms": 4000},
    {"name": "byjus", "category": "startup",
     "build_url": lambda q, loc: "https://byjus.com/careers/", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "a[href*='/careers/']", "[class*='job']", "[class*='opening']"]},
    {"name": "unacademy", "category": "startup",
     "build_url": lambda q, loc: "https://unacademy.com/jobs", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']", "[class*='Job']"]},
    {"name": "dream11", "category": "startup",
     "build_url": lambda q, loc: "https://www.dream11.com/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']", "[class*='openings']"]},
    {"name": "makemytrip", "category": "startup",
     "build_url": lambda q, loc: "https://careers.makemytrip.com/", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']", "tr[role='row']"]},
    {"name": "policybazaar", "category": "startup",
     "build_url": lambda q, loc: "https://www.policybazaar.com/careers/", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "nykaa", "category": "startup",
     "build_url": lambda q, loc: "https://www.nykaa.com/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "bharatpe", "category": "startup",
     "build_url": lambda q, loc: "https://bharatpe.com/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "pinelabs", "category": "startup",
     "build_url": lambda q, loc: "https://www.pinelabs.com/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "khatabook", "category": "startup",
     "build_url": lambda q, loc: "https://khatabook.com/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "urbancompany", "category": "startup",
     "build_url": lambda q, loc: "https://www.urbancompany.com/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "lenskart", "category": "startup",
     "build_url": lambda q, loc: "https://careers.lenskart.com/", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "boat", "category": "startup",
     "build_url": lambda q, loc: "https://www.imaginemarketingindia.com/careers/", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "blinkit", "category": "startup",
     "build_url": lambda q, loc: "https://blinkit.com/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "mpl", "category": "startup",
     "build_url": lambda q, loc: "https://www.mpl.live/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "games24x7", "category": "startup",
     "build_url": lambda q, loc: "https://www.games24x7.com/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "oyo", "category": "startup",
     "build_url": lambda q, loc: "https://www.oyorooms.com/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "rapido", "category": "startup",
     "build_url": lambda q, loc: "https://www.rapido.bike/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "acko", "category": "startup",
     "build_url": lambda q, loc: "https://www.acko.com/careers/", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "mygate", "category": "startup",
     "build_url": lambda q, loc: "https://mygate.com/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "nobroker", "category": "startup",
     "build_url": lambda q, loc: "https://www.nobroker.in/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "mamaearth", "category": "startup",
     "build_url": lambda q, loc: "https://mamaearth.in/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "sharechat", "category": "startup",
     "build_url": lambda q, loc: "https://sharechat.com/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "glance", "category": "startup",
     "build_url": lambda q, loc: "https://glance.com/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "practo", "category": "startup",
     "build_url": lambda q, loc: "https://www.practo.com/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "pharmeasy", "category": "startup",
     "build_url": lambda q, loc: "https://pharmeasy.in/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "healthifyme", "category": "startup",
     "build_url": lambda q, loc: "https://www.healthifyme.com/in/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "firstcry", "category": "startup",
     "build_url": lambda q, loc: "https://www.firstcry.com/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "bookmyshow", "category": "startup",
     "build_url": lambda q, loc: "https://in.bookmyshow.com/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "spinny", "category": "startup",
     "build_url": lambda q, loc: "https://www.spinny.com/careers/", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "cars24", "category": "startup",
     "build_url": lambda q, loc: "https://www.cars24.com/careers/", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "upgrad", "category": "startup",
     "build_url": lambda q, loc: "https://www.upgrad.com/careers/", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "vedantu", "category": "startup",
     "build_url": lambda q, loc: "https://www.vedantu.com/careers", "wait_ms": 3500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},

    # ============ MID-LEVEL / INDIAN IT SERVICES ============
    {"name": "tcs", "category": "midlevel",
     "build_url": lambda q, loc: "https://www.tcs.com/careers",
     "selectors": ["a[href*='/careers/']", "[class*='job']", "[class*='opening']"], "wait_ms": 3500},
    {"name": "infosys", "category": "midlevel",
     "build_url": lambda q, loc: "https://careers.infosys.com/joblist?lang=en_US",
     "selectors": ["a[href*='/joblist']", "[class*='job']", "tr[role='row']"], "wait_ms": 4500},
    {"name": "wipro", "category": "midlevel",
     "build_url": lambda q, loc: "https://careers.wipro.com/",
     "selectors": ["a[href*='/job/']", "[class*='job-card']", "[class*='search-result']"], "wait_ms": 4000},
    {"name": "hcltech", "category": "midlevel",
     "build_url": lambda q, loc: "https://www.hcltech.com/careers", "wait_ms": 4000,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']", "[class*='search-result']"]},
    {"name": "techmahindra", "category": "midlevel",
     "build_url": lambda q, loc: "https://careers.techmahindra.com/", "wait_ms": 4000,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']", "[class*='search-result']"]},
    {"name": "cognizant", "category": "midlevel",
     "build_url": lambda q, loc: f"https://careers.cognizant.com/global/en/search-results?keywords={q.replace(' ', '%20')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/job/']", "[class*='job-card']", "[class*='search-result']"]},
    {"name": "capgemini", "category": "midlevel",
     "build_url": lambda q, loc: f"https://www.capgemini.com/in-en/careers/job-search/?keyword={q.replace(' ', '+')}",
     "wait_ms": 4000,
     "selectors": ["a[href*='/job/']", "[class*='job-card']", "[class*='opening']"]},
    {"name": "mphasis", "category": "midlevel",
     "build_url": lambda q, loc: "https://www.mphasis.com/home/careers/job-openings.html", "wait_ms": 4000,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "ltimindtree", "category": "midlevel",
     "build_url": lambda q, loc: "https://www.ltimindtree.com/careers/", "wait_ms": 4000,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "persistent", "category": "midlevel",
     "build_url": lambda q, loc: "https://www.persistent.com/careers/", "wait_ms": 4000,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "coforge", "category": "midlevel",
     "build_url": lambda q, loc: "https://www.coforge.com/careers", "wait_ms": 4000,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "hexaware", "category": "midlevel",
     "build_url": lambda q, loc: "https://hexaware.com/careers/", "wait_ms": 4000,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "birlasoft", "category": "midlevel",
     "build_url": lambda q, loc: "https://www.birlasoft.com/careers", "wait_ms": 4000,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "cyient", "category": "midlevel",
     "build_url": lambda q, loc: "https://www.cyient.com/careers", "wait_ms": 4000,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},
    {"name": "mastek", "category": "midlevel",
     "build_url": lambda q, loc: "https://www.mastek.com/careers", "wait_ms": 4000,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='opening']"]},

    # ============ MNCs ============
    {"name": "google", "category": "mnc",
     "build_url": lambda q, loc: f"https://www.google.com/about/careers/applications/jobs/results/?q={q.replace(' ', '%20')}&location={(loc or 'India').replace(' ', '%20')}",
     "selectors": ["[class*='search-result']", "a[class*='job']", "a[href*='/jobs/results/']"], "wait_ms": 4500},
    {"name": "microsoft", "category": "mnc",
     "build_url": lambda q, loc: f"https://careers.microsoft.com/v2/global/en/search-results?keywords={q.replace(' ', '%20')}",
     "selectors": ["[class*='search-result']", "[class*='job-tile']", "a[href*='/job/']"], "wait_ms": 5000},
    {"name": "amazon", "category": "mnc",
     "build_url": lambda q, loc: f"https://www.amazon.jobs/en/search?base_query={q.replace(' ', '+')}&loc_query={(loc or 'India').replace(' ', '+')}",
     "selectors": ["div.job-tile", "[class*='job']", "a[href*='/en/jobs/']"], "wait_ms": 4500},
    {"name": "meta", "category": "mnc",
     "build_url": lambda q, loc: f"https://www.metacareers.com/jobs?q={q.replace(' ', '%20')}",
     "selectors": ["a[href*='/jobs/']", "[class*='job-list']", "[class*='job-card']"], "wait_ms": 4500},
    {"name": "adobe", "category": "mnc",
     "build_url": lambda q, loc: f"https://careers.adobe.com/us/en/search-results?keywords={q.replace(' ', '%20')}",
     "selectors": ["[class*='job-tile']", "a[href*='/job/']", "[class*='search-result']"], "wait_ms": 4500},
    {"name": "ibm", "category": "mnc",
     "build_url": lambda q, loc: f"https://www.ibm.com/careers/search?q={q.replace(' ', '%20')}",
     "selectors": ["[class*='job-card']", "a[href*='/careers/job/']", "[class*='search-result']"], "wait_ms": 4500},
    {"name": "nvidia", "category": "mnc",
     "build_url": lambda q, loc: f"https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite?q={q.replace(' ', '+')}",
     "selectors": ["[data-automation-id='jobTitle']", "a[data-automation-id='jobTitle']", "[class*='WGGE']"], "wait_ms": 5000},
    {"name": "apple", "category": "mnc",
     "build_url": lambda q, loc: f"https://jobs.apple.com/en-in/search?search={q.replace(' ', '%20')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/details/']", "[class*='table--advanced-search']", "[class*='job']"]},
    {"name": "oracle", "category": "mnc",
     "build_url": lambda q, loc: f"https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs?keyword={q.replace(' ', '+')}",
     "wait_ms": 5000,
     "selectors": ["a[href*='/job/']", "[class*='job-tile']", "[class*='posting']"]},
    {"name": "salesforce", "category": "mnc",
     "build_url": lambda q, loc: f"https://careers.salesforce.com/en/jobs/?search={q.replace(' ', '+')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='card']"]},
    {"name": "sap", "category": "mnc",
     "build_url": lambda q, loc: f"https://jobs.sap.com/search/?q={q.replace(' ', '+')}",
     "wait_ms": 4500,
     "selectors": ["a.jobTitle-link", "[class*='job']", "[class*='posting']"]},
    {"name": "cisco", "category": "mnc",
     "build_url": lambda q, loc: f"https://jobs.cisco.com/jobs/SearchJobs/?{q.replace(' ', '+')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/jobs/Pro']", "[class*='job-result']", "[class*='posting']"]},
    {"name": "intel", "category": "mnc",
     "build_url": lambda q, loc: f"https://jobs.intel.com/en/search-jobs/{q.replace(' ', '%20')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/job/']", "[class*='job']", "[class*='card']"]},
    {"name": "qualcomm", "category": "mnc",
     "build_url": lambda q, loc: f"https://careers.qualcomm.com/careers/SearchJobs/?{q.replace(' ', '+')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='posting']"]},
    {"name": "vmware", "category": "mnc",
     "build_url": lambda q, loc: "https://careers.vmware.com/main/jobs", "wait_ms": 4500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='card']"]},
    {"name": "dell", "category": "mnc",
     "build_url": lambda q, loc: f"https://jobs.dell.com/search-jobs?q={q.replace(' ', '+')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/job/']", "[class*='job']", "[class*='card']"]},
    {"name": "atlassian", "category": "mnc",
     "build_url": lambda q, loc: f"https://www.atlassian.com/company/careers/all-jobs?search={q.replace(' ', '+')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/careers/details/']", "[class*='job']", "[class*='posting']"]},
    {"name": "servicenow", "category": "mnc",
     "build_url": lambda q, loc: f"https://careers.servicenow.com/en/jobs/?search={q.replace(' ', '+')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/jobs/']", "[class*='job']", "[class*='card']"]},
    {"name": "workday", "category": "mnc",
     "build_url": lambda q, loc: f"https://workday.wd5.myworkdayjobs.com/Workday?q={q.replace(' ', '+')}",
     "wait_ms": 5000,
     "selectors": ["[data-automation-id='jobTitle']", "a[data-automation-id='jobTitle']"]},
    {"name": "snowflake", "category": "mnc",
     "build_url": lambda q, loc: f"https://careers.snowflake.com/us/en/search-results?keywords={q.replace(' ', '%20')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/job/']", "[class*='job-tile']", "[class*='card']"]},
    {"name": "databricks", "category": "mnc",
     "build_url": lambda q, loc: f"https://www.databricks.com/company/careers/open-positions?search={q.replace(' ', '+')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/careers/']", "[class*='job']", "[class*='posting']"]},
    {"name": "mongodb", "category": "mnc",
     "build_url": lambda q, loc: f"https://www.mongodb.com/careers/jobs?search={q.replace(' ', '+')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/careers/']", "[class*='job']", "[class*='posting']"]},
    {"name": "uber", "category": "mnc",
     "build_url": lambda q, loc: f"https://www.uber.com/global/en/careers/list/?query={q.replace(' ', '%20')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/careers/']", "[class*='job']", "[class*='card']"]},
    {"name": "netflix", "category": "mnc",
     "build_url": lambda q, loc: f"https://explore.jobs.netflix.net/careers?query={q.replace(' ', '%20')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/careers/']", "[class*='job']", "[class*='card']"]},
    {"name": "jpmorgan", "category": "mnc",
     "build_url": lambda q, loc: f"https://careers.jpmorgan.com/global/en/professionals/search-results?keyword={q.replace(' ', '+')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/job/']", "[class*='job']", "[class*='search-result']"]},
    {"name": "goldmansachs", "category": "mnc",
     "build_url": lambda q, loc: f"https://www.goldmansachs.com/careers/our-firm/search-jobs.html?q={q.replace(' ', '+')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/careers/']", "[class*='job']", "[class*='listing']"]},
    {"name": "morganstanley", "category": "mnc",
     "build_url": lambda q, loc: f"https://morganstanley.tal.net/vx/search/external/index?q={q.replace(' ', '+')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/job/']", "[class*='job']", "[class*='posting']"]},
    {"name": "citi", "category": "mnc",
     "build_url": lambda q, loc: f"https://jobs.citi.com/search-jobs?q={q.replace(' ', '+')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/job/']", "[class*='job']", "[class*='listing']"]},
    {"name": "deloitte", "category": "mnc",
     "build_url": lambda q, loc: f"https://jobsindia.deloitte.com/search/?q={q.replace(' ', '+')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/job/']", "[class*='job']", "[class*='search-result']"]},
    {"name": "accenture", "category": "mnc",
     "build_url": lambda q, loc: f"https://www.accenture.com/in-en/careers/jobsearch?jk={q.replace(' ', '+')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/jobdetails']", "[class*='job']", "[class*='card']"]},
    {"name": "genpact", "category": "mnc",
     "build_url": lambda q, loc: f"https://www.genpact.com/careers/search-jobs?search={q.replace(' ', '+')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/job/']", "[class*='job']", "[class*='listing']"]},
    {"name": "paypal", "category": "mnc",
     "build_url": lambda q, loc: f"https://paypal.eightfold.ai/careers?query={q.replace(' ', '%20')}",
     "wait_ms": 4500,
     "selectors": ["a[href*='/careers/']", "[class*='job']", "[class*='card']"]},
]


_VALID_CATS = ("startup", "midlevel", "mnc")
COMPANY_PRIORITY_1 = os.environ.get("COMPANY_PRIORITY_1", "").strip().lower()
COMPANY_PRIORITY_2 = os.environ.get("COMPANY_PRIORITY_2", "").strip().lower()
COMPANY_PRIORITY_3 = os.environ.get("COMPANY_PRIORITY_3", "").strip().lower()


def _ranked_priorities() -> list[str]:
    """Return the user's ranked priority list, deduped, only valid categories kept."""
    out: list[str] = []
    for p in (COMPANY_PRIORITY_1, COMPANY_PRIORITY_2, COMPANY_PRIORITY_3):
        if p in _VALID_CATS and p not in out:
            out.append(p)
    return out


def _select_sites() -> list[dict]:
    """Order matters: when ranked priorities are set, return the matching sites
    in priority order (1st-preference category first), then boards at the end.
    Falls back to the single-pick FILTER_COMPANY_SIZE if no priorities are set.
    """
    priorities = _ranked_priorities()
    if priorities:
        selected: list[dict] = []
        for cat in priorities:
            selected.extend(s for s in SITES if s["category"] == cat)
        selected.extend(s for s in SITES if s["category"] == "board")
        return selected
    if FILTER_COMPANY_SIZE in _VALID_CATS:
        return [s for s in SITES if s["category"] in ("board", FILTER_COMPANY_SIZE)]
    return SITES


def run() -> dict:
    active = get_active()
    if not active:
        print("No active résumé — upload one first.")
        return {"jobs": 0}
    # If the user typed a role/title in the upload form, that wins over the
    # résumé-derived one. Otherwise we auto-detect from the active résumé.
    if FILTER_ROLE:
        q = FILTER_ROLE
        query_source = "user-typed"
    else:
        qs = derive_queries(active)
        q = qs[0] if qs else "data scientist"
        query_source = "auto-detected from résumé"
    selected = _select_sites()
    priorities = _ranked_priorities()
    print(f"Query:    {q!r}  ({query_source})")
    print(f"Location: {FILTER_LOCATION or 'India (default)'}")
    if priorities:
        print(f"Priority: " + " > ".join(priorities) + "  (then boards)")
    else:
        print(f"Company:  {FILTER_COMPANY_SIZE or 'any'}")
    print(f"Cap:      {SCRAPE_LIMIT or 'no cap (full sweep)'}")
    print(f"Sites:    {len(selected)} of {len(SITES)}\n")
    skills = active.get("skills") or []

    report: list[dict] = []
    aggregated: list[dict] = []

    for i, site in enumerate(selected, start=1):
        url = _inject_advanced_filters(site["name"], site["build_url"](q, FILTER_LOCATION))
        print("=" * 60)
        print(f"  [{i}/{len(selected)}]  {site['name'].upper()}  ({site['category']})")
        print(f"  URL: {url}")
        print(f"  >>> opening a new Chromium WINDOW now...")
        print("=" * 60)
        # Publish what we're doing right NOW so the live panel updates.
        _write_state({
            "current": i, "total": len(selected),
            "site_name": site["name"], "category": site["category"],
            "url": url, "screenshot": None,
            "running": True, "jobs_running_total": len({r["url"] for r in aggregated}),
        })

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS, args=_LAUNCH_ARGS)
            ctx = browser.new_context(user_agent=UA, no_viewport=True)
            page = ctx.new_page()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(site["wait_ms"])
            except Exception as e:
                msg = str(e).splitlines()[0][:80]
                print(f"  nav error: {msg}")
                report.append({"site": site["name"], "result": f"nav error: {msg}"})
                try: browser.close()
                except Exception: pass
                continue

            shot = SHOTS / f"multi_{i}_{site['name']}.png"
            try: page.screenshot(path=str(shot))
            except Exception: pass

            block = _blocked(page)
            if block:
                print(f"  BLOCKED: {block}")
                report.append({"site": site["name"], "result": f"BLOCKED ({block})", "shot": str(shot)})
                try: page.wait_for_timeout(2000)
                except Exception: pass
                try: browser.close()
                except Exception: pass
                continue

            try:
                jobs = page.evaluate(_EXTRACT_JS, site["selectors"]) or []
            except Exception as e:
                jobs = []
                print(f"  extract error: {str(e)[:80]}")

            print(f"  extracted: {len(jobs)} job cards")
            report.append({"site": site["name"], "result": f"{len(jobs)} jobs", "shot": str(shot)})
            # Publish the screenshot we just took so the live panel can show it.
            _write_state({
                "current": i, "total": len(selected),
                "site_name": site["name"], "category": site["category"],
                "url": url, "screenshot": f"multi_{i}_{site['name']}.png",
                "jobs_this_site": len(jobs),
                "running": True,
                "jobs_running_total": len({r["url"] for r in aggregated}) + len([j for j in jobs if j.get("url")]),
            })
            site_rows: list[dict] = []
            for j in jobs:
                if j.get("url"):
                    row = {
                        "source": site["name"],
                        "url": j["url"],
                        "title": j.get("title") or "Untitled",
                        "company": j.get("company") or None,
                        "location": j.get("location") or None,
                        "description": "",
                        "status": "discovered",
                        "raw": {"query": q, "site": site["name"], "category": site["category"],
                                "location_filter": FILTER_LOCATION,
                                "company_size_filter": FILTER_COMPANY_SIZE},
                    }
                    aggregated.append(row)
                    site_rows.append(row)
            try: page.wait_for_timeout(3000)
            except Exception: pass
            try: browser.close()
            except Exception: pass

        # ── Score & upsert EACH JOB individually so the home page polling
        # sees them appear in real time (one at a time, not as a batch).
        if site_rows:
            sb_jobs = get_supabase().table("jobs")
            saved_count = 0
            for r in site_rows:
                s, reason = keyword_score(skills, r)
                r["score"] = s
                r["score_reason"] = reason
                r["status"] = "scored"
                try:
                    sb_jobs.upsert([r], on_conflict="url").execute()
                    saved_count += 1
                except Exception as e:
                    print(f"  ↳ DB upsert error: {str(e)[:80]}")
            if saved_count:
                print(f"  ↳ saved {saved_count} rows one-by-one (running total: {len(aggregated)})")
        time.sleep(0.7)

        # Early-stop: once we've collected at least SCRAPE_LIMIT unique URLs, end the walk.
        # Dedupe is the same as the final pass below so the count is honest.
        if SCRAPE_LIMIT and len({r["url"] for r in aggregated}) >= SCRAPE_LIMIT:
            print(f"\n  >>> Cap of {SCRAPE_LIMIT} jobs reached after site {i}. Stopping the walk.\n")
            break

    # Final dedupe across the entire walk — each row was already scored & upserted
    # per-site above; this block is now just for an honest end-of-run report.
    by_url = {r["url"]: r for r in aggregated}
    rows = list(by_url.values())

    print("\n" + "=" * 60)
    print("  PER-SITE REPORT")
    print("=" * 60)
    for r in report:
        print(f"  {r['site']:14}  ->  {r['result']}")
    print(f"\n  Total unique jobs saved to DB: {len(rows)}")
    _clear_state()  # signal to /api/scrape/state that the walk is done
    return {"jobs": len(rows), "report": report}


if __name__ == "__main__":
    print(run())
