# Production image for cloud deploys (Render, Railway, Fly.io, etc.).
#
# Uses Microsoft's official Playwright-Python image which ships with Chromium,
# every required Linux shared library, AND the playwright Python package
# pre-installed. The 108-site scrape runs out of the box on Render.
#
# HEADLESS_BROWSER=1 is baked in so the scrapers know they're in the cloud and
# launch Chromium with container-safe flags (no --start-maximized).
#
# We use `python3 -m ...` everywhere — pip install, build-time verification, and
# the runtime CMD — so the SAME python3 interpreter is used by uvicorn AND by
# every subprocess. Otherwise `sys.executable` in FastAPI ends up pointing to a
# python without playwright and the scrape fails with ModuleNotFoundError.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HEADLESS_BROWSER=1

WORKDIR /app

COPY requirements.txt .

# Install app deps using python3 explicitly. The base image already has
# playwright + chromium, so we re-pin it here defensively and verify the SAME
# python3 sees it — if it doesn't, the build fails loudly here instead of
# silently breaking at scrape time.
RUN python3 -m pip install --no-cache-dir -r requirements.txt \
 && python3 -m pip install --no-cache-dir playwright==1.49.1 \
 && python3 -c "from playwright.sync_api import sync_playwright; print('Playwright OK at build time')"

COPY app ./app
COPY supabase ./supabase

ENV PORT=10000
EXPOSE 10000

# python3 -m uvicorn so sys.executable in the FastAPI process matches the
# python3 the subprocess scrape relies on.
CMD ["sh", "-c", "python3 -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
