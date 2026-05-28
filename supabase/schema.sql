-- AI Career Agent - initial schema (Phase 0)
-- Run this in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.

create extension if not exists "pgcrypto";

-- Your single profile row: the source of truth the agent tailors FROM.
create table if not exists profile (
    id               uuid primary key default gen_random_uuid(),
    full_name        text not null,
    email            text,
    headline         text,
    skills           text[] default '{}',
    years_experience numeric,
    base_resume      text,          -- your master resume (plain text or markdown)
    label            text,          -- human label, e.g. the uploaded filename
    is_active        boolean not null default false,  -- exactly one active at a time
    created_at       timestamptz not null default now()
);

-- Discovered jobs flow in here; status + score get filled by later phases.
create table if not exists jobs (
    id           uuid primary key default gen_random_uuid(),
    source       text not null,            -- e.g. 'remoteok', 'remotive'
    url          text not null unique,     -- dedupe key
    title        text not null,
    company      text,
    location     text,
    description  text,
    status       text not null default 'discovered',
        -- discovered -> scored -> tailored -> approved -> applied / rejected
    score        int,                      -- 0-100 fit, set in Phase 2
    score_reason text,
    raw          jsonb,                    -- original payload from the source
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create index if not exists jobs_status_idx on jobs (status);
create index if not exists jobs_score_idx  on jobs (score desc);
