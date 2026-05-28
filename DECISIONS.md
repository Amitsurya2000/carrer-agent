# Decisions log

Running record of architecture choices so the project — and the AI helping build it —
stays oriented across sessions.

## 2026-05-27 — Phase 0
- **LLM provider:** Claude (Anthropic). Strongest at "tailor truthfully, never fabricate."
  Default model `claude-sonnet-4-6` for scoring; may bump to Opus for final tailoring.
- **Storage:** Supabase (managed Postgres). Tables: `profile` (one row, source of truth)
  and `jobs` (discovered listings + status + score).
- **Vectors:** ChromaDB, deferred to Phase 4. Local, zero-setup; swap for Pinecone only if scale demands.
- **Apply step:** ASSISTED, not auto. The agent prepares everything; a human reviews and
  clicks submit. No CAPTCHA-defeating, no proxy rotation, no mass spam, no fabricated
  experience. Rationale: principle + protects real accounts (esp. LinkedIn) + higher-quality
  applications actually convert better.
- **Hosting (planned):** backend on Render/Railway (long-running Python), frontend on Vercel.
- **Python version:** **3.11** (`py -3.11`). Python 3.14 is installed on this machine but is too
  new — `supabase` -> `storage3` pulls `pyiceberg`/`pyroaring`, which have no 3.14 wheels and fail to
  compile without MSVC. 3.11 has prebuilt wheels for everything, including ChromaDB (Phase 4).
  The venv lives at `.venv/`; always create it with `py -3.11 -m venv .venv`.

## 2026-05-27 — Phase 1 (discovery)
- **First source:** **Adzuna API**, country `in` (India). User wants India-based jobs; Naukri/Indeed
  have no clean public API, and scraping them on day one is the anti-bot fight we chose to avoid.
  Adzuna returns structured JSON with a free key.
- **Roles hunted:** computer vision / ML, AI-LLM, data science — as the editable `QUERIES` list in
  `app/discover.py`.
- **Dedupe:** `jobs.url` is unique; upsert uses `ON CONFLICT (url) DO NOTHING` so re-running discovery
  never overwrites a job's later-phase status/score.
- **Trigger:** `python -m app.discover` (CLI) or `POST /discover` (API).

## 2026-05-27 — Apply agent (Phase 5, lite)
- **Built:** an *attended* form-filler at `app/apply/filler.py` (Playwright, Python).
  Visible browser; the user logs in and reaches the form themselves; it fills mapped
  fields from `apply_profile.json`, highlights them, and STOPS at submit. No automated
  login, no submit, no headless.
- **Declined:** building it on **patchright** / any stealth/anti-detection automation.
  patchright's purpose is evading platform bot-detection — i.e. the detection-evasion
  layer we agreed to leave out. Reasons unchanged: permanent-ban risk on the user's real
  LinkedIn, not demoable, against ToS. Attended Playwright needs no stealth because a
  human is present, so this loses nothing legitimate.
- **Field matching is heuristic** (label/name/placeholder keyword rules in `FIELD_RULES`).
  Verified end-to-end headlessly against a synthetic form. The human always reviews.

## 2026-05-27 — Phase 2 (scoring)
- **Model:** default `claude-opus-4-7` (per current Claude API guidance — most capable; don't
  silently downgrade for cost). `MODEL` in `app/llm.py` is one line; switch to
  `claude-sonnet-4-6` / `claude-haiku-4-5` to cut cost on high-volume scoring.
- **Structured output:** `client.messages.parse(..., output_format=JobScore)` (Pydantic
  `{score:int, reason:str}`), with a plain-JSON fallback for older SDKs. Score clamped 0–100
  in code (JSON-schema can't bound numbers).
- **Prompt caching:** instructions + candidate profile go in the `system` blocks with a
  `cache_control: ephemeral` breakpoint — identical across every job in a run, so the profile
  prefix is cached after the first call. (Only caches above the model's ~4096-token min prefix.)
- **Profile source:** single `profile` row in Supabase, seeded from local `profile.json`
  (gitignored) via `python -m app.seed_profile`. Scoring reads `status='discovered'` jobs,
  writes `score`/`score_reason`, sets `status='scored'`. Trigger: `python -m app.score` or `POST /score`.
- **Verified offline** with a mocked Claude client (parse path, clamp, cache breakpoint, fallback).

## 2026-05-27 — Deploy scaffolding (Phase 7, prepared early on request)
- **Docker image** (`Dockerfile`, python:3.11-slim) runs **only** the FastAPI server. Playwright
  and the attended apply tool are excluded — a headless server has no browser/human, so it has no
  business there. Split deps: `requirements.txt` (core/server) vs `requirements-apply.txt` (local-only Playwright).
- **Secrets never baked into the image** — injected as runtime env vars; pydantic-settings reads
  the environment directly (no `.env` needed in prod). `.dockerignore` keeps `.env`, `.venv`, browser
  data, and local profile JSON out of the build context.
- **`render.yaml`** = Docker web service, `/health` check, 5 `sync:false` secret env vars. Railway
  also works (auto-detects Dockerfile). Not deployed yet — waiting on a verified local run.
