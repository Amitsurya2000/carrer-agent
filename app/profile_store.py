"""Résumé (profile) storage: keep many résumés, exactly one marked active.

Requires the `profile` table to have `label text` and `is_active boolean` columns
(added by the Phase-2.5 migration).
"""
from __future__ import annotations

from typing import Optional

from app.db import get_supabase
from app.resume import extract_keywords

_ALL = "00000000-0000-0000-0000-000000000000"  # sentinel so update/delete touch every row


def _deactivate_all(sb) -> None:
    sb.table("profile").update({"is_active": False}).neq("id", _ALL).execute()


def upload_profile(label: str, text: str) -> dict:
    """Save a new résumé from its extracted text and make it the active one."""
    sb = get_supabase()
    _deactivate_all(sb)
    row = {
        "full_name": label or "Uploaded resume",
        "label": label or "resume",
        "skills": extract_keywords(text),
        "base_resume": text,
        "is_active": True,
    }
    return sb.table("profile").insert(row).execute().data[0]


def list_profiles() -> list[dict]:
    sb = get_supabase()
    return (
        sb.table("profile")
        .select("id,label,is_active,skills,created_at")
        .order("created_at", desc=True)
        .execute()
        .data
    )


def set_active(profile_id: str) -> None:
    sb = get_supabase()
    _deactivate_all(sb)
    sb.table("profile").update({"is_active": True}).eq("id", profile_id).execute()


def get_active() -> Optional[dict]:
    """The active résumé, or None if none is selected (→ UI shows empty job lists)."""
    res = get_supabase().table("profile").select("*").eq("is_active", True).limit(1).execute()
    return res.data[0] if res.data else None
