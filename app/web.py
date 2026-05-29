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

# Session flag — only True between an Upload and a Clear/Remove/server-restart.
# When False, the home page shows the empty 'Ready to find your next role' CTA
# even if there are jobs already in the database. This is the user's explicit
# ask: 'show job list ONLY when I upload the résumé'. Saved jobs stay in
# Supabase, they just stay hidden behind the CTA until the next upload.
_SESSION_HAS_UPLOAD = {"v": False}

_HEAD = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Manrope:wght@500;600;700;800&display=swap" rel="stylesheet">
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
 h1,h2,h3{font-family:'Manrope','Inter',system-ui,sans-serif;font-weight:800;letter-spacing:-.025em}
 h1{margin:0;font-size:2.3rem;line-height:1.05}
 h2{margin:0;font-size:1.6rem;line-height:1.15}
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
 tbody tr{transition:transform .18s, background .18s, box-shadow .18s}
 tbody tr:nth-child(even){background:rgba(0,0,0,.015)}
 [data-theme='dark'] tbody tr:nth-child(even){background:rgba(255,255,255,.02)}
 tbody tr:hover{
   background:var(--primary-light) !important;
   transform:translateX(4px);
   box-shadow:-4px 0 0 0 var(--primary), 0 2px 12px rgba(79,70,229,.08);
 }
 td{padding:13px 8px;border-bottom:1px solid var(--border);vertical-align:middle}
 tbody tr td:first-child{padding-left:14px}

 /* Hot match rows — gradient strip + pink accent border */
 tr.hot-row{
   background:linear-gradient(90deg, rgba(236,72,153,.10) 0%, rgba(236,72,153,.04) 30%, transparent 70%) !important;
   border-left:4px solid #ec4899;
 }
 tr.hot-row:hover{
   background:linear-gradient(90deg, rgba(236,72,153,.20) 0%, rgba(79,70,229,.08) 50%, transparent 90%) !important;
   box-shadow:-4px 0 0 0 #ec4899, 0 4px 18px rgba(236,72,153,.14);
 }
 tr.hot-row td:first-child{
   color:#ec4899 !important;font-weight:800;
 }

 /* Active jobs header gets a colorful icon backdrop */
 .card h3 .h3-icon{
   display:inline-flex;align-items:center;justify-content:center;
   width:34px;height:34px;border-radius:10px;margin-right:.5rem;
   background:linear-gradient(135deg,var(--primary),#ec4899);color:#fff;
   font-size:1.1rem;box-shadow:0 4px 10px rgba(79,70,229,.25);
 }

 /* Custom file-picker button — gradient pill instead of ugly OS file input */
 .file-picker{
   display:inline-flex;align-items:center;gap:.5rem;cursor:pointer;
   padding:9px 16px;border-radius:10px;font-weight:700;font-size:.92rem;
   background:linear-gradient(135deg,var(--primary) 0%,#7c3aed 50%,#ec4899 100%);
   color:#fff;border:none;
   box-shadow:0 4px 14px rgba(124,58,237,.32);
   transition:transform .15s, box-shadow .2s;
 }
 .file-picker:hover{
   transform:translateY(-2px);
   box-shadow:0 6px 20px rgba(124,58,237,.5);
 }
 .file-picker:active{ transform:translateY(0) scale(.98); }
 .file-name-display{
   display:inline-block;margin-left:.6rem;padding:6px 12px;
   background:var(--surface-alt);border:1px dashed var(--border);
   border-radius:8px;font-size:.85rem;color:var(--text-muted);
   max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
   vertical-align:middle;
 }
 .file-name-display.has-file{
   color:var(--text);background:var(--primary-light);
   border-color:var(--primary);border-style:solid;font-weight:600;
 }

 /* Danger button for 'Clear results' */
 button.danger{
   background:rgba(239,68,68,.08);color:#ef4444;border:1px solid rgba(239,68,68,.3);
   font-weight:600;
 }
 button.danger:hover{
   background:#ef4444;color:#fff;border-color:#ef4444;
 }

 /* ───── Hero-style upload card layout ───── */
 .upload-hero{
   display:grid;grid-template-columns:1.4fr 1fr;gap:2rem;align-items:center;
 }
 @media (max-width:880px){ .upload-hero{ grid-template-columns:1fr; gap:1.4rem; } }

 .how-flow{
   display:flex;flex-direction:column;gap:.8rem;
   padding:1.4rem;border-radius:14px;
   background:linear-gradient(135deg, rgba(79,70,229,.06), rgba(236,72,153,.04));
   border:1px solid var(--border);
 }
 .how-step{
   display:flex;align-items:center;gap:.8rem;
   padding:.5rem .6rem;border-radius:10px;
   transition:background .2s;
 }
 .how-step:hover{ background:rgba(79,70,229,.06); }
 .how-step-num{
   flex-shrink:0;width:30px;height:30px;border-radius:50%;
   background:linear-gradient(135deg,var(--primary),#ec4899);color:#fff;
   font-weight:800;font-size:.88rem;
   display:flex;align-items:center;justify-content:center;
   box-shadow:0 3px 8px rgba(79,70,229,.25);
 }
 .how-step-text{font-size:.92rem;font-weight:500;color:var(--text)}
 .how-step-sub{font-size:.78rem;color:var(--text-muted);margin-top:1px}

 /* Premium primary CTA — replaces the .primary button on key actions */
 button.cta, .cta{
   padding:13px 28px;border-radius:12px;border:none;cursor:pointer;
   background:linear-gradient(135deg,var(--primary) 0%,#7c3aed 50%,#ec4899 100%);
   background-size:200% 200%;background-position:0% 50%;
   color:#fff;font-family:inherit;font-weight:800;font-size:1rem;letter-spacing:-.01em;
   box-shadow:0 8px 24px rgba(124,58,237,.35), inset 0 1px 0 rgba(255,255,255,.2);
   transition:transform .2s, box-shadow .2s, background-position .35s;
   position:relative;overflow:hidden;
 }
 button.cta:hover, .cta:hover{
   transform:translateY(-2px);
   background-position:100% 50%;
   box-shadow:0 12px 32px rgba(124,58,237,.5), inset 0 1px 0 rgba(255,255,255,.3);
 }
 button.cta:active, .cta:active{ transform:translateY(0); }
 button.cta::before, .cta::before{
   content:'';position:absolute;inset:0;
   background:linear-gradient(120deg, transparent 35%, rgba(255,255,255,.4) 50%, transparent 65%);
   transform:translateX(-100%);transition:transform 0s;
 }
 button.cta:hover::before, .cta:hover::before{ transform:translateX(100%);transition:transform .9s; }

 /* ───── Featured Match card — premium card shown above the table ───── */
 .featured-match{
   border-radius:16px;padding:1.6rem 1.8rem;margin-bottom:1.4rem;
   background:linear-gradient(135deg, #4f46e5 0%, #7c3aed 50%, #ec4899 100%);
   color:#fff;box-shadow:0 20px 50px rgba(79,70,229,.3);
   position:relative;overflow:hidden;
 }
 .featured-match::before{
   content:'';position:absolute;inset:0;
   background:radial-gradient(circle at 20% 30%, rgba(255,255,255,.18), transparent 50%);
   pointer-events:none;
 }
 .featured-label{
   display:inline-flex;align-items:center;gap:.4rem;
   background:rgba(255,255,255,.2);backdrop-filter:blur(6px);
   color:#fff;padding:5px 12px;border-radius:999px;
   font-size:.74rem;font-weight:800;letter-spacing:.6px;text-transform:uppercase;
   margin-bottom:.7rem;
 }
 .featured-title{
   font-size:1.5rem;font-weight:800;margin:0 0 .4rem;color:#fff;
   font-family:'Manrope','Inter',sans-serif;letter-spacing:-.02em;
 }
 .featured-company{ font-size:1rem;opacity:.95;margin-bottom:.8rem; }
 .featured-meta{
   display:flex;flex-wrap:wrap;gap:1rem;font-size:.88rem;opacity:.92;
   margin-bottom:1rem;
 }
 .featured-cta{
   display:inline-block;background:#fff;color:var(--primary);
   padding:10px 22px;border-radius:10px;font-weight:700;text-decoration:none;
   box-shadow:0 4px 12px rgba(0,0,0,.15);transition:transform .15s;
 }
 .featured-cta:hover{transform:translateY(-2px);text-decoration:none;}
 .featured-score{
   position:absolute;top:1.4rem;right:1.6rem;
   font-size:3.2rem;font-weight:800;line-height:1;
   color:rgba(255,255,255,.95);font-family:'Manrope','Inter',sans-serif;
   letter-spacing:-.04em;
 }
 .featured-score-label{
   position:absolute;top:.8rem;right:1.7rem;
   font-size:.66rem;font-weight:800;letter-spacing:1.2px;
   color:rgba(255,255,255,.7);text-transform:uppercase;
 }

 /* Scroll-reveal: cards fade and rise on appearance */
 .reveal{ opacity:0;transform:translateY(20px);transition:opacity .6s ease, transform .6s ease; }
 .reveal.in{ opacity:1;transform:translateY(0); }

 /* ───── Advanced filters — modern sectioned layout (LinkedIn / Naukri style) ───── */
 details.adv-filters{
   margin-top:1.2rem;border-radius:14px;
   background:linear-gradient(135deg, rgba(79,70,229,.04), rgba(236,72,153,.02));
   border:1px solid var(--border);overflow:hidden;
 }
 details.adv-filters > summary{
   cursor:pointer;padding:14px 18px;list-style:none;
   display:flex;align-items:center;justify-content:space-between;
   font-weight:700;color:var(--text);font-size:.95rem;
   transition:background .2s;
 }
 details.adv-filters > summary::-webkit-details-marker{display:none}
 details.adv-filters > summary:hover{background:rgba(79,70,229,.05)}
 details.adv-filters > summary::after{
   content:'▾';color:var(--primary);font-size:.9rem;
   transition:transform .25s;
 }
 details.adv-filters[open] > summary::after{ transform:rotate(180deg); }
 details.adv-filters > summary span.adv-icon{
   display:inline-flex;align-items:center;justify-content:center;
   width:32px;height:32px;border-radius:9px;margin-right:.6rem;
   background:linear-gradient(135deg,var(--primary),#7c3aed);color:#fff;
   font-size:.95rem;box-shadow:0 3px 8px rgba(79,70,229,.25);
 }

 .adv-body{padding:1.2rem 1.4rem 1.5rem;display:grid;gap:1.1rem}

 .filter-section{
   background:var(--surface);border-radius:12px;
   border:1px solid var(--border);padding:1rem 1.15rem;
   box-shadow:var(--shadow-sm);
 }
 .filter-section-title{
   display:flex;align-items:center;gap:.55rem;
   font-size:.78rem;font-weight:800;color:var(--text-muted);
   text-transform:uppercase;letter-spacing:.7px;
   margin-bottom:.85rem;padding-bottom:.6rem;
   border-bottom:1px solid var(--border);
 }
 .filter-section-title-icon{
   display:inline-flex;align-items:center;justify-content:center;
   width:24px;height:24px;border-radius:7px;
   background:var(--primary-light);color:var(--primary);
   font-size:.78rem;
 }
 .filter-grid{
   display:grid;grid-template-columns:repeat(3,1fr);gap:.85rem;
 }
 @media (max-width:680px){ .filter-grid{ grid-template-columns:1fr; } }

 .filter-grid label{
   display:flex;flex-direction:column;gap:.32rem;
   font-size:.78rem;font-weight:600;color:var(--text-muted);
   letter-spacing:.2px;
 }
 .filter-grid label select,
 .filter-grid label input{
   width:100%;padding:9px 12px;font-size:.9rem;font-weight:500;
   border:1px solid var(--border);border-radius:9px;
   background:var(--bg);color:var(--text);
   transition:border-color .15s, box-shadow .15s, background .15s;
 }
 .filter-grid label select:hover,
 .filter-grid label input:hover{ background:var(--surface); }
 .filter-grid label select:focus,
 .filter-grid label input:focus{
   outline:none;border-color:var(--primary);
   box-shadow:0 0 0 3px rgba(79,70,229,.12);
 }
 .filter-grid .full{ grid-column:1/-1; }

 .filter-check{
   display:flex;align-items:center;gap:.55rem;
   padding:.55rem .8rem;border-radius:9px;
   border:1px solid var(--border);background:var(--bg);
   cursor:pointer;transition:all .15s;font-size:.88rem;font-weight:500;
 }
 .filter-check:hover{ border-color:var(--primary);background:var(--primary-light); }
 .filter-check input[type=checkbox]{
   width:16px;height:16px;accent-color:var(--primary);cursor:pointer;
 }
 .filter-check:has(input:checked){
   border-color:var(--primary);background:var(--primary-light);color:var(--primary);font-weight:700;
 }

 /* Priority chips (1st/2nd/3rd) — compact horizontal */
 .priority-row{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
 .priority-row > div{flex:1;min-width:160px;display:flex;flex-direction:column;gap:.25rem}
 .priority-label{
   display:inline-block;font-size:.7rem;font-weight:800;letter-spacing:.5px;
   text-transform:uppercase;color:var(--primary);
   background:var(--primary-light);padding:2px 8px;border-radius:6px;align-self:flex-start;
 }

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
    """Clear old jobs AND drop the 'has uploaded this session' flag, so the
    home page goes back to the empty CTA. The user must re-upload the active
    résumé to trigger a fresh Chromium scrape — we don't auto-run any API
    source here. Chromium-only is intentional.
    """
    _clear_jobs()
    _SESSION_HAS_UPLOAD["v"] = False


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


_HOW_IT_WORKS = [
    ("📄", "Upload your résumé",     "PDF or DOCX • parsed in seconds"),
    ("🎯", "Pick filters",            "Location, experience, company type"),
    ("🚀", "We scan 108 platforms",   "Naukri, LinkedIn, Razorpay, Google…"),
    ("✨", "Ranked matches",          "Scored by skill overlap, live-streamed"),
]


def _how_it_works_html() -> str:
    steps = "".join(
        f"<div class='how-step'>"
        f"<div class='how-step-num'>{html.escape(emoji)}</div>"
        f"<div><div class='how-step-text'>{html.escape(t)}</div>"
        f"<div class='how-step-sub'>{html.escape(sub)}</div></div></div>"
        for emoji, t, sub in _HOW_IT_WORKS
    )
    return f"<div class='how-flow'>{steps}</div>"


def _featured_match_html(jobs: list[dict]) -> str:
    """Premium hero card showing the single best matching job above the table.
    Only renders when there's at least one job with score >= 60."""
    if not jobs:
        return ""
    top = max(jobs, key=lambda j: int(j.get("score") or 0))
    score = int(top.get("score") or 0)
    if score < 60:
        return ""
    url = html.escape(top.get("url") or "#")
    title = html.escape((top.get("title") or "Untitled")[:90])
    company = html.escape(top.get("company") or "")
    location = html.escape(top.get("location") or "Remote / Anywhere")
    source = html.escape(top.get("source") or "")
    return f"""
<div class="featured-match reveal">
  <div class="featured-score-label">Top match</div>
  <div class="featured-score">{score}</div>
  <span class="featured-label">⭐ Your strongest match</span>
  <h2 class="featured-title">{title}</h2>
  <div class="featured-company">{('at <b>' + company + '</b>' if company else '')} {('· via ' + source if source else '')}</div>
  <div class="featured-meta">
    <span>📍 {location}</span>
    <span>🎯 {score}/100 skill match</span>
  </div>
  <a class="featured-cta" href="{url}" target="_blank" rel="noopener">View this job →</a>
</div>"""


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
    """Color-coded gradient pill: green ≥70 (with pulse glow on ≥85), yellow 40-69, red <40."""
    if score is None:
        return "<span style='color:#888'>-</span>"
    s = int(score)
    if s >= 70:    bg = "linear-gradient(135deg,#10b981,#059669)"
    elif s >= 40:  bg = "linear-gradient(135deg,#f59e0b,#d97706)"
    else:          bg = "linear-gradient(135deg,#ef4444,#dc2626)"
    cls = " score-glow" if s >= 85 else ""
    return (f"<span class='{cls.strip()}' style='background:{bg};color:#fff;padding:6px 14px;"
            f"border-radius:999px;font-weight:800;font-size:.95rem;display:inline-block;"
            f"min-width:46px;text-align:center;box-shadow:0 2px 6px rgba(0,0,0,.12)'>{s}</span>")


_SOURCE_COLORS = {
    "naukri": "#f97316", "linkedin": "#0a66c2", "indeed": "#003a9b",
    "wellfound": "#ec4899", "foundit": "#a855f7", "shine": "#06b6d4",
    "glassdoor": "#0caa41", "apna": "#16a34a", "internshala": "#3b82f6",
    "jobsforher": "#db2777", "workindia": "#f59e0b", "hirist": "#1d4ed8",
    "cutshort": "#7c3aed", "instahyre": "#9333ea", "timesjobs": "#dc2626",
    "razorpay": "#0c2074", "zomato": "#e23744", "phonepe": "#5f259f",
    "cred": "#000000", "meesho": "#9333ea", "ola": "#84cc16",
    "paytm": "#012a72", "freshworks": "#1f8b4c", "zerodha": "#387ed1",
    "groww": "#00b386", "postman": "#ef5b25", "zoho": "#cc2640",
    "swiggy": "#fc8019", "flipkart": "#2874f0",
    "google": "#4285f4", "microsoft": "#00a4ef", "amazon": "#ff9900",
    "meta": "#0668e1", "adobe": "#fa0f00", "ibm": "#1f70c1",
    "nvidia": "#76b900", "apple": "#0a0a0a", "oracle": "#c74634",
    "salesforce": "#00a1e0", "sap": "#0070ad", "cisco": "#005073",
    "intel": "#0071c5",
    "tcs": "#e60000", "infosys": "#007cc3", "wipro": "#341e62",
    "hcltech": "#0070b8", "techmahindra": "#e31837", "cognizant": "#005eb8",
    "capgemini": "#0070ad", "ltimindtree": "#7e1f86",
}


def _source_pill(source: str | None) -> str:
    if not source:
        return ""
    bg = _SOURCE_COLORS.get(source.lower(), "#6366f1")
    return (f"<span style='background:{bg};color:#fff;padding:3px 10px;"
            f"border-radius:6px;font-size:.72rem;font-weight:700;"
            f"text-transform:lowercase;letter-spacing:.3px;"
            f"box-shadow:0 1px 3px rgba(0,0,0,.15)'>{html.escape(source)}</span>")


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
        score = int(j.get("score") or 0)
        is_hot = score >= 85
        url = j.get("url") or ""
        title = str(j.get("title") or "")
        desc_short = (j.get("description") or "")[:240]
        raw = j.get("raw") or {}
        posted = (raw.get("created") or "")[:10] if raw.get("created") else (raw.get("posted_text") or "-")
        source = j.get("source") or raw.get("site") or ""
        company = str(j.get("company") or "")

        title_html = (
            f"<a href='{html.escape(url)}' target='_blank' rel='noopener' "
            f"title='{html.escape(desc_short)}' style='font-weight:600;color:var(--text)'>"
            f"{html.escape(title)}</a>"
            if url else html.escape(title)
        )
        # Hot match badge gets a fire emoji prefix on the title
        if is_hot:
            title_html = f"<span style='color:#ec4899;font-weight:800;font-size:.7rem;background:rgba(236,72,153,.12);padding:2px 7px;border-radius:4px;margin-right:6px;vertical-align:middle'>🔥 HOT</span>" + title_html

        apply_html = (
            f"<a href='{html.escape(url)}' target='_blank' rel='noopener' "
            f"style='background:linear-gradient(135deg,var(--primary),#7c3aed);color:#fff;"
            f"padding:5px 12px;border-radius:6px;font-size:.8rem;font-weight:700;"
            f"text-decoration:none;display:inline-block;box-shadow:0 2px 6px rgba(79,70,229,.3);"
            f"transition:transform .15s'>Apply →</a>"
            if url else ""
        )

        row_cls = " class='hot-row'" if is_hot else ""
        rows += (f"<tr{row_cls}>"
                 f"<td style='font-weight:700;color:var(--text-muted)'>{i}</td>"
                 f"<td>{_score_badge(j.get('score'))}</td>"
                 f"<td>{title_html}</td>"
                 f"<td style='font-weight:600'>{html.escape(company)}</td>"
                 f"<td class='muted' style='font-size:.84rem'>{html.escape(str(j.get('location') or ''))}</td>"
                 f"<td class='muted' style='font-size:.82rem'>{html.escape(str(posted))}</td>"
                 f"<td>{_source_pill(source)}</td>"
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
    # Hide the stat strip entirely until the user uploads in this session —
    # gated on the same flag as the job tables so the empty CTA is the only
    # thing on the page until upload.
    stats_strip = _stats_strip(active_jobs, active_count) if (active and _SESSION_HAS_UPLOAD["v"]) else ""
    marquee_html = ""  # placeholder; the marquee renders below the hero always — let HTML have it

    # ── Job-list block: only rendered when (a) there's an active résumé AND
    #    (b) the user has clicked Upload in this server session. If either is
    #    false, the two job cards are REPLACED by the empty CTA. This is the
    #    explicit ask: 'show job list only when I upload the résumé'.
    show_jobs = active and _SESSION_HAS_UPLOAD["v"]
    if show_jobs:
        clear_btn = (f"<form method='post' action='/ui/clear' style='display:inline;margin-left:.6rem' "
                     f"onsubmit='return confirm(\"Clear all job results? Your résumé stays. You can re-upload to search again.\")'>"
                     f"<button class='danger' type='submit' style='padding:5px 12px;font-size:.82rem'>🗑 Clear results</button></form>")
        featured = _featured_match_html(active_jobs) if active_jobs else ""
        job_block = f"""
{featured}
<div class="card reveal">
  <h3>
    <span style="display:flex;align-items:center;flex-wrap:wrap"><span class="h3-icon">💼</span>
      <span>Active jobs <span style="background:linear-gradient(135deg,var(--primary),#ec4899);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:transparent;font-weight:800;font-size:1rem">— {active_count} match{('' if active_count == 1 else 'es')}</span></span>
    </span>
    <span>{_limit_dropdown('30')}{clear_btn}</span>
  </h3>
  {_filter_pills_html()}
  <table><thead><tr><th>#</th><th>Score</th><th>Title</th><th>Company</th><th>Location</th><th>Posted</th><th>Source</th><th>Status</th><th>Apply</th></tr></thead>
  <tbody>{active_html}</tbody></table>
</div>

<div class="card reveal">
  <h3>
    <span style="display:flex;align-items:center"><span class="h3-icon" style="background:linear-gradient(135deg,#6b7280,#9ca3af)">📋</span>
      <span>Past / expired jobs <span class="muted" style="font-weight:500">— {past_count} (posted &gt; {_EXPIRE_DAYS} days ago)</span></span>
    </span>
  </h3>
  <table><thead><tr><th>#</th><th>Score</th><th>Title</th><th>Company</th><th>Location</th><th>Posted</th><th>Source</th><th>Status</th><th>Apply</th></tr></thead>
  <tbody>{past_html}</tbody></table>
</div>"""
    else:
        job_block = """
<div class="card reveal" style="text-align:center;padding:3.5rem 2rem;background:linear-gradient(135deg,rgba(79,70,229,.06) 0%,rgba(236,72,153,.04) 50%,rgba(6,182,212,.06) 100%);border:2px dashed var(--primary)">
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
    smart_banner = _smart_banner_html(active, active_jobs, active_count) if (active and _SESSION_HAS_UPLOAD["v"]) else ""
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

<div class="card reveal">
  <div class="upload-hero">
    <div>
      <h2 style="margin:0 0 .3rem">Find your next role.</h2>
      <p class="muted" style="font-size:.95rem;margin:0 0 1.2rem;max-width:480px">
        Drop your résumé — we'll scan <b style="color:var(--primary)">108 platforms</b>
        from Naukri to Google in minutes and rank every match by skill overlap.
      </p>
      <form method="post" action="/ui/upload" enctype="multipart/form-data">
    <label class="file-picker" for="resume-file"><span style="font-size:1.1rem">📄</span> Choose résumé</label>
    <input type="file" id="resume-file" name="file" accept=".pdf,.docx,.doc,.txt,.md,.rtf" required
           style="position:absolute;width:1px;height:1px;opacity:0;overflow:hidden"
           onchange="document.getElementById('resume-file-name').textContent = this.files[0] ? this.files[0].name : 'No file chosen'; document.getElementById('resume-file-name').classList.toggle('has-file', !!this.files[0]);">
    <span id="resume-file-name" class="file-name-display">No file chosen</span>
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
    <button class="cta" type="submit">🚀 Upload résumé + Search jobs</button>
    <details class="adv-filters">
      <summary>
        <span style="display:flex;align-items:center">
          <span class="adv-icon">⚙</span>
          Advanced filters
          <span class="muted" style="margin-left:.5rem;font-weight:500;font-size:.83rem">— salary, skills, company stage, sort, and more</span>
        </span>
      </summary>
      <div class="adv-body">

        <!-- 💰 Compensation -->
        <div class="filter-section">
          <div class="filter-section-title">
            <span class="filter-section-title-icon">💰</span> Compensation
          </div>
          <div class="filter-grid">
            <label>Min annual salary (LPA)
              <input name="min_salary" type="number" min="0" step="1" placeholder="e.g. 8">
            </label>
            <label>Min equity (%)
              <input name="min_equity" type="number" min="0" max="100" step="0.1" placeholder="e.g. 0.25">
            </label>
            <label>Sort results by
              <select name="sort_by">
                <option value="score">Best match score</option>
                <option value="recent">Most recent</option>
                <option value="title">Title (A → Z)</option>
              </select>
            </label>
          </div>
        </div>

        <!-- 📅 Schedule & Mode -->
        <div class="filter-section">
          <div class="filter-section-title">
            <span class="filter-section-title-icon">📅</span> Schedule &amp; Work mode
          </div>
          <div class="filter-grid">
            <label>Date posted
              <select name="date_posted">
                <option value="">Any time</option>
                <option value="1">Last 24 hours</option>
                <option value="3">Last 3 days</option>
                <option value="7">Last week</option>
                <option value="30">Last month</option>
              </select>
            </label>
            <label>Work mode
              <select name="work_mode">
                <option value="">Any work mode</option>
                <option value="onsite">On-site</option>
                <option value="hybrid">Hybrid</option>
                <option value="remote">Remote only</option>
              </select>
            </label>
            <label>Job type
              <select name="job_type">
                <option value="">Any type</option>
                <option value="fulltime">Full-time</option>
                <option value="parttime">Part-time</option>
                <option value="contract">Contract</option>
                <option value="temporary">Temporary</option>
                <option value="internship">Internship</option>
                <option value="freelance">Freelance</option>
              </select>
            </label>
            <label>Joining date
              <select name="joining_date">
                <option value="">Any</option>
                <option value="immediate">Immediately</option>
                <option value="within_1_month">Within 1 month</option>
                <option value="flexible">Flexible</option>
              </select>
            </label>
            <label>Notice period
              <select name="notice_period">
                <option value="">Any</option>
                <option value="15">15 days</option>
                <option value="30">30 days</option>
                <option value="60">60 days</option>
                <option value="90">90 days</option>
              </select>
            </label>
          </div>
        </div>

        <!-- 🏢 Company -->
        <div class="filter-section">
          <div class="filter-section-title">
            <span class="filter-section-title-icon">🏢</span> Company
          </div>
          <div class="filter-grid">
            <label>Employee count
              <select name="employee_count">
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
            <label>Company stage / Funding
              <select name="company_stage">
                <option value="">Any</option>
                <option value="seed">Seed</option>
                <option value="series-a">Series A</option>
                <option value="series-b">Series B</option>
                <option value="series-c">Series C+</option>
                <option value="public">Public</option>
              </select>
            </label>
            <label>Industry
              <input name="industry" placeholder="e.g. FinTech, HealthTech, SaaS">
            </label>
            <div class="full">
              <div style="font-size:.78rem;font-weight:600;color:var(--text-muted);margin-bottom:.5rem">
                Company priority order
                <span class="muted" style="font-weight:400">— overrides Company size above. 1st-preference career pages get scraped first.</span>
              </div>
              <div class="priority-row">
                <div>
                  <span class="priority-label">1st pick</span>
                  <select name="priority_1">
                    <option value="">— none —</option>
                    <option value="startup">Startup / Unicorn (1–500)</option>
                    <option value="midlevel">Mid-level / IT services (500–50k)</option>
                    <option value="mnc">MNC (50k+)</option>
                  </select>
                </div>
                <div>
                  <span class="priority-label">2nd pick</span>
                  <select name="priority_2">
                    <option value="">— none —</option>
                    <option value="startup">Startup / Unicorn</option>
                    <option value="midlevel">Mid-level / IT services</option>
                    <option value="mnc">MNC</option>
                  </select>
                </div>
                <div>
                  <span class="priority-label">3rd pick</span>
                  <select name="priority_3">
                    <option value="">— none —</option>
                    <option value="startup">Startup / Unicorn</option>
                    <option value="midlevel">Mid-level / IT services</option>
                    <option value="mnc">MNC</option>
                  </select>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- ✅ Skills & Preferences -->
        <div class="filter-section">
          <div class="filter-section-title">
            <span class="filter-section-title-icon">✅</span> Skills &amp; Preferences
          </div>
          <div class="filter-grid">
            <label class="full">Required skills <span class="muted" style="font-weight:400">(comma-separated — jobs without these get filtered out)</span>
              <input name="required_skills" placeholder="e.g. python, aws, react, kubernetes">
            </label>
            <label class="filter-check">
              <input type="checkbox" name="visa_sponsorship" value="1"> 🛂 Visa sponsorship offered
            </label>
            <label class="filter-check">
              <input type="checkbox" name="jobs_for_women" value="1"> 👩 Jobs for women
            </label>
          </div>
        </div>

        <p class="muted" style="margin:0;font-size:.78rem;text-align:center">
          🔌 Date posted, Work mode, Job type, and Experience are injected into
          <b>Naukri / LinkedIn / Indeed / Foundit / Glassdoor</b> URLs.
          Salary, skills, and sort are applied to the displayed result list after scrape.
        </p>
      </div>
    </details>
      </form>
    </div>
    <div>
      {_how_it_works_html()}
    </div>
  </div>
  <p class="muted" style="margin-top:1.4rem;padding-top:1.2rem;border-top:1px solid var(--border)">Uploading opens visible Chromium windows that walk <b>up to ~108 platforms</b>: 15 job boards (Naukri, LinkedIn, Indeed, Foundit, Shine, Glassdoor, Apna, Internshala, JobsForHer, WorkIndia, Hirist, Cutshort, Instahyre, TimesJobs, Wellfound) + 46 Indian startups/unicorns (Razorpay, Zomato, PhonePe, Cred, Meesho, BYJU's, Unacademy, Dream11, MakeMyTrip, Nykaa, BharatPe, Urban Company, Lenskart, OYO, Acko, Practo, PharmEasy, etc.) + 15 IT services (TCS, Infosys, Wipro, HCL, Tech Mahindra, Cognizant, Capgemini, LTIMindtree, etc.) + 32 MNCs (Google, Microsoft, Amazon, Meta, Apple, Oracle, Salesforce, SAP, Cisco, Intel, Adobe, IBM, Nvidia, Atlassian, ServiceNow, Snowflake, Databricks, Uber, Netflix, JPMorgan, Goldman, Citi, Deloitte, Accenture, etc.). <b>Location</b> is injected into the URL of every site that supports it. <b>Company size</b> picks which career pages are scraped — Startup (~61 sites), Mid-level (~30 sites), MNC (~47 sites), or blank for all ~108. Boards always run.</p>
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

// ----- IntersectionObserver: cards fade-and-rise on scroll into view -----
(function(){{
  const els = document.querySelectorAll('.reveal');
  if (!('IntersectionObserver' in window)){{ els.forEach(e=>e.classList.add('in')); return; }}
  const obs = new IntersectionObserver((entries)=>{{
    entries.forEach(en=>{{
      if (en.isIntersecting){{ en.target.classList.add('in'); obs.unobserve(en.target); }}
    }});
  }}, {{rootMargin:'0px 0px -40px 0px', threshold:.05}});
  els.forEach(e=>obs.observe(e));
  // Above-the-fold elements: reveal immediately so they don't pop-in awkwardly
  setTimeout(()=>{{
    document.querySelectorAll('.reveal').forEach((e,i)=>{{
      const r = e.getBoundingClientRect();
      if (r.top < window.innerHeight) {{ setTimeout(()=>e.classList.add('in'), i*60); }}
    }});
  }}, 30);
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
    # Flip the session flag so the home page now renders the job tables instead
    # of the empty CTA. The flag stays True until the user clicks Clear results,
    # Remove résumé, switches résumé, or the server restarts.
    _SESSION_HAS_UPLOAD["v"] = True
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


@router.post("/ui/clear")
def ui_clear():
    """Manually wipe all non-applied jobs from the table without removing the
    active résumé — and drop the session flag so the home page renders the
    empty CTA again."""
    _clear_jobs()
    _SESSION_HAS_UPLOAD["v"] = False
    return RedirectResponse("/", status_code=303)


@router.post("/ui/status")
def ui_status(job_id: str = Form(...), status: str = Form(...)):
    """Update a job's status from the per-row dropdown (Saved / Applied / Interviewing / …)."""
    if status not in _VALID_STATUSES:
        return RedirectResponse("/", status_code=303)
    get_supabase().table("jobs").update({"status": status}).eq("id", job_id).execute()
    return RedirectResponse("/", status_code=303)


