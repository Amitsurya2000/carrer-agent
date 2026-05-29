# Production image for cloud deploys (Render, Railway, Fly.io, etc.).
#
# Built on Microsoft's official Playwright-Python image which ships Chromium,
# all required Linux libs, AND the playwright Python package preinstalled.
#
# We forcibly use `python3` everywhere — pip install, build-time checks,
# and the runtime CMD — and we PRINT diagnostic info at build time so any
# mismatch between the build-time python and the runtime python is visible
# in the build logs instead of silently breaking with ModuleNotFoundError.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HEADLESS_BROWSER=1

WORKDIR /app

# ── Step 1: dump the python landscape so logs reveal which interpreter is which ──
RUN echo "==== PYTHON LANDSCAPE BEFORE INSTALL ====" && \
    echo "which python3:  $(which python3)" && \
    echo "which python:   $(which python || echo none)" && \
    echo "which pip3:     $(which pip3 || echo none)" && \
    python3 --version && \
    python3 -c "import sys; print('  sys.executable:', sys.executable)" && \
    python3 -c "import playwright; print('  pre-install playwright location:', playwright.__file__)" || echo "  playwright NOT importable pre-install"

# ── Step 2: install our deps + (re-)install playwright into the SAME python3 ──
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt && \
    python3 -m pip install --no-cache-dir playwright==1.49.1

# ── Step 3: verify playwright is importable by the SAME python3 that CMD will use ──
RUN echo "==== POST-INSTALL VERIFICATION ====" && \
    python3 -c "import playwright; print('  post-install playwright location:', playwright.__file__)" && \
    python3 -c "from playwright.sync_api import sync_playwright; print('  Playwright IMPORT OK with the build python3 — runtime CMD uses the same python3')"

COPY app ./app
COPY supabase ./supabase

ENV PORT=10000
EXPOSE 10000

# CMD uses `python3 -m uvicorn` so sys.executable in FastAPI is GUARANTEED
# to be the same python3 that we just verified has playwright. Any subprocess
# launched via subprocess.Popen([sys.executable, ...]) inherits it correctly.
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "10000"]
