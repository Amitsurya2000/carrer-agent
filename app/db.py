"""Supabase client. Created lazily so the app imports even before .env is filled."""
from functools import lru_cache

from supabase import Client, create_client

from app.config import settings


@lru_cache
def get_supabase() -> Client:
    if not settings.is_configured:
        raise RuntimeError(
            "Supabase not configured — set SUPABASE_URL and SUPABASE_KEY in .env"
        )
    return create_client(settings.supabase_url, settings.supabase_key)
