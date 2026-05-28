# AI Career Agent

An AI agent that discovers jobs, scores them against your profile, drafts **truthful**
tailored resumes + cover letters, and surfaces them for one-click **assisted** application.
You stay on the submit button — no mass auto-apply, no fabricated experience.

## Stack (lean by design)
- **Backend / orchestrator:** Python + FastAPI
- **Agent brain:** Claude (Anthropic API)
- **Structured storage:** Supabase (Postgres)
- **Vector memory (RAG):** ChromaDB — added in Phase 4
- **Frontend:** Next.js on Vercel — added in Phase 5
- **Scraping:** only where a source has no API — added as needed

## Phase status
- [x] **Phase 0 — Foundations:** repo, venv, config, Supabase schema
- [x] **Phase 1 — Discovery:** Adzuna API (India), upsert + dedupe. Runs once Adzuna keys are set.
- [x] **Phase 2 — Scoring:** Claude rates each job 0–100 + reason; prompt-cached profile. Needs `ANTHROPIC_API_KEY` + a profile row.
- [ ] Phase 3 — Tailoring + ATS match
- [ ] Phase 4 — RAG memory
- [~] Phase 5 — Assisted apply: attended Playwright form-filler built (CLI). Review dashboard still TODO.
- [ ] Phase 6 — Orchestration + notifications
- [ ] Phase 7 — Deploy

## Setup
1. **Install deps** (done in Phase 0). Use Python 3.11 — see DECISIONS.md for why:
   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
2. **Create the database.** Make a project at https://supabase.com, then open
   **SQL Editor -> New query**, paste `supabase/schema.sql`, and run it.
3. **Add secrets.** Copy `.env.template` to `.env` and fill in:
   - `ANTHROPIC_API_KEY` — https://console.anthropic.com
   - `SUPABASE_URL` + `SUPABASE_KEY` — Supabase dashboard -> Settings -> API
     (use the **service_role** key for the backend; keep it secret)
4. **Run the API:**
   ```powershell
   uvicorn app.main:app --reload
   ```
   Then open **http://127.0.0.1:8000** in a browser — that's the **web app**. Upload a
   résumé (PDF/DOCX/TXT) and it **automatically searches for jobs matching that résumé**
   (search terms derived from its job titles + skills — works for any field, tech or not)
   and scores them, free. Keep several résumés and click **Use this** to switch which one
   drives the search. The page shows **"Searching jobs for: …"** so you see what it detected.
   (Health: `/health`; API docs: `/docs`.)
5. **Run discovery** (Phase 1) — needs `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` in `.env`:
   ```powershell
   python -m app.discover          # one-off run from the CLI
   ```
   or `POST http://127.0.0.1:8000/discover` while the server is running. Edit the
   `QUERIES` list in `app/discover.py` to change which roles it hunts.
6. **Optional — upgrade scoring to Claude** instead of the free keyword scorer. Add
   `ANTHROPIC_API_KEY` to `.env`, then trigger `POST /score` or `python -m app.score`.
   Default model is `claude-opus-4-7` (set in `app/llm.py`); switch to
   `claude-sonnet-4-6` / `claude-haiku-4-5` to cut cost.
7. **Assisted apply** (Phase 5, attended) — copy `apply_profile.example.json` to
   `apply_profile.json`, fill it in (and set `resume_path`). Playwright is a separate,
   local-only dependency (not in the server image):
   ```powershell
   pip install -r requirements-apply.txt
   python -m playwright install chromium
   python -m app.apply.filler "https://careers.example.com/job/123/apply"
   ```
   A visible browser opens. You log in / reach the form, press Enter; it fills what it
   can, highlights it, and **stops at submit**. You review and click submit yourself.
   It never logs in for you and never submits. Field matching is heuristic — always check.

## Deploy (Phase 7 — scaffolding ready, not yet deployed)
The backend ships as a Docker image (`Dockerfile`) that runs only the FastAPI server —
the attended apply tool is intentionally excluded (headless server, no browser).

- **Build/run locally:**
  ```powershell
  docker build -t career-agent .
  docker run -p 8000:8000 --env-file .env career-agent
  ```
- **Render:** push this repo to GitHub, then Render → New → Blueprint → select the repo.
  `render.yaml` defines a Docker web service with a `/health` check; set the 5 secrets in
  the dashboard when prompted (they're `sync:false`, never committed). Railway works too
  (it auto-detects the Dockerfile).
- Secrets are **never** baked into the image — they're injected as env vars at runtime.

See `DECISIONS.md` for why things are the way they are.
