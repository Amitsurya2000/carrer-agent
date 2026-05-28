"""Pydantic models mirroring the Supabase tables (see supabase/schema.sql)."""
from typing import Optional

from pydantic import BaseModel


class Profile(BaseModel):
    id: Optional[str] = None
    full_name: str
    email: Optional[str] = None
    headline: Optional[str] = None
    skills: list[str] = []
    years_experience: Optional[float] = None
    base_resume: Optional[str] = None  # master resume the agent tailors FROM


class Job(BaseModel):
    id: Optional[str] = None
    source: str
    url: str
    title: str
    company: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    status: str = "discovered"  # discovered -> scored -> tailored -> approved -> applied/rejected
    score: Optional[int] = None  # 0-100 fit, set in Phase 2
    score_reason: Optional[str] = None
