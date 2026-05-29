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
from app.score import score_pending_keyword

_PROJ = Path(__file__).resolve().parent.parent

# Vercel sets VERCEL=1 in its function environment. On Vercel there's no display
# server, no subprocess access, and a 60-second function timeout — so the visible
# Playwright walks are silently disabled and the UI falls back to API-only discovery.
IS_VERCEL = bool(os.environ.get("VERCEL"))
# IS_CLOUD = running in a headless cloud container (Render, Railway, Fly via our
# Dockerfile which sets HEADLESS_BROWSER=1). The scrape works exactly like local
# but launches DETACHED so the HTTP request returns immediately — Render's load
# balancer drops connections after ~100s and the user's browser would time out
# waiting for the 5-16 min scrape otherwise.
IS_CLOUD = os.environ.get("HEADLESS_BROWSER", "").strip() == "1"


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

# In-memory snapshot of the most recent filter selections — used by _page() to
# render the active-filter pills and to apply post-scrape display filters
# (min salary, required skills, sort). Lives only in this process; resets on
# server restart. Good enough for a single-user local app.
_LAST_FILTERS: dict[str, str] = {}

_HEAD = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E🎯%3C/text%3E%3C/svg%3E">
<style>
 :root{
   --bg:#f5f6fa;--surface:#fff;--surface-alt:#fafbfc;
   --text:#1a1d29;--text-muted:#6b7280;
   --primary:#4f46e5;--primary-hover:#4338ca;--primary-light:#eef2ff;
   --accent:#10b981;--warn:#f59e0b;--danger:#ef4444;--info:#06b6d4;
   --border:#e5e7eb;
   --shadow-sm:0 1px 2px rgba(15,23,42,.04);
   --shadow:0 4px 16px rgba(15,23,42,.06);
   --shadow-lg:0 12px 28px rgba(15,23,42,.10);
   --radius:14px;--radius-sm:8px;
 }
 [data-theme='dark']{
   --bg:#0b0d14;--surface:#161922;--surface-alt:#1c2030;
   --text:#e5e7eb;--text-muted:#9ca3af;
   --primary:#818cf8;--primary-hover:#a5b4fc;--primary-light:#1e1b4b;
   --border:#2d3142;
   --shadow-sm:0 1px 2px rgba(0,0,0,.4);
   --shadow:0 4px 16px rgba(0,0,0,.5);
   --shadow-lg:0 12px 28px rgba(0,0,0,.6);
 }
 *{box-sizing:border-box}
 html{
   position:relative;overflow-x:hidden;
   background:var(--bg);            /* base color lives on <html> ... */
   min-height:100%;
   transition:background .2s;
 }
 body{
   position:relative;overflow-x:hidden;
   font-family:'Inter',system-ui,-apple-system,Segoe UI,sans-serif;
   background:transparent;          /* ... so the z-index:-1 decorations show through <body> */
   color:var(--text);
   max-width:1180px;margin:0 auto;padding:1.5rem;line-height:1.55;
   -webkit-font-smoothing:antialiased;transition:color .2s;
 }

 /* ----- Floating gradient blobs in the background (Stripe / Linear style) ----- */
 .bg-blob{
   position:fixed;border-radius:50%;filter:blur(110px);
   opacity:.55;z-index:-1;pointer-events:none;
   will-change:transform;
 }
 [data-theme='dark'] .bg-blob{ opacity:.35; }
 .bg-blob-1{
   width:560px;height:560px;top:-180px;left:-180px;
   background:radial-gradient(circle, #4f46e5 0%, transparent 70%);
   animation:floatA 22s ease-in-out infinite;
 }
 .bg-blob-2{
   width:480px;height:480px;bottom:-140px;right:-120px;
   background:radial-gradient(circle, #ec4899 0%, transparent 70%);
   animation:floatB 26s ease-in-out infinite;
 }
 .bg-blob-3{
   width:380px;height:380px;top:38%;right:-100px;
   background:radial-gradient(circle, #06b6d4 0%, transparent 70%);
   animation:floatC 30s ease-in-out infinite;
 }
 @keyframes floatA{
   0%,100% { transform:translate(0,0) scale(1); }
   50%     { transform:translate(120px,180px) scale(1.15); }
 }
 @keyframes floatB{
   0%,100% { transform:translate(0,0) scale(1); }
   50%     { transform:translate(-140px,-90px) scale(.85); }
 }
 @keyframes floatC{
   0%,100% { transform:translate(0,0) scale(1); }
   33%     { transform:translate(-80px,150px) scale(1.1); }
   66%     { transform:translate(60px,-120px) scale(.9); }
 }

 /* ----- Dotted grid texture overlay — gives the whole page a sense of depth ----- */
 /* On <html> (not body::before) so it shows through the transparent body */
 html::before{
   content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
   background-image:radial-gradient(circle, var(--text-muted) 1.3px, transparent 1.3px);
   background-size:32px 32px;
   opacity:.22;
 }
 [data-theme='dark'] html::before{ opacity:.14; }

 /* ----- Floating career-themed decorations — slow drift, low opacity ----- */
 .floater{
   position:fixed;font-size:2rem;opacity:.32;
   pointer-events:none;z-index:-1;will-change:transform;
   filter:saturate(1.3);
 }
 [data-theme='dark'] .floater{ opacity:.22; }
 .floater-1{ top:11%;  left:6%;   animation:driftA 22s ease-in-out infinite; }
 .floater-2{ top:26%;  right:9%;  animation:driftB 26s ease-in-out infinite; animation-delay:-3s; }
 .floater-3{ top:45%;  left:4%;   animation:driftA 24s ease-in-out infinite; animation-delay:-8s; }
 .floater-4{ top:62%;  right:6%;  animation:driftB 28s ease-in-out infinite; animation-delay:-12s; }
 .floater-5{ top:80%;  left:14%;  animation:driftA 30s ease-in-out infinite; animation-delay:-5s; }
 .floater-6{ top:18%;  left:48%;  animation:driftB 25s ease-in-out infinite; animation-delay:-10s; }
 .floater-7{ top:55%;  right:28%; animation:driftA 27s ease-in-out infinite; animation-delay:-15s; }
 .floater-8{ top:38%;  left:23%;  animation:driftB 23s ease-in-out infinite; animation-delay:-7s; }
 .floater-9{ top:73%;  left:50%;  animation:driftA 29s ease-in-out infinite; animation-delay:-18s; }
 .floater-10{top:8%;   right:26%; animation:driftB 31s ease-in-out infinite; animation-delay:-22s; }
 .floater-11{top:33%;  right:42%; animation:driftA 28s ease-in-out infinite; animation-delay:-2s; }
 .floater-12{top:88%;  right:18%; animation:driftB 26s ease-in-out infinite; animation-delay:-14s; }

 @keyframes driftA{
   0%,100% { transform:translate(0,0) rotate(0deg) scale(1); }
   25%     { transform:translate(36px,-26px) rotate(10deg) scale(1.1); }
   50%     { transform:translate(-22px,46px) rotate(-6deg) scale(.95); }
   75%     { transform:translate(-36px,18px) rotate(4deg) scale(1.05); }
 }
 @keyframes driftB{
   0%,100% { transform:translate(0,0) rotate(0deg) scale(1); }
   25%     { transform:translate(-28px,-38px) rotate(-10deg) scale(.95); }
   50%     { transform:translate(46px,-18px) rotate(12deg) scale(1.1); }
   75%     { transform:translate(28px,38px) rotate(-5deg) scale(1.05); }
 }

 /* Outlined geometric shapes — at viewport edges where they're not hidden by cards */
 .deco{ position:fixed; pointer-events:none; z-index:-1; opacity:.55; }
 [data-theme='dark'] .deco{ opacity:.4; }
 .deco-circle{
   width:110px; height:110px; border-radius:50%;
   border:3px solid var(--primary);
   top:8%; right:3%;                /* moved to top-right corner — visible above cards */
   animation:driftA 35s ease-in-out infinite;
 }
 .deco-square{
   width:80px; height:80px; border-radius:14px;
   border:3px solid #ec4899; transform:rotate(45deg);
   bottom:8%; left:2%;              /* moved to bottom-left corner */
   animation:driftB 40s ease-in-out infinite; animation-delay:-12s;
 }
 .deco-ring{
   width:140px; height:140px; border-radius:50%;
   border:3px dashed #06b6d4;
   top:48%; right:-30px;            /* peeking off the right edge of the viewport */
   animation:driftA 45s linear infinite;
 }
 .deco-triangle{                    /* new — bottom-right outlined triangle */
   width:0;height:0;
   border-left:38px solid transparent;border-right:38px solid transparent;
   border-bottom:60px solid transparent;
   filter:drop-shadow(0 0 0 #10b981);  /* gives the empty triangle an outline tint */
   bottom:30%; right:-10px;
   border-bottom-color:#10b981;
   opacity:.6;
   animation:driftB 50s ease-in-out infinite; animation-delay:-7s;
 }

 /* Glassmorphism — cards float over the animated background */
 .card{
   backdrop-filter:blur(10px) saturate(1.2);
   -webkit-backdrop-filter:blur(10px) saturate(1.2);
 }
 [data-theme='light'] .card{ background:rgba(255,255,255,.85); }
 [data-theme='dark']  .card{ background:rgba(22,25,34,.78); }
 h1,h3{font-weight:800;letter-spacing:-.02em}
 h1{margin:0;font-size:2.2rem;line-height:1.1}
 h3{margin:0 0 1.1rem;font-size:1.15rem;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:.6rem}
 .gradient-text{
   background:linear-gradient(135deg,#fff 0%,#fce7f3 50%,#fff 100%);
   -webkit-background-clip:text;background-clip:text;color:transparent;
   -webkit-text-fill-color:transparent;
 }
 .gradient-text-color{
   background:linear-gradient(135deg,#4f46e5 0%,#7c3aed 50%,#ec4899 100%);
   -webkit-background-clip:text;background-clip:text;color:transparent;
   -webkit-text-fill-color:transparent;
 }
 .muted{color:var(--text-muted);font-size:.88rem}

 .hero{
   background:linear-gradient(135deg,#4f46e5 0%,#7c3aed 50%,#ec4899 100%);
   color:#fff;border-radius:var(--radius);
   padding:1.6rem 2rem;margin-bottom:1.4rem;box-shadow:var(--shadow-lg);
   display:flex;justify-content:space-between;align-items:center;
   flex-wrap:wrap;gap:1rem;
 }
 .hero-title{display:flex;align-items:center;gap:.7rem}
 .hero h1{color:#fff}
 .hero-subtitle{margin:.3rem 0 0;opacity:.92;font-size:.95rem;font-weight:400}
 .theme-toggle{
   background:rgba(255,255,255,.18);color:#fff;border:1px solid rgba(255,255,255,.25);
   padding:9px 14px;border-radius:999px;cursor:pointer;font-size:1.1rem;
   backdrop-filter:blur(8px);transition:all .15s;
 }
 .theme-toggle:hover{background:rgba(255,255,255,.3);transform:translateY(-1px)}

 .stats{display:grid;grid-template-columns:repeat(5,1fr);gap:.9rem;margin-bottom:1.4rem}
 .stat{
   background:var(--surface);border-radius:var(--radius-sm);
   padding:1.1rem 1.2rem;box-shadow:var(--shadow-sm);
   border:1px solid var(--border);border-left:4px solid var(--border);
   transition:transform .25s cubic-bezier(.4,0,.2,1), box-shadow .25s, border-color .15s;
   position:relative;overflow:hidden;
 }
 .stat::after{
   content:'';position:absolute;inset:0;
   background:linear-gradient(135deg, transparent 0%, rgba(255,255,255,.04) 50%, transparent 100%);
   opacity:0;transition:opacity .3s;pointer-events:none;
 }
 .stat:hover{transform:translateY(-4px) scale(1.02);box-shadow:var(--shadow-lg)}
 .stat:hover::after{opacity:1}
 .stat.primary{border-left-color:var(--primary)}
 .stat.success{border-left-color:var(--accent)}
 .stat.warn   {border-left-color:var(--warn)}
 .stat.info   {border-left-color:var(--info)}
 .stat.danger {border-left-color:var(--danger)}
 .stat-value{
   font-size:2.4rem;font-weight:800;line-height:1;letter-spacing:-.03em;
   background:linear-gradient(135deg,var(--primary) 0%,#7c3aed 50%,#ec4899 100%);
   -webkit-background-clip:text;background-clip:text;
   -webkit-text-fill-color:transparent;color:transparent;
 }
 .stat.success .stat-value{background:linear-gradient(135deg,#10b981 0%,#34d399 100%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:transparent}
 .stat.warn .stat-value   {background:linear-gradient(135deg,#f59e0b 0%,#fbbf24 100%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:transparent}
 .stat.info .stat-value   {background:linear-gradient(135deg,#06b6d4 0%,#22d3ee 100%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:transparent}
 .stat-label{font-size:.74rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.7px;font-weight:700;margin-top:.3rem}

 .banner{
   background:linear-gradient(90deg,var(--primary-light),transparent);
   border-left:4px solid var(--primary);
   color:var(--text);padding:12px 16px;border-radius:var(--radius-sm);
   margin-bottom:1rem;display:flex;align-items:center;gap:10px;
   font-size:.92rem;font-weight:500;
 }
 .banner.warn{background:linear-gradient(90deg,#fef3c7,transparent);border-left-color:var(--warn);color:#92400e}
 [data-theme='dark'] .banner.warn{color:#fbbf24}
 .banner.info{background:linear-gradient(90deg,#cffafe,transparent);border-left-color:var(--info);color:#155e75}
 [data-theme='dark'] .banner.info{color:#67e8f9}
 .banner.success{background:linear-gradient(90deg,#d1fae5,transparent);border-left-color:var(--accent);color:#065f46}
 [data-theme='dark'] .banner.success{color:#6ee7b7}

 .card{
   background:var(--surface);border-radius:var(--radius);
   padding:1.5rem 1.7rem;margin-bottom:1.4rem;
   box-shadow:var(--shadow);border:1px solid var(--border);
   transition:transform .35s cubic-bezier(.4,0,.2,1), box-shadow .35s;
 }
 .card:hover{transform:translateY(-3px);box-shadow:var(--shadow-lg)}

 /* Platform marquee — scrolling row of all 108 platforms, just below the hero */
 .marquee{
   margin:0 0 1.4rem;border-radius:999px;
   background:rgba(255,255,255,.6);backdrop-filter:blur(8px);
   border:1px solid var(--border);padding:.55rem 0;
   overflow:hidden;mask-image:linear-gradient(90deg,transparent,#000 8%,#000 92%,transparent);
 }
 [data-theme='dark'] .marquee{background:rgba(22,25,34,.6)}
 .marquee-track{
   display:inline-flex;gap:1.6rem;white-space:nowrap;
   animation:marquee 55s linear infinite;
   font-size:.83rem;font-weight:600;color:var(--text-muted);
 }
 .marquee-track span{display:inline-flex;align-items:center;gap:.4rem}
 @keyframes marquee{
   from{transform:translateX(0)}
   to  {transform:translateX(-50%)}
 }

 /* Score badge glow when strong match (≥ 70) */
 .score-glow{
   animation:scoreGlow 2.4s ease-in-out infinite;
   box-shadow:0 0 0 0 rgba(10,125,40,.5);
 }
 @keyframes scoreGlow{
   0%,100% { box-shadow:0 0 0 0 rgba(10,125,40,.0); }
   50%     { box-shadow:0 0 0 6px rgba(10,125,40,.18); }
 }

 /* Emoji wiggle on hover (apply to .wiggle class) */
 .wiggle{display:inline-block;transition:transform .3s}
 .wiggle:hover{animation:wiggle .5s ease-in-out}
 @keyframes wiggle{
   0%,100% { transform:rotate(0deg); }
   25%     { transform:rotate(-12deg) scale(1.1); }
   75%     { transform:rotate(12deg) scale(1.1); }
 }

 table{border-collapse:collapse;width:100%;font-size:.88rem}
 th{
   text-align:left;padding:10px 8px;border-bottom:2px solid var(--border);
   font-weight:600;font-size:.74rem;color:var(--text-muted);
   text-transform:uppercase;letter-spacing:.5px;background:var(--surface);
 }
 td{padding:11px 8px;border-bottom:1px solid var(--border);vertical-align:middle}
 tbody tr{transition:background .12s}
 tbody tr:nth-child(even){background:var(--surface-alt)}
 tbody tr:hover{background:var(--primary-light)}

 a{color:var(--primary);text-decoration:none;font-weight:500}
 a:hover{text-decoration:underline}

 button{
   padding:8px 16px;border-radius:var(--radius-sm);border:1px solid var(--border);
   background:var(--surface);color:var(--text);cursor:pointer;
   font-family:inherit;font-weight:500;font-size:.88rem;transition:all .15s;
 }
 button:hover{background:var(--primary-light);border-color:var(--primary);color:var(--primary)}
 button.primary{background:var(--primary);color:#fff;border-color:var(--primary);
                box-shadow:0 4px 12px rgba(79,70,229,.25)}
 button.primary:hover{background:var(--primary-hover);color:#fff;transform:translateY(-1px)}

 input[type=text],input[type=number],input[type=file],input:not([type]),select,textarea{
   padding:9px 11px;border:1px solid var(--border);border-radius:var(--radius-sm);
   background:var(--surface);color:var(--text);font-family:inherit;font-size:.9rem;
   transition:border-color .15s, box-shadow .15s;
 }
 input:focus,select:focus,textarea:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px rgba(79,70,229,.15)}

 details summary{user-select:none}
 .pill{display:inline-block;background:var(--primary-light);color:var(--primary);
       border-radius:999px;padding:3px 11px;margin:2px 4px 2px 0;font-size:.78rem;font-weight:500}

 .empty-state{text-align:center;padding:3rem 1.5rem;color:var(--text-muted)}
 .empty-state-icon{font-size:3rem;margin-bottom:.8rem;opacity:.55}
 .empty-state-title{font-size:1.05rem;font-weight:600;margin-bottom:.3rem;color:var(--text)}

 @media (max-width:720px){
   body{padding:1rem}
   .stats{grid-template-columns:repeat(2,1fr)}
   .hero{padding:1.2rem;flex-direction:column;align-items:flex-start}
   .hero h1{font-size:1.4rem}
   table{font-size:.8rem}
   th,td{padding:8px 4px}
   .card{padding:1rem 1.1rem}
 }
</style>
"""
_CSS = _HEAD  # alias kept for backward-compat in case anything else imports it


def _clear_jobs() -> None:
    """Drop all non-applied jobs, so the list only ever holds the active résumé's search."""
    get_supabase().table("jobs").delete().neq("status", "applied").execute()


def _refresh_for_active() -> None:
    """Clear old jobs. The user must re-upload the active résumé to trigger a
    fresh Chromium scrape — we don't auto-run Adzuna or any API source here.
    Chromium-only is intentional.
    """
    _clear_jobs()


_FILTER_LABELS = {
    "location": "📍 ", "experience": "🎓 ", "company_size": "🏢 ",
    "date_posted": "📅 last ", "work_mode": "💻 ", "job_type": "💼 ",
    "min_salary": "💰 ≥", "min_equity": "📈 ≥", "joining_date": "🚪 ",
    "notice_period": "⏳ ", "employee_count": "👥 ", "company_stage": "🌱 ",
    "industry": "🏷 ", "required_skills": "🛠 ", "visa_sponsorship": "🛂 visa sponsored",
    "jobs_for_women": "👩 women-only", "sort_by": "↕ sort: ",
}
_FILTER_SUFFIX = {"date_posted": " days", "min_salary": " LPA", "min_equity": "%",
                  "notice_period": " days"}


def _filter_pills_html() -> str:
    """Compact chips showing each active filter — visible on the home page."""
    pills = []
    for key, val in _LAST_FILTERS.items():
        if not val or val == "score":  # "score" is the default sort, skip showing
            continue
        if key in ("visa_sponsorship", "jobs_for_women"):
            txt = _FILTER_LABELS[key]
        else:
            prefix = _FILTER_LABELS.get(key, f"{key}: ")
            suffix = _FILTER_SUFFIX.get(key, "")
            txt = f"{prefix}{val}{suffix}"
        pills.append(f"<span class='pill'>{html.escape(txt)}</span>")
    if not pills:
        return ""
    return ("<div style='margin:.4rem 0 1rem'><b style='font-size:.78rem;"
            "color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px'>"
            "Active filters</b><br>" + "".join(pills) + "</div>")


_MARQUEE_PLATFORMS = [
    "🟧 Naukri", "🟦 LinkedIn", "🟪 Wellfound", "🟫 Indeed", "🟨 Foundit",
    "⚫ Shine", "🟢 Apna", "🟣 Internshala", "🟡 Glassdoor", "🔵 Hirist",
    "🟤 Cutshort", "🟪 Instahyre", "🅻 Razorpay", "🍕 Zomato", "💸 PhonePe",
    "💳 Cred", "🛒 Meesho", "🚕 Ola", "💱 Paytm", "🌿 Freshworks",
    "📈 Zerodha", "📊 Groww", "📮 Postman", "🌐 Zoho", "🥡 Swiggy",
    "📦 Flipkart", "🐦 BYJU's", "🎓 Unacademy", "🏏 Dream11", "✈️ MakeMyTrip",
    "🛡 PolicyBazaar", "💄 Nykaa", "🏠 NoBroker", "🥗 PharmEasy", "💊 Practo",
    "💧 boAt", "🛍 Mamaearth", "🅖 Google", "🪟 Microsoft", "🛒 Amazon",
    "Ⓜ️ Meta", "🅰 Adobe", "💼 IBM", "🟩 Nvidia", "🍎 Apple", "🧡 Oracle",
    "☁️ Salesforce", "🟦 SAP", "🌐 Cisco", "💼 Intel", "🚖 Uber", "🎬 Netflix",
    "🏦 JPMorgan", "📊 Goldman", "🅖 Genpact", "🟪 Accenture", "🟦 TCS",
    "🟨 Infosys", "🟩 Wipro", "🟥 HCL", "🌟 Capgemini",
]


def _platform_marquee_html() -> str:
    """Infinite-scroll bar of all the platforms we scrape — visible just under
    the hero. Looks like the 'as seen in' / 'trusted by' strip on every modern
    startup landing page. CSS handles the loop; we duplicate the content so the
    translateX(-50%) reset is invisible.
    """
    pills = "".join(f"<span>{html.escape(p)}</span>" for p in _MARQUEE_PLATFORMS)
    return (f"<div class='marquee' aria-hidden='true'>"
            f"<div class='marquee-track'>{pills}{pills}</div></div>")


def _greeting() -> str:
    """Time-of-day-aware greeting."""
    h = datetime.now().hour
    if h < 12: return "Good morning"
    if h < 18: return "Good afternoon"
    return "Good evening"


def _stat_card(value: int, label: str, kind: str = "primary") -> str:
    return (f"<div class='stat {kind}'>"
            f"<div class='stat-value'>{value}</div>"
            f"<div class='stat-label'>{label}</div></div>")


def _stats_strip(jobs: list[dict], active_count: int) -> str:
    """5 metric cards across the top: Matches / Saved / Applied / Interviewing / Offers."""
    by_status = {"saved": 0, "applied": 0, "interviewing": 0, "offer": 0, "rejected": 0}
    for j in jobs:
        st = j.get("status") or "scored"
        if st in by_status:
            by_status[st] += 1
    return ("<div class='stats'>"
            + _stat_card(active_count, "💼 Matches", "primary")
            + _stat_card(by_status["saved"], "⭐ Saved", "info")
            + _stat_card(by_status["applied"], "✅ Applied", "success")
            + _stat_card(by_status["interviewing"], "📞 Interviewing", "warn")
            + _stat_card(by_status["offer"], "🎉 Offers", "success")
            + "</div>")


def _smart_banner_html(active: dict | None, jobs: list[dict], active_count: int) -> str:
    """One context-aware banner that nudges the user toward the next useful action."""
    if not active:
        return ("<div class='banner info'>👋 <span><b>Welcome!</b> Upload your résumé "
                "below to scan 108 platforms and find matching roles.</span></div>")
    if active_count == 0:
        return ("<div class='banner info'>📭 <span>No matches yet — click "
                "<b>Upload résumé + Search jobs</b> to start the 108-site walk.</span></div>")
    strong = sum(1 for j in jobs if (j.get("score") or 0) >= 70)
    if strong > 0:
        return (f"<div class='banner success'>🎯 <span>You have <b>{strong} strong match"
                f"{'es' if strong != 1 else ''}</b> (score ≥ 70) — review them at the top.</span></div>")
    unset = sum(1 for j in jobs if (j.get("status") or "scored") == "scored")
    if unset > 5:
        return (f"<div class='banner warn'>⚡ <span><b>{unset} jobs</b> need your review — "
                f"use the Status dropdown to track which ones you've saved or applied to.</span></div>")
    applied = sum(1 for j in jobs if (j.get("status") or "") == "applied")
    if applied > 0 and not any((j.get("status") or "") == "interviewing" for j in jobs):
        return (f"<div class='banner info'>📬 <span><b>{applied} application"
                f"{'s' if applied != 1 else ''} sent</b> — follow up on any that haven't responded yet.</span></div>")
    return ""


def _extract_salary_lpa(job: dict) -> float | None:
    """Best-effort: pull an annual salary in LPA from title/description/raw."""
    blob = " ".join(str(x or "") for x in (job.get("title"), job.get("description"),
                                            (job.get("raw") or {}).get("salary")))
    if not blob:
        return None
    import re as _re
    # "12 LPA", "₹15,00,000", "$120,000", "Rs 8 lakh", "8-12 LPA"
    m = _re.search(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*[-–to]+\s*(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:LPA|lpa|lakh)", blob)
    if m:
        try: return float(m.group(1).replace(",", ""))
        except ValueError: pass
    m = _re.search(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:LPA|lpa|lakh)", blob)
    if m:
        try: return float(m.group(1).replace(",", ""))
        except ValueError: pass
    return None


def _apply_display_filters(jobs: list[dict]) -> list[dict]:
    """Apply post-scrape display filters (min salary, required skills) and sort."""
    f = _LAST_FILTERS
    out = list(jobs)

    if f.get("min_salary"):
        try:
            floor = float(f["min_salary"])
            out = [j for j in out if (s := _extract_salary_lpa(j)) is None or s >= floor]
        except ValueError:
            pass

    req = (f.get("required_skills") or "").lower()
    if req:
        wanted = [w.strip() for w in req.split(",") if w.strip()]
        def has_all(j: dict) -> bool:
            blob = " ".join(str(x or "") for x in (j.get("title"), j.get("description"),
                                                    j.get("score_reason"))).lower()
            return all(w in blob for w in wanted)
        out = [j for j in out if has_all(j)]

    sort_by = f.get("sort_by") or "score"
    if sort_by == "recent":
        out.sort(key=lambda j: (j.get("raw") or {}).get("created") or "", reverse=True)
    elif sort_by == "title":
        out.sort(key=lambda j: (j.get("title") or "").lower())
    # default "score" is already applied by the Supabase query

    return out


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


_STATUS_OPTIONS = [
    ("scored", "— set —"),
    ("saved", "⭐ Saved"),
    ("applied", "✅ Applied"),
    ("interviewing", "📞 Interviewing"),
    ("offer", "🎉 Offer"),
    ("rejected", "❌ Rejected"),
]


_LIMIT_OPTIONS = [("30", "Top 30"), ("70", "Top 70"), ("all", "All matches")]


def _limit_dropdown(current: str) -> str:
    """The 'Show: Top 30 / Top 70 / All' selector. Pure client-side — toggles row
    visibility via JS, no URL params, no page reload. Choice persists in localStorage.
    """
    opts = ""
    for val, label in _LIMIT_OPTIONS:
        opts += f"<option value='{val}'>{html.escape(label)}</option>"
    return (f"<label style='font-size:.85rem;color:var(--text-muted);margin-left:.6rem;font-weight:500'>"
            f"Show: <select id='showLimit' onchange='applyShowLimit()' "
            f"style='padding:3px 8px;font-size:.85rem;border:1px solid var(--border);"
            f"border-radius:6px;background:var(--surface);color:var(--text)'>{opts}</select></label>")


def _score_badge(score) -> str:
    """Color-coded pill: green ≥70 (with pulse glow on ≥90), yellow 40-69, red <40."""
    if score is None:
        return "<span style='color:#888'>-</span>"
    s = int(score)
    if s >= 70:    bg = "#0a7d28"
    elif s >= 40:  bg = "#c69500"
    else:          bg = "#bb2222"
    cls = " score-glow" if s >= 90 else ""
    return (f"<span class='{cls.strip()}' style='background:{bg};color:#fff;padding:3px 11px;"
            f"border-radius:999px;font-weight:700;font-size:.85rem;display:inline-block'>{s}</span>")


def _status_dropdown(job_id: str, current: str | None) -> str:
    opts = ""
    cur = current or "scored"
    for val, label in _STATUS_OPTIONS:
        sel = " selected" if val == cur else ""
        opts += f"<option value='{html.escape(val)}'{sel}>{html.escape(label)}</option>"
    return (f"<form method='post' action='/ui/status' style='display:inline;margin:0'>"
            f"<input type='hidden' name='job_id' value='{html.escape(str(job_id))}'>"
            f"<select name='status' onchange='this.form.submit()' "
            f"style='padding:2px 4px;font-size:.82rem;border:1px solid #ccc;border-radius:4px'>{opts}</select>"
            f"</form>")


def _job_rows(jobs: list[dict]) -> str:
    rows = ""
    for i, j in enumerate(jobs, start=1):
        url = j.get("url") or ""
        title = str(j.get("title") or "")
        desc_short = (j.get("description") or "")[:240]
        raw = j.get("raw") or {}
        posted = (raw.get("created") or "")[:10] if raw.get("created") else (raw.get("posted_text") or "-")
        source = j.get("source") or raw.get("site") or ""
        title_html = (
            f"<a href='{html.escape(url)}' target='_blank' rel='noopener' "
            f"title='{html.escape(desc_short)}'>{html.escape(title)}</a>"
            if url else html.escape(title)
        )
        apply_html = (
            f"<a href='{html.escape(url)}' target='_blank' rel='noopener'>Apply &rarr;</a>"
            if url else ""
        )
        source_html = (
            f"<span style='background:#eef;color:#334;padding:1px 7px;border-radius:4px;"
            f"font-size:.78rem'>{html.escape(source)}</span>" if source else ""
        )
        rows += (f"<tr><td>{i}</td>"
                 f"<td>{_score_badge(j.get('score'))}</td>"
                 f"<td>{title_html}</td>"
                 f"<td>{html.escape(str(j.get('company') or ''))}</td>"
                 f"<td>{html.escape(str(j.get('location') or ''))}</td>"
                 f"<td>{html.escape(str(posted))}</td>"
                 f"<td>{source_html}</td>"
                 f"<td>{_status_dropdown(j.get('id'), j.get('status'))}</td>"
                 f"<td>{apply_html}</td></tr>")
    if rows:
        return rows
    return ("<tr><td colspan='9'><div class='empty-state'>"
            "<div class='empty-state-icon'>📭</div>"
            "<div class='empty-state-title'>No jobs here yet</div>"
            "<div class='muted'>Upload your résumé and click the search button — "
            "the 108-site walk will fill this in.</div></div></td></tr>")


def _page() -> str:
    """Renders the full page. The 'Show:' dropdown is purely client-side now —
    all rows go into the HTML and JS toggles visibility based on the user's choice.
    """
    profiles = list_profiles()
    active = get_active()
    HARD_CAP = 200  # never render more than this server-side

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

    empty = ("<tr><td colspan='9'><div class='empty-state'>"
             "<div class='empty-state-icon'>👋</div>"
             "<div class='empty-state-title'>Upload a résumé to begin</div>"
             "<div class='muted'>Drop your PDF/DOCX into the form above — we'll scan 108 platforms for you.</div>"
             "</div></td></tr>")
    active_html = past_html = empty
    active_count = past_count = 0
    active_jobs: list[dict] = []
    if active:
        jobs = (get_supabase().table("jobs")
                .select("id,source,status,title,company,location,score,score_reason,raw,url,description")
                .gt("score", 15)  # only résumé-matching jobs (drops unscored + no-overlap)
                .order("score", desc=True).limit(200).execute().data)
        jobs = _apply_display_filters(jobs)
        active_jobs = [j for j in jobs if not _is_expired(j)]
        past_jobs = [j for j in jobs if _is_expired(j)]
        active_count, past_count = len(active_jobs), len(past_jobs)
        active_html = _job_rows(active_jobs[:HARD_CAP])
        past_html = _job_rows(past_jobs[:HARD_CAP])

    # Hero / stats / smart banner — only rendered once per page load
    greeting = _greeting()
    profile_name = html.escape(str((active or {}).get("label") or "there").split(".")[0])
    if active:
        subtitle = (f"{greeting}, {profile_name} &middot; Matching your résumé across "
                    f"<b>108 platforms</b> &middot; <b>{active_count}</b> live matches found")
    else:
        subtitle = f"{greeting} &middot; Upload a résumé to scan 108 job platforms"
    # When no résumé is active, hide the stat strip entirely — empty zeros
    # everywhere are visual noise. The hero subtitle already nudges to upload.
    stats_strip = _stats_strip(active_jobs, active_count) if active else ""
    marquee_html = ""  # placeholder; the marquee renders below the hero always — let HTML have it

    # ── Job-list block: only rendered when a résumé is active. When there's
    #    no résumé at all, the two job cards are REPLACED by a single big
    #    "Upload to begin" CTA so the page is calm and obvious.
    if active:
        job_block = f"""
<div class="card">
  <h3><span>💼 Active jobs <span style="color:var(--primary);font-weight:600;font-size:.9rem">— {active_count} match{('' if active_count == 1 else 'es')}</span></span>{_limit_dropdown('30')}</h3>
  {_filter_pills_html()}
  <table><thead><tr><th>#</th><th>Score</th><th>Title</th><th>Company</th><th>Location</th><th>Posted</th><th>Source</th><th>Status</th><th>Apply</th></tr></thead>
  <tbody>{active_html}</tbody></table>
</div>

<div class="card">
  <h3>📋 Past / expired jobs <span class="muted">— {past_count} (posted &gt; {_EXPIRE_DAYS} days ago)</span></h3>
  <table><thead><tr><th>#</th><th>Score</th><th>Title</th><th>Company</th><th>Location</th><th>Posted</th><th>Source</th><th>Status</th><th>Apply</th></tr></thead>
  <tbody>{past_html}</tbody></table>
</div>"""
    else:
        job_block = """
<div class="card" style="text-align:center;padding:3.5rem 2rem;background:linear-gradient(135deg,rgba(79,70,229,.06) 0%,rgba(236,72,153,.04) 50%,rgba(6,182,212,.06) 100%);border:2px dashed var(--primary)">
  <div style="font-size:5rem;line-height:1;margin-bottom:.8rem" class="wiggle">🚀</div>
  <h2 style="margin:0 0 .6rem;font-size:1.9rem;font-weight:800" class="gradient-text-color">Ready to find your next role?</h2>
  <p class="muted" style="font-size:1rem;max-width:540px;margin:0 auto 1.4rem">
    Drop your résumé in the form above and we'll scan <b>108 job platforms</b>
    (Naukri · LinkedIn · Razorpay · Google · Adobe · TCS · and more),
    score every match against your skills, and rank them here.
  </p>
  <p class="muted" style="font-size:.9rem;display:flex;justify-content:center;gap:1.5rem;flex-wrap:wrap">
    <span>📄 Upload PDF or DOCX</span>
    <span>🎯 Pick filters</span>
    <span>🚀 Click Search</span>
    <span>✨ See ranked jobs</span>
  </p>
</div>"""
    smart_banner = _smart_banner_html(active, active_jobs, active_count)
    vercel_banner = ('<div class="banner warn">☁️ <span><b>Hosted on Vercel.</b> '
                     'Uploads search Adzuna + Remotive + RemoteOK APIs (~2 s). For '
                     'the 108-site visible Chromium walk, run the project locally '
                     'and double-click <code>start.bat</code>.</span></div>'
                     if IS_VERCEL else '')
    cloud_banner = ('<div class="banner info">☁️ <span><b>Hosted on Render</b> &middot; '
                    'After clicking Upload, the headless Chromium 108-site scrape '
                    'runs <b>in the background</b> (no pop-up windows — cloud has no screen). '
                    'Wait <b>3–5 minutes</b> then <b>refresh this page</b> to see new jobs. '
                    'Live progress is in <i>Render dashboard → Logs tab</i>.</span></div>'
                    if IS_CLOUD else '')

    return f"""<!doctype html><html lang="en" data-theme="light"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Career Agent</title>{_HEAD}</head><body data-job-count="{active_count}">
<!-- Animated gradient blobs (color haze) -->
<div class="bg-blob bg-blob-1"></div>
<div class="bg-blob bg-blob-2"></div>
<div class="bg-blob bg-blob-3"></div>

<!-- Outlined geometric shapes drifting at viewport edges (above cards) -->
<div class="deco deco-circle"></div>
<div class="deco deco-square"></div>
<div class="deco deco-ring"></div>
<div class="deco deco-triangle"></div>

<!-- Career-themed emoji decorations drifting at different speeds -->
<div class="floater floater-1">💼</div>
<div class="floater floater-2">🚀</div>
<div class="floater floater-3">⭐</div>
<div class="floater floater-4">📈</div>
<div class="floater floater-5">🎯</div>
<div class="floater floater-6">✨</div>
<div class="floater floater-7">💡</div>
<div class="floater floater-8">📊</div>
<div class="floater floater-9">🏆</div>
<div class="floater floater-10">🎓</div>
<div class="floater floater-11">💻</div>
<div class="floater floater-12">🌟</div>

<header class="hero">
  <div>
    <div class="hero-title"><span class="wiggle" style="font-size:2.2rem">🎯</span><h1 class="gradient-text">AI Career Agent</h1><span class="wiggle" style="font-size:1.4rem">✨</span></div>
    <p class="hero-subtitle">{subtitle}</p>
  </div>
  <button class="theme-toggle" id="themeBtn" onclick="toggleTheme()" title="Toggle light/dark mode">🌙</button>
</header>

{_platform_marquee_html()}

{stats_strip}
{smart_banner}
{vercel_banner}
{cloud_banner}

<div class="card">
  <h3>📄 Your résumé</h3>
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
    <select name="scrape_limit" id="scrape_limit" onchange="updateEstimate()" style="padding:5px" title="How many jobs to collect before stopping the scrape.">
      <option value="30">Search Top 30</option>
      <option value="70">Search Top 70</option>
      <option value="all">Search All matches</option>
    </select>
    &nbsp;
    <span id="time-est" class="muted" style="font-size:.85rem"></span>
    <br><br>
    <button class="primary" type="submit">🚀 Upload résumé + Search jobs</button>
    <details style="margin-top:12px">
      <summary style="cursor:pointer;color:#1d6ef0;font-weight:600">+ Advanced filters (date posted / job type / work mode / salary / skills / company stage / sort / …)</summary>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:12px;padding:10px;background:#fafafa;border-radius:6px">
        <label>Date posted
          <select name="date_posted" style="width:100%;padding:5px">
            <option value="">Any time</option>
            <option value="1">Last 24 hours</option>
            <option value="3">Last 3 days</option>
            <option value="7">Last week</option>
            <option value="30">Last month</option>
          </select>
        </label>
        <label>Work mode
          <select name="work_mode" style="width:100%;padding:5px">
            <option value="">Any work mode</option>
            <option value="onsite">On-site</option>
            <option value="hybrid">Hybrid</option>
            <option value="remote">Remote</option>
          </select>
        </label>
        <label>Job type
          <select name="job_type" style="width:100%;padding:5px">
            <option value="">Any job type</option>
            <option value="fulltime">Full-time</option>
            <option value="parttime">Part-time</option>
            <option value="contract">Contract</option>
            <option value="temporary">Temporary</option>
            <option value="internship">Internship</option>
            <option value="freelance">Freelance</option>
          </select>
        </label>
        <label>Min salary (LPA)
          <input name="min_salary" type="number" min="0" step="1" style="width:100%;padding:5px" placeholder="e.g. 8">
        </label>
        <label>Min equity (%)
          <input name="min_equity" type="number" min="0" max="100" step="0.1" style="width:100%;padding:5px" placeholder="e.g. 0.25">
        </label>
        <label>Joining date
          <select name="joining_date" style="width:100%;padding:5px">
            <option value="">Any</option>
            <option value="immediate">Immediately</option>
            <option value="within_1_month">Within 1 month</option>
            <option value="flexible">Flexible</option>
          </select>
        </label>
        <label>Notice period
          <select name="notice_period" style="width:100%;padding:5px">
            <option value="">Any</option>
            <option value="15">15 days</option>
            <option value="30">30 days</option>
            <option value="60">60 days</option>
            <option value="90">90 days</option>
          </select>
        </label>
        <label>Employee count
          <select name="employee_count" style="width:100%;padding:5px">
            <option value="">Any</option>
            <option value="1-10">1–10</option>
            <option value="11-50">11–50</option>
            <option value="51-200">51–200</option>
            <option value="201-500">201–500</option>
            <option value="501-1000">501–1,000</option>
            <option value="1001-5000">1,001–5,000</option>
            <option value="5000+">5,000+</option>
          </select>
        </label>
        <label>Company stage
          <select name="company_stage" style="width:100%;padding:5px">
            <option value="">Any</option>
            <option value="seed">Seed</option>
            <option value="series-a">Series A</option>
            <option value="series-b">Series B</option>
            <option value="series-c">Series C+</option>
            <option value="public">Public</option>
          </select>
        </label>
        <label>Industry
          <input name="industry" style="width:100%;padding:5px" placeholder="e.g. FinTech, HealthTech, SaaS">
        </label>
        <label style="grid-column:span 3">Required skills (comma-separated)
          <input name="required_skills" style="width:100%;padding:5px" placeholder="e.g. python, aws, react, kubernetes">
        </label>
        <label style="grid-column:span 3">Company priority order <span class="muted" style="font-weight:400">(overrides "Company size" above &mdash; 1st-preference career pages are scraped first, then 2nd, then 3rd)</span>
          <div style="display:flex;gap:10px;align-items:center;margin-top:6px">
            <span style="font-size:.85rem;min-width:36px">1st:</span>
            <select name="priority_1" style="flex:1;padding:5px">
              <option value="">— none —</option>
              <option value="startup">Startup / Unicorn (1–500 employees)</option>
              <option value="midlevel">Mid-level / IT services (500–50,000)</option>
              <option value="mnc">MNC (50,000+)</option>
            </select>
            <span style="font-size:.85rem;min-width:36px">2nd:</span>
            <select name="priority_2" style="flex:1;padding:5px">
              <option value="">— none —</option>
              <option value="startup">Startup / Unicorn</option>
              <option value="midlevel">Mid-level / IT services</option>
              <option value="mnc">MNC</option>
            </select>
            <span style="font-size:.85rem;min-width:36px">3rd:</span>
            <select name="priority_3" style="flex:1;padding:5px">
              <option value="">— none —</option>
              <option value="startup">Startup / Unicorn</option>
              <option value="midlevel">Mid-level / IT services</option>
              <option value="mnc">MNC</option>
            </select>
          </div>
        </label>
        <label style="display:flex;align-items:center;gap:6px">
          <input type="checkbox" name="visa_sponsorship" value="1"> Visa sponsorship required
        </label>
        <label style="display:flex;align-items:center;gap:6px">
          <input type="checkbox" name="jobs_for_women" value="1"> Jobs for women
        </label>
        <label>Sort by
          <select name="sort_by" style="width:100%;padding:5px">
            <option value="score">Match score (default)</option>
            <option value="recent">Most recent</option>
            <option value="title">Title A → Z</option>
          </select>
        </label>
      </div>
      <p class="muted" style="margin-top:6px;font-size:.8rem">Date posted, Work mode, Job type and Experience are injected into Naukri / LinkedIn / Indeed URLs where each site supports the parameter. Other filters are captured and applied to the displayed result list.</p>
    </details>
  </form>
  <p class="muted">Uploading opens visible Chromium windows that walk <b>up to ~108 platforms</b>: 15 job boards (Naukri, LinkedIn, Indeed, Foundit, Shine, Glassdoor, Apna, Internshala, JobsForHer, WorkIndia, Hirist, Cutshort, Instahyre, TimesJobs, Wellfound) + 46 Indian startups/unicorns (Razorpay, Zomato, PhonePe, Cred, Meesho, BYJU's, Unacademy, Dream11, MakeMyTrip, Nykaa, BharatPe, Urban Company, Lenskart, OYO, Acko, Practo, PharmEasy, etc.) + 15 IT services (TCS, Infosys, Wipro, HCL, Tech Mahindra, Cognizant, Capgemini, LTIMindtree, etc.) + 32 MNCs (Google, Microsoft, Amazon, Meta, Apple, Oracle, Salesforce, SAP, Cisco, Intel, Adobe, IBM, Nvidia, Atlassian, ServiceNow, Snowflake, Databricks, Uber, Netflix, JPMorgan, Goldman, Citi, Deloitte, Accenture, etc.). <b>Location</b> is injected into the URL of every site that supports it. <b>Company size</b> picks which career pages are scraped — Startup (~61 sites), Mid-level (~30 sites), MNC (~47 sites), or blank for all ~108. Boards always run.</p>
  <table><thead><tr><th>Resume</th><th>Keywords</th><th>Active</th><th></th></tr></thead>
  <tbody>{prof_rows}</tbody></table>
</div>

{job_block}

<footer class="muted" style="text-align:center;padding:1rem 0 2rem;font-size:.82rem">
  Built with FastAPI + Playwright + Supabase &middot; All matches are scored locally against your résumé skills.
</footer>

<!-- Loading overlay shown while the scrape runs -->
<div id="loadingOverlay" style="display:none;position:fixed;inset:0;background:rgba(11,13,20,.72);
     backdrop-filter:blur(6px);z-index:9999;align-items:center;justify-content:center">
  <div style="background:var(--surface);color:var(--text);border-radius:16px;padding:2rem 2.5rem;
              box-shadow:0 25px 50px rgba(0,0,0,.3);text-align:center;min-width:340px">
    <div style="font-size:3rem;animation:spin 1.5s linear infinite;display:inline-block">🔍</div>
    <h3 style="margin:.6rem 0;font-size:1.15rem">Scraping platforms…</h3>
    <p class="muted" style="margin:.2rem 0;font-size:.88rem">Visible Chromium windows are
       popping up in sequence — keep them visible.</p>
    <div style="display:flex;justify-content:center;gap:2rem;margin-top:1.2rem">
      <div><div style="font-size:.74rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px">Estimated</div>
           <div style="font-size:1.3rem;font-weight:700;color:var(--primary)" id="loadEst">~1–3 min</div></div>
      <div><div style="font-size:.74rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px">Elapsed</div>
           <div style="font-size:1.3rem;font-weight:700;color:var(--accent)" id="loadElapsed">0:00</div></div>
    </div>
  </div>
</div>

<style>@keyframes spin{{from{{transform:rotate(0)}}to{{transform:rotate(360deg)}}}}</style>

<script>
// ----- theme toggle (persisted) -----
function toggleTheme(){{
  const cur = document.documentElement.dataset.theme || 'light';
  const next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try {{ localStorage.setItem('theme', next); }} catch(e) {{}}
  document.getElementById('themeBtn').textContent = next === 'dark' ? '☀️' : '🌙';
}}
(function(){{
  let saved = 'light';
  try {{ saved = localStorage.getItem('theme') || 'light'; }} catch(e) {{}}
  document.documentElement.dataset.theme = saved;
  const btn = document.getElementById('themeBtn');
  if (btn) btn.textContent = saved === 'dark' ? '☀️' : '🌙';
}})();

// ----- 'Show: Top 30/70/All' — pure client-side row hiding, persists in localStorage -----
function applyShowLimit(){{
  const sel = document.getElementById('showLimit');
  if (!sel) return;
  const v = sel.value;
  try {{ localStorage.setItem('showLimit', v); }} catch(e) {{}}
  const cap = v === 'all' ? Infinity : parseInt(v, 10);
  document.querySelectorAll('table tbody').forEach(tb => {{
    let shown = 0;
    tb.querySelectorAll('tr').forEach(tr => {{
      // Empty-state rows have colspan; skip them.
      if (tr.querySelector('td[colspan]')) return;
      if (shown < cap) {{ tr.style.display = ''; shown++; }}
      else tr.style.display = 'none';
    }});
  }});
}}
(function(){{
  let saved = '30';
  try {{ saved = localStorage.getItem('showLimit') || '30'; }} catch(e) {{}}
  const sel = document.getElementById('showLimit');
  if (sel) {{ sel.value = saved; applyShowLimit(); }}
}})();

// ----- live job streaming: poll /api/jobs/count every ~7s, reload when it grows -----
(function(){{
  const initial = parseInt(document.body.dataset.jobCount || '0', 10);
  let last = initial;
  let stableTicks = 0;
  async function tick(){{
    try {{
      const r = await fetch('/api/jobs/count', {{cache:'no-store'}});
      const d = await r.json();
      const n = d.count || 0;
      if (n > last) {{
        // New jobs landed — refresh so they appear in the table.
        location.reload();
        return;
      }}
      stableTicks = (n === last) ? stableTicks + 1 : 0;
      last = n;
      // Stop polling after ~3 min of stability — scrape is probably done.
      if (stableTicks < 25) setTimeout(tick, 7000);
    }} catch(e) {{ setTimeout(tick, 15000); }}
  }}
  setTimeout(tick, 7000);
}})();

// ----- estimated-time hint next to the Search dropdown -----
const TIME_EST = {{
  '30':  '⏱ ~1–3 min',
  '70':  '⏱ ~3–8 min',
  'all': '⏱ ~10–16 min (full 108-site sweep)'
}};
function updateEstimate(){{
  const sel = document.getElementById('scrape_limit');
  const out = document.getElementById('time-est');
  if (sel && out) out.textContent = TIME_EST[sel.value] || '';
}}
updateEstimate();  // run once on page load

// ----- loading overlay on form submit -----
(function(){{
  const form = document.querySelector('form[action="/ui/upload"]');
  if (!form) return;
  form.addEventListener('submit', function(){{
    const sel = document.getElementById('scrape_limit');
    const v = sel ? sel.value : '30';
    document.getElementById('loadEst').textContent = (TIME_EST[v] || '').replace('⏱ ','');
    const overlay = document.getElementById('loadingOverlay');
    overlay.style.display = 'flex';
    const start = Date.now();
    setInterval(() => {{
      const sec = Math.floor((Date.now() - start) / 1000);
      const m = Math.floor(sec / 60), s = (sec % 60).toString().padStart(2,'0');
      const el = document.getElementById('loadElapsed');
      if (el) el.textContent = m + ':' + s;
    }}, 1000);
  }});
}})();
</script>
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
    date_posted: str = Form(""),
    work_mode: str = Form(""),
    job_type: str = Form(""),
    min_salary: str = Form(""),
    min_equity: str = Form(""),
    joining_date: str = Form(""),
    notice_period: str = Form(""),
    employee_count: str = Form(""),
    company_stage: str = Form(""),
    industry: str = Form(""),
    required_skills: str = Form(""),
    visa_sponsorship: str = Form(""),
    jobs_for_women: str = Form(""),
    sort_by: str = Form("score"),
    scrape_limit: str = Form("30"),
    priority_1: str = Form(""),
    priority_2: str = Form(""),
    priority_3: str = Form(""),
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
    # Stash the active filter snapshot in module state so the home page can show
    # filter pills AND apply the post-scrape filters (salary / sort / required skills).
    _LAST_FILTERS.update({
        "location": location, "experience": experience, "company_size": company_size,
        "date_posted": date_posted, "work_mode": work_mode, "job_type": job_type,
        "min_salary": min_salary, "min_equity": min_equity,
        "joining_date": joining_date, "notice_period": notice_period,
        "employee_count": employee_count, "company_stage": company_stage,
        "industry": industry, "required_skills": required_skills,
        "visa_sponsorship": visa_sponsorship, "jobs_for_women": jobs_for_women,
        "sort_by": sort_by or "score",
    })
    # Build the filter env once — shared by all execution modes.
    sl = (scrape_limit or "30").strip().lower()
    scrape_n = "" if sl == "all" else (sl if sl.isdigit() else "30")
    env = {**os.environ,
           "FILTER_LOCATION": (location or "").strip(),
           "FILTER_EXPERIENCE": (experience or "").strip(),
           "FILTER_COMPANY_SIZE": (company_size or "").strip().lower(),
           "FILTER_DATE_POSTED": (date_posted or "").strip(),
           "FILTER_WORK_MODE": (work_mode or "").strip().lower(),
           "FILTER_JOB_TYPE": (job_type or "").strip().lower(),
           "FILTER_JOBS_FOR_WOMEN": "1" if jobs_for_women else "",
           "FILTER_VISA_SPONSORSHIP": "1" if visa_sponsorship else "",
           "SCRAPE_LIMIT": scrape_n,
           "COMPANY_PRIORITY_1": (priority_1 or "").strip().lower(),
           "COMPANY_PRIORITY_2": (priority_2 or "").strip().lower(),
           "COMPANY_PRIORITY_3": (priority_3 or "").strip().lower()}

    if IS_VERCEL:
        # Vercel can't run Chromium — fall back to API-source discovery.
        try:
            active = get_active()
            run_discovery(queries=derive_queries(active) if active else None)
            score_pending_keyword()
        except Exception:
            pass
    elif IS_CLOUD:
        # Render / Railway / Fly: detach the scrape so the HTTP request returns
        # IMMEDIATELY. Render's load balancer drops connections after ~100 s and
        # the browser would otherwise time out waiting for the 5-16 min walk.
        #
        # We do NOT redirect stdout/stderr — they inherit from uvicorn so the
        # scraper's per-site progress streams live into Render's Logs tab. The
        # child keeps these fds open even after the request handler returns
        # (start_new_session=True only detaches the process group, not the fds).
        #
        # `python3` (PATH-resolved by the shell) is used INSTEAD of sys.executable
        # because sys.executable in Microsoft's Playwright base image can point to
        # a python that doesn't have playwright. The container's PATH `python3` is
        # the same one the Dockerfile verifies playwright is importable from.
        cmd_str = ("python3 -m app.scrape.multi_site && "
                   "python3 -m app.scrape.hn_hiring")
        try:
            subprocess.Popen(
                ["sh", "-c", cmd_str],
                cwd=str(_PROJ), env=env,
                start_new_session=True,  # detach process group, keep stdout/stderr inherited
            )
        except Exception:
            pass
    else:
        # Local: visible Chromium, blocking.
        _visible_subprocess(
            [sys.executable, "-m", "app.scrape.multi_site"],
            env=env, timeout=1200,
        )
        _visible_subprocess(
            [sys.executable, "-m", "app.scrape.hn_hiring"],
            env=env, timeout=600,
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


_VALID_STATUSES = {s for s, _ in _STATUS_OPTIONS}


@router.get("/api/jobs/count")
def api_jobs_count():
    """Lightweight count endpoint the home-page JS polls every ~7s.

    When this number bumps up (because the live per-site upsert in
    multi_site.py landed new rows), the page reloads so the user sees the
    new jobs streaming in without manually hitting refresh.
    """
    res = (get_supabase().table("jobs")
           .select("id", count="exact")
           .gt("score", 15).execute())
    return {"count": res.count or 0}


@router.post("/ui/status")
def ui_status(job_id: str = Form(...), status: str = Form(...)):
    """Update a job's status from the per-row dropdown (Saved / Applied / Interviewing / …)."""
    if status not in _VALID_STATUSES:
        return RedirectResponse("/", status_code=303)
    get_supabase().table("jobs").update({"status": status}).eq("id", job_id).execute()
    return RedirectResponse("/", status_code=303)


