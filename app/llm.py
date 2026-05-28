"""Anthropic (Claude) client — used from Phase 2 (scoring) onward."""
from functools import lru_cache

from anthropic import Anthropic

from app.config import settings

# Default model for scoring/tailoring. Opus 4.7 is the most capable.
# Scoring runs over many jobs, so cost adds up: to cut it, switch this one line to
# "claude-sonnet-4-6" (~half the price) or "claude-haiku-4-5" (cheapest). Your call.
MODEL = "claude-opus-4-7"


@lru_cache
def get_anthropic() -> Anthropic:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing in .env")
    return Anthropic(api_key=settings.anthropic_api_key)
