"""Phase 2 - scoring. Claude rates each discovered job's fit (0-100) + a one-line reason.

Run:  python -m app.score      (scores jobs with status='discovered')
or:   POST /score
Needs ANTHROPIC_API_KEY, a résumé uploaded via the web app (which writes to the
`profile` table), and jobs already discovered.
"""
from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field

from app.db import get_supabase
from app.llm import MODEL, get_anthropic
from app.profile_store import get_active

SCORING_INSTRUCTIONS = """You are a careful technical recruiter scoring how well a candidate \
fits a specific job posting. Use this scale:
- 85-100: strong fit (core skills, domain, and seniority all align)
- 60-84: solid fit with some gaps
- 30-59: weak fit, major gaps
- 0-29: not a fit / wrong field
Judge on genuine overlap of skills, domain, and level. Do not inflate scores. Keep the
reason to one sentence."""


class JobScore(BaseModel):
    score: int = Field(description="Fit score from 0 to 100")
    reason: str = Field(description="One sentence explaining the score")


def _system_blocks(profile_text: str) -> list[dict]:
    """Instructions + profile are identical for every job in a run, so cache them.
    (Prompt caching only kicks in above the model's min prefix; harmless if it doesn't.)"""
    return [
        {"type": "text", "text": SCORING_INSTRUCTIONS},
        {
            "type": "text",
            "text": f"CANDIDATE PROFILE:\n{profile_text}",
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _format_jd(job: dict) -> str:
    lines = [
        f"Title: {job.get('title')}",
        f"Company: {job.get('company')}",
        f"Location: {job.get('location')}",
        f"Description:\n{job.get('description')}",
    ]
    return "\n".join(line for line in lines if line.rsplit(": ", 1)[-1] not in ("None", ""))


def _parse_loose(text: str) -> JobScore:
    """Fallback if structured output isn't available: pull score + reason from text."""
    try:
        data = json.loads(text)
        return JobScore(score=int(data["score"]), reason=str(data.get("reason", "")))
    except Exception:
        m = re.search(r"\b(\d{1,3})\b", text)
        return JobScore(score=int(m.group(1)) if m else 0, reason=text.strip()[:200])


def score_job(client, profile_text: str, job: dict) -> JobScore:
    """Score one job. Takes the client + data so it's easy to test in isolation."""
    system = _system_blocks(profile_text)
    messages = [{"role": "user", "content": f"JOB POSTING:\n{_format_jd(job)}"}]

    if hasattr(client.messages, "parse"):
        resp = client.messages.parse(
            model=MODEL, max_tokens=400, system=system,
            messages=messages, output_format=JobScore,
        )
        result = resp.parsed_output
    else:  # older SDK: ask for JSON and parse it ourselves
        resp = client.messages.create(
            model=MODEL, max_tokens=400, system=system,
            messages=[{"role": "user", "content": messages[0]["content"]
                       + '\n\nReply ONLY with JSON: {"score": <0-100>, "reason": "<one sentence>"}'}],
        )
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        result = _parse_loose(text)

    result.score = max(0, min(100, int(result.score)))  # schema can't bound numbers
    return result


def get_profile_text() -> str:
    p = get_active()
    if not p:
        raise RuntimeError("No résumé yet — upload one in the web app at http://127.0.0.1:8000.")
    lines = [
        f"Name: {p.get('full_name')}",
        f"Headline: {p.get('headline')}",
        f"Years of experience: {p.get('years_experience')}",
        f"Skills: {', '.join(p.get('skills') or [])}",
        f"Resume:\n{p.get('base_resume')}",
    ]
    return "\n".join(line for line in lines if line.rsplit(": ", 1)[-1] not in ("None", ""))


# --- Free keyword scoring (no API) -------------------------------------------------

def keyword_score(skills: list[str], job: dict) -> tuple[int, str]:
    """Cheap, API-free fit score: how many of your skills appear in the posting."""
    text = (job.get("title", "") + " " + (job.get("description") or "")).lower()
    hits = sorted({s for s in (skills or []) if s and s.lower() in text})
    score = min(98, 15 + len(hits) * 15)
    reason = (f"Matches {len(hits)} skills: {', '.join(hits[:5])}"
              if hits else "No clear skill overlap")
    return score, reason


def score_pending_keyword(limit: int = 1000) -> dict:
    """(Re)score jobs with the free keyword scorer using the active résumé.

    Re-scores everything not yet applied to, so switching résumés re-ranks the list."""
    p = get_active()
    if not p:
        raise RuntimeError("No résumé yet — upload one in the web app first.")
    skills = p.get("skills") or []
    sb = get_supabase()
    jobs = sb.table("jobs").select("*").neq("status", "applied").limit(limit).execute().data
    for job in jobs:
        s, reason = keyword_score(skills, job)
        sb.table("jobs").update(
            {"score": s, "score_reason": reason, "status": "scored"}
        ).eq("id", job["id"]).execute()
    return {"scored": len(jobs)}


def score_pending(limit: int = 25) -> dict:
    """Score the most recent jobs still in 'discovered' state; write score back."""
    profile_text = get_profile_text()
    client = get_anthropic()
    sb = get_supabase()
    res = (
        sb.table("jobs").select("*").eq("status", "discovered")
        .order("created_at", desc=True).limit(limit).execute()
    )
    jobs = res.data
    for job in jobs:
        s = score_job(client, profile_text, job)
        sb.table("jobs").update(
            {"score": s.score, "score_reason": s.reason, "status": "scored"}
        ).eq("id", job["id"]).execute()
    return {"pending": len(jobs), "scored": len(jobs)}


if __name__ == "__main__":
    print(score_pending())
