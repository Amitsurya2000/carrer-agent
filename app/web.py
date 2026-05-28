"""Web UI served by the FastAPI app.

Rules (per user):
- Jobs shown are STRICTLY the active résumé's search results — changing résumé clears
  and re-searches, so the list always reflects the chosen résumé.
- No active résumé  ->  empty job lists.
- Jobs are split into Active (live postings) and Past/Expired (older postings).
"""
from __future__ import annotations

import html
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import get_supabase
from app.discover import discover as run_discovery
from app.profile_store import get_active, list_profiles, set_active, upload_profile
from app.resume import derive_queries, extract_text
from app.score import keyword_score, score_pending_keyword

_PROJ = Path(__file__).resolve().parent.parent

# Vercel sets VERCEL=1 in its function environment. On Vercel there's no display
# server, no subprocess access, and a 60-second function timeout — so the visible
# Playwright walks are silently disabled and the UI falls back to API-only discovery.
IS_VERCEL = bool(os.environ.get("VERCEL"))


def _visible_subprocess(cmd: list[str], env: dict | None = None, timeout: int = 300) -> None:
    """Run a subprocess so its console window AND any GUI (Playwright Chromium) actually
    appear on screen. The server is launched HIDDEN by start_hidden.vbs, and that hidden
    flag propagates to children unless we explicitly override STARTUPINFO + creationflags.
    """
    kwargs: dict = {"cwd": str(_PROJ), "timeout": timeout, "check": False}
    if env is not None:
        kwargs["env"] = env
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 1  # SW_SHOWNORMAL — force the new console to be VISIBLE
        kwargs["startupinfo"] = si
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    try:
        subprocess.run(cmd, **kwargs)
    except Exception:
        pass

router = APIRouter()

_ALLOWED = (".pdf", ".docx", ".doc", ".txt", ".md", ".rtf")
_EXPIRE_DAYS = 30

_CSS = """
<style>
 body{font-family:system-ui,Segoe UI,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#222}
 h1{margin-bottom:.2rem}h3{margin:.2rem 0}.muted{color:#777;font-size:.9rem}
 .card{border:1px solid #ddd;border-radius:8px;padding:1rem 1.2rem;margin:1rem 0}
 table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;font-size:.92rem}
 .score{font-weight:700}.active{color:#0a7d28;font-weight:700}
 button{padding:5px 10px;border-radius:6px;border:1px solid #888;background:#f6f6f6;cursor:pointer}
 .row{display:flex;gap:1rem;align-items:center;flex-wrap:wrap}
</style>
"""


def _pw_buttons_html() -> str:
    """Visible-Playwright buttons. Hidden on Vercel (no display, no subprocess)."""
    if IS_VERCEL:
        return ("<br><span class='muted'>Hosted on Vercel — visible-Chromium scraping "
                "is desktop-only. Run the local launcher to use the 108-site visible walk.</span>")
    return (
        '&nbsp; <form method="post" action="/ui/enrich" style="display:inline">'
        '<button title="Open each top job\'s page in a headless browser and grab the full description">'
        'Get full descriptions (Playwright)</button></form>'
        '&nbsp; <form method="post" action="/ui/hn" style="display:inline">'
        '<button title="Open a visible Chromium, navigate to HN \'Who is hiring\', scrape every job post, score them">'
        'Scrape HN Who is Hiring (Playwright, visible)</button></form>'
        '&nbsp; <form method="post" action="/ui/naukri" style="display:inline">'
        '<button title="Open a visible Chromium, scrape Naukri\'s public listings for your résumé\'s keywords, score them">'
        'Scrape Naukri (Playwright, visible)</button></form>'
        '&nbsp; <form method="post" action="/ui/multi" style="display:inline">'
        '<button title="Walk one Chromium through 108 platforms — visibly. Honest per-site report.">'
        'Scrape ALL sites (visible)</button></form>'
    )


def _clear_jobs() -> None:
    """Drop all non-applied jobs, so the list only ever holds the active résumé's search."""
    get_supabase().table("jobs").delete().neq("status", "applied").execute()


def _refresh_for_active() -> None:
    """Clear old jobs, then search + score for the currently active résumé."""
    active = get_active()
    if not active:
        _clear_jobs()
        return
    _clear_jobs()
    run_discovery(queries=derive_queries(active))
    score_pending_keyword()


