# Backend image for the AI Career Agent (FastAPI: discovery, scoring, API).
#
# NOT included: the attended apply tool (app/apply) — that's a local, human-driven
# Playwright flow with a visible browser and has no place on a headless server.
# Its dependency lives in requirements-apply.txt, which this image deliberately skips.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first so this layer caches unless requirements change.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# App code + the SQL schema (handy to have in the image for reference).
COPY app ./app
COPY supabase ./supabase

# Secrets are NOT baked in — Render/Railway inject them as real env vars at runtime,
# and pydantic-settings reads them from the environment (no .env file needed in prod).
ENV PORT=8000
EXPOSE 8000

# Shell form so ${PORT} (set by the host platform) expands at runtime.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
