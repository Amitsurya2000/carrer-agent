# Production image for cloud deploys (Render, Railway, Fly.io, etc.).
#
# Uses Microsoft's official Playwright-Python image which ships with Chromium
# AND every Linux shared library Chromium needs (libnss3, libxss1, etc.) — so
# the 108-site scrape runs out of the box.
#
# Runs HEADLESS in the cloud (no display server). The visible-window flow
# stays a local-only feature; HEADLESS_BROWSER=1 makes the scrapers skip the
# `--start-maximized` flag and use container-safe Chromium switches.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HEADLESS_BROWSER=1

WORKDIR /app

# Install Python deps first so this layer caches unless requirements change.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# App code + the SQL schema (handy to have in the image for reference).
COPY app ./app
COPY supabase ./supabase

# Secrets are NOT baked in — Render/Railway inject them as real env vars
# at runtime, and pydantic-settings reads them straight from the environment.
ENV PORT=10000
EXPOSE 10000

# Shell form so ${PORT} (set by the host platform) expands at runtime.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