def _is_expired(job: dict) -> bool:
    created = (job.get("raw") or {}).get("created")
    if not created:
        return False
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except Exception:
        return False
    # HN's posted_iso is naive; Adzuna's `created` carries +00:00. Normalize to aware-UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < datetime.now(timezone.utc) - timedelta(days=_EXPIRE_DAYS)


def _job_rows(jobs: list[dict]) -> str:
    rows = ""
    for i, j in enumerate(jobs, start=1):
        sc = j.get("score")
        url = j.get("url") or ""
        title = str(j.get("title") or "")
        desc_short = (j.get("description") or "")[:240]
        raw = j.get("raw") or {}
        posted = (raw.get("created") or "")[:10] if raw.get("created") else (raw.get("posted_text") or "-")
        title_html = (
            f"<a href='{html.escape(url)}' target='_blank' rel='noopener' "
            f"title='{html.escape(desc_short)}'>{html.escape(title)}</a>"
            if url else html.escape(title)
        )
        apply_html = (
            f"<a href='{html.escape(url)}' target='_blank' rel='noopener'>Apply &rarr;</a>"
            if url else ""
        )
        rows += (f"<tr><td>{i}</td>"
                 f"<td class='score'>{sc if sc is not None else '-'}</td>"
                 f"<td>{title_html}</td>"
                 f"<td>{html.escape(str(j.get('company') or ''))}</td>"
                 f"<td>{html.escape(str(j.get('location') or ''))}</td>"
                 f"<td>{html.escape(str(posted))}</td>"
                 f"<td class='muted'>{html.escape(str(j.get('score_reason') or ''))}</td>"
                 f"<td>{apply_html}</td></tr>")
    return rows or "<tr><td colspan='8' class='muted'>Empty — upload a résumé and the search will fill this in.</td></tr>"


def _page() -> str:
    profiles = list_profiles()
    active = get_active()
    querystr = ", ".join(derive_queries(active)) if active else ""

    prof_rows = ""
    for p in profiles:
        tag = ("<span class='active'>active</span>" if p.get("is_active") else
               f"<form method='post' action='/ui/use' style='display:inline'>"
               f"<input type='hidden' name='profile_id' value='{p['id']}'><button>Use</button></form>")
        rm = (f"<form method='post' action='/ui/delete' style='display:inline'>"
              f"<input type='hidden' name='profile_id' value='{p['id']}'><button>Remove</button></form>")
        prof_rows += (f"<tr><td>{html.escape(str(p.get('label') or 'resume'))}</td>"
                      f"<td>{len(p.get('skills') or [])} kw</td><td>{tag}</td><td>{rm}</td></tr>")
    if not prof_rows:
        prof_rows = "<tr><td colspan='4' class='muted'>No resume uploaded yet.</td></tr>"

    empty = "<tr><td colspan='8' class='muted'>No active resume - upload one above to begin.</td></tr>"
    active_html = past_html = empty
    if active:
        jobs = (get_supabase().table("jobs")
                .select("title,company,location,score,score_reason,raw,url,description")
                .gt("score", 15)  # only résumé-matching jobs (drops unscored + no-overlap)
                .order("score", desc=True).limit(200).execute().data)
        active_html = _job_rows([j for j in jobs if not _is_expired(j)][:30])
        past_html = _job_rows([j for j in jobs if _is_expired(j)][:20])

    searching = html.escape(querystr) if querystr else "(no active resume)"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>AI Career Agent</title>{_CSS}</head><body>
<h1>AI Career Agent</h1>
<p class="muted">Jobs are matched strictly to your active resume. Free keyword scoring, no API.</p>
{('<p class="muted" style="background:#fff8d6;border:1px solid #e4cc5e;padding:8px 12px;border-radius:6px"><b>Hosted on Vercel.</b> Uploads search Adzuna + Remotive + RemoteOK APIs (~2 s). For the 108-site visible Chromium walk, run the project locally — clone the repo and double-click <code>start.bat</code>.</p>' if IS_VERCEL else '')}

<div class="card">
  <h3>1. Your resumes</h3>
  <form method="post" action="/ui/upload" enctype="multipart/form-data">
    <input type="file" name="file" accept=".pdf,.docx,.doc,.txt,.md,.rtf" required>
    &nbsp;
    <input name="location" placeholder="Location (e.g. Bengaluru / Remote / India)" style="width:24%;padding:5px">
    &nbsp;
    <select name="experience" style="padding:5px">
      <option value="">Any experience</option>
      <option value="fresher">Fresher / Entry-level</option>
      <option value="mid">Mid (2-5 yrs)</option>
      <option value="senior">Senior (5+ yrs)</option>
    </select>
    &nbsp;
    <select name="company_size" style="padding:5px" title="Restricts which career pages are scraped. Job boards (Naukri, Indeed, etc.) run for every choice.">
      <option value="">Any company size</option>
      <option value="startup">Startup / Unicorn</option>
      <option value="midlevel">Mid-level / IT services</option>
      <option value="mnc">MNC</option>
    </select>
    &nbsp;
    <button>Upload resume + Search jobs (visible)</button>
  </form>
  <p class="muted">Uploading opens visible Chromium windows that walk <b>up to ~108 platforms</b>: 15 job boards (Naukri, LinkedIn, Indeed, Foundit, Shine, Glassdoor, Apna, Internshala, JobsForHer, WorkIndia, Hirist, Cutshort, Instahyre, TimesJobs, Wellfound) + 46 Indian startups/unicorns (Razorpay, Zomato, PhonePe, Cred, Meesho, BYJU's, Unacademy, Dream11, MakeMyTrip, Nykaa, BharatPe, Urban Company, Lenskart, OYO, Acko, Practo, PharmEasy, etc.) + 15 IT services (TCS, Infosys, Wipro, HCL, Tech Mahindra, Cognizant, Capgemini, LTIMindtree, etc.) + 32 MNCs (Google, Microsoft, Amazon, Meta, Apple, Oracle, Salesforce, SAP, Cisco, Intel, Adobe, IBM, Nvidia, Atlassian, ServiceNow, Snowflake, Databricks, Uber, Netflix, JPMorgan, Goldman, Citi, Deloitte, Accenture, etc.). <b>Location</b> is injected into the URL of every site that supports it. <b>Company size</b> picks which career pages are scraped — Startup (~61 sites), Mid-level (~30 sites), MNC (~47 sites), or blank for all ~108. Boards always run.</p>
  <table><thead><tr><th>Resume</th><th>Keywords</th><th>Active</th><th></th></tr></thead>
  <tbody>{prof_rows}</tbody></table>
  <p class="muted">Searching jobs for: <b>{searching}</b>
     &nbsp; <form method="post" action="/ui/discover" style="display:inline"><button>Refresh matches</button></form>
     {_pw_buttons_html()}</p>
</div>

<div class="card">
  <h3>2. Active jobs (live postings)</h3>
  <table><thead><tr><th>#</th><th>Score</th><th>Title</th><th>Company</th><th>Location</th><th>Posted</th><th>Why</th><th></th></tr></thead>
  <tbody>{active_html}</tbody></table>
</div>

<div class="card">
  <h3>3. Add a job from any site (Wellfound, LinkedIn, Naukri, etc.)</h3>
  <p class="muted">Browse those sites in your <b>normal</b> browser, copy a job's URL, paste it here. Strongly recommended: also paste the description text — that's what scoring matches against.</p>
  <form method="post" action="/ui/add_job">
    <input name="url" placeholder="https://wellfound.com/jobs/... or any job URL" required style="width:55%;padding:5px">
    &nbsp;<input name="title" placeholder="Job title (optional)" style="width:35%;padding:5px">
    <br><br>
    <textarea name="description" placeholder="Paste the full job description here (optional but recommended)" rows="4" style="width:97%;padding:5px"></textarea>
    <br><br>
    <button>Add and score</button>
  </form>
</div>

<div class="card">
  <h3>4. Past / expired jobs (posted &gt; {_EXPIRE_DAYS} days ago)</h3>
  <table><thead><tr><th>#</th><th>Score</th><th>Title</th><th>Company</th><th>Location</th><th>Posted</th><th>Why</th><th></th></tr></thead>
  <tbody>{past_html}</tbody></table>
</div>
</body></html>"""


@router.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(_page(), headers={"Cache-Control": "no-store"})


@router.post("/ui/upload")
async def ui_upload(
    file: UploadFile = File(...),
    location: str = Form(""),
    experience: str = Form(""),
    company_size: str = Form(""),
):
    name = (file.filename or "").lower()
    if not name.endswith(_ALLOWED):
        return RedirectResponse("/", status_code=303)  # ignore non-résumé files
    text = extract_text(file.filename, await file.read())
    sb = get_supabase()
    sb.table("profile").delete().eq("label", file.filename).execute()  # replace same-name
    upload_profile(file.filename, text)
    # Clean slate: Active jobs starts EMPTY and only fills with the new search's results.
    _clear_jobs()
    if IS_VERCEL:
        # On Vercel: no display, no subprocess, 60-second timeout. Use API-source
        # discovery (Adzuna + Remotive + RemoteOK) which finishes in ~1-3 seconds.
        try:
            active = get_active()
            run_discovery(queries=derive_queries(active) if active else None)
            score_pending_keyword()
        except Exception:
            pass
    else:
        env = {**os.environ,
               "FILTER_LOCATION": (location or "").strip(),
               "FILTER_EXPERIENCE": (experience or "").strip(),
               "FILTER_COMPANY_SIZE": (company_size or "").strip().lower()}
        # VISIBLE Playwright walk across MULTIPLE platforms — one Chromium window per site
        # pops up in sequence (Naukri, LinkedIn, Wellfound, Indeed, Foundit, Razorpay).
        _visible_subprocess(
            [sys.executable, "-m", "app.scrape.multi_site"],
            env=env, timeout=420,
        )
        _visible_subprocess(
            [sys.executable, "-m", "app.scrape.hn_hiring"],
            env=env, timeout=300,
        )
        try:
            score_pending_keyword()
        except Exception:
            pass
    return RedirectResponse("/", status_code=303)


@router.post("/ui/use")
def ui_use(profile_id: str = Form(...)):
    set_active(profile_id)
    _refresh_for_active()
    return RedirectResponse("/", status_code=303)


@router.post("/ui/delete")
def ui_delete(profile_id: str = Form(...)):
    get_supabase().table("profile").delete().eq("id", profile_id).execute()
    _refresh_for_active()  # if that removed the active one, get_active() is now None -> clears jobs
    return RedirectResponse("/", status_code=303)


@router.post("/ui/discover")
def ui_discover():
    _refresh_for_active()
    return RedirectResponse("/", status_code=303)


@router.post("/ui/enrich")
def ui_enrich():
    """Visible Playwright enrichment — opens a Chromium that visits each top job's page."""
    _visible_subprocess([sys.executable, "-m", "app.enrich"], timeout=300)
    return RedirectResponse("/", status_code=303)


@router.post("/ui/hn")
def ui_hn():
    """Visible Playwright scrape of HN 'Who is Hiring'."""
    _visible_subprocess([sys.executable, "-m", "app.scrape.hn_hiring"], timeout=300)
    return RedirectResponse("/", status_code=303)


@router.post("/ui/naukri")
def ui_naukri():
    """Visible Playwright scrape of Naukri's public job listings."""
    _visible_subprocess([sys.executable, "-m", "app.scrape.naukri"], timeout=300)
    return RedirectResponse("/", status_code=303)


@router.post("/ui/multi")
def ui_multi():
    """Visible multi-site Playwright walk: Naukri / LinkedIn / Wellfound / Indeed / Foundit / Razorpay."""
    _visible_subprocess([sys.executable, "-m", "app.scrape.multi_site"], timeout=420)
    return RedirectResponse("/", status_code=303)


@router.post("/ui/add_job")
def ui_add_job(
    url: str = Form(...),
    title: str = Form(""),
    description: str = Form(""),
):
    """Manually add a job by URL (+ optional title / pasted description).

    Use this for sites that block bots (Wellfound, LinkedIn, Naukri, etc.) — you browse
    them in your own browser, copy the link + JD text, and the app scores it like any
    other job. No scraping needed.
    """
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return RedirectResponse("/", status_code=303)
    row = {
        "source": "manual",
        "url": url,
        "title": (title.strip() or url[:80]),
        "description": description.strip()[:8000] or None,
        "status": "discovered",
    }
    sb = get_supabase()
    sb.table("jobs").upsert([row], on_conflict="url").execute()
    active = get_active()
    if active:
        skills = active.get("skills") or []
        rows = sb.table("jobs").select("*").eq("url", url).limit(1).execute().data
        if rows:
            s, reason = keyword_score(skills, rows[0])
            sb.table("jobs").update(
                {"score": s, "score_reason": reason, "status": "scored"}
            ).eq("id", rows[0]["id"]).execute()
    return RedirectResponse("/", status_code=303)
