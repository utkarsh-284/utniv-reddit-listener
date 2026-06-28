-- Reddit Listening Engine — Supabase schema (same project as the scorecard, separate tables).
-- All tables are prefixed `reddit_` so they live cleanly alongside the scorecard tables.
-- RLS on; all writes go through the listener using the service-role key (CI secret only).
-- See: business/sales/reddit-listening-engine-plan-2026-06-26.md

create extension if not exists "pgcrypto";

-- Which subreddits we watch + how much self-promo each tolerates (drives a score dim).
create table if not exists reddit_subreddits (
  name             text primary key,            -- e.g. 'agency' (no r/ prefix)
  role             text,                         -- primary | informant | reach | probe
  promo_tolerance  numeric not null default 0.2, -- 0..1
  poll_new         boolean not null default true,
  active           boolean not null default true,
  sort_order       int not null default 100,    -- groups combined feeds: ~10-21 busy, ~30-41 niche
  created_at       timestamptz not null default now()
);

-- Every thread we see. Dedup on reddit_id; re-polls upsert (so velocity tracks as it grows).
-- We store EVERYTHING we fetch+score, even below-threshold, so we can re-tune later.
create table if not exists reddit_threads (
  id              uuid primary key default gen_random_uuid(),
  reddit_id       text unique not null,          -- t3_xxxxx (dedup key)
  subreddit       text not null,
  url             text not null,
  permalink       text,
  title           text,
  body            text,
  author          text,
  created_utc     timestamptz,

  -- deterministic signals (computed in code, free)
  upvotes         int default 0,
  num_comments    int default 0,
  age_hours       numeric,
  velocity        numeric,                       -- upvotes / age_hours
  decay_factor    numeric,                       -- 1.0/0.7/0.3/0.1 by freshness
  engagement      numeric,                       -- comments / max(upvotes,1)
  phrase_hits     int default 0,
  matched_phrases jsonb default '[]'::jsonb,

  -- llm judgment (only set once, on first scoring)
  icp_relevance   int,
  pain_acuteness  int,
  is_question     boolean,
  trigger_type    text,                          -- departure|churn|onboarding|documentation|retrieval|notetaker|none
  suggested_action text,                         -- reply|mine|dm|ignore
  one_line_why    text,
  llm_scored      boolean not null default false,
  llm_model       text,                          -- which model/provider scored it

  -- composite + lifecycle
  composite_score int,
  alerted         boolean not null default false,
  status          text not null default 'new',   -- new | alerted | actioned | ignored

  first_seen_at   timestamptz not null default now(),
  last_seen_at    timestamptz not null default now()
);
create index if not exists reddit_threads_score_idx on reddit_threads (composite_score desc);
create index if not exists reddit_threads_status_idx on reddit_threads (status);
create index if not exists reddit_threads_created_idx on reddit_threads (created_utc desc);

-- Voice-of-customer bank — verbatim quotes worth reusing (feeds copywriting + dossier + scorecard).
create table if not exists reddit_voc (
  id           uuid primary key default gen_random_uuid(),
  thread_id    uuid references reddit_threads (id) on delete cascade,
  quote        text not null,
  theme        text,
  subreddit    text,
  source_url   text,
  used_in      jsonb default '[]'::jsonb,
  captured_at  timestamptz not null default now()
);

-- One row per cron run — observability + cost tracking (engineering standards).
create table if not exists reddit_runs (
  id             uuid primary key default gen_random_uuid(),
  started_at     timestamptz not null default now(),
  finished_at    timestamptz,
  subs_polled    int default 0,
  posts_fetched  int default 0,
  posts_new      int default 0,
  posts_scored   int default 0,     -- how many hit the LLM
  alerts_sent    int default 0,
  llm_calls      int default 0,
  llm_provider   text,              -- openai | nim | none
  errors         jsonb default '[]'::jsonb,
  ok             boolean default true
);

-- v1.1: drafted replies (gated — posted by hand, never auto-posted).
create table if not exists reddit_drafts (
  id                uuid primary key default gen_random_uuid(),
  thread_id         uuid not null references reddit_threads (id) on delete cascade,
  body              text,
  status            text not null default 'drafted',  -- drafted | edited | posted | skipped
  posted_by_hand_at timestamptz,
  created_at        timestamptz not null default now()
);

-- RLS on; service-role key (server/CI only) bypasses it. No anon policies = no public access.
alter table reddit_subreddits enable row level security;
alter table reddit_threads    enable row level security;
alter table reddit_voc        enable row level security;
alter table reddit_runs       enable row level security;
alter table reddit_drafts     enable row level security;

-- Seed the watchlist (idempotent). sort_order groups combined feeds so high-volume subs
-- (10-21) don't crowd out low-volume high-ICP niche subs (30-41), which get their own feed.
insert into reddit_subreddits (name, role, promo_tolerance, sort_order, poll_new, active) values
  -- group 1 — higher-volume / broad (share one combined feed)
  ('marketing',          'reach',     0.20, 10, true, true),
  ('digital_marketing',  'reach',     0.40, 11, true, true),
  ('advertising',        'primary',   0.20, 12, true, true),
  ('socialmedia',        'reach',     0.30, 13, true, true),
  ('SEO',                'primary',   0.30, 14, true, true),
  ('PPC',                'primary',   0.40, 15, true, true),
  ('webdev',             'reach',     0.30, 16, true, true),
  ('consulting',         'probe',     0.40, 17, true, true),
  ('sysadmin',           'probe',     0.50, 18, true, true),
  ('devops',             'probe',     0.50, 19, true, true),
  ('projectmanagement',  'primary',   0.40, 20, true, true),
  ('humanresources',     'informant', 0.50, 21, true, true),
  -- group 2 — low-volume / high-ICP-density niche (share a separate combined feed)
  ('agency',             'primary',   0.20, 30, true, true),
  ('agencylife',         'informant', 0.10, 31, true, true),
  ('PublicRelations',    'primary',   0.40, 32, true, true),
  ('content_marketing',  'primary',   0.40, 33, true, true),
  ('branding',           'primary',   0.40, 34, true, true),
  ('web_design',         'primary',   0.40, 35, true, true),
  ('bigseo',             'primary',   0.40, 36, true, true),
  ('freelance',          'informant', 0.30, 37, true, true),
  ('msp',                'probe',     0.60, 38, true, true),
  ('CustomerSuccess',    'primary',   0.50, 39, true, true),
  ('ProductManagement',  'probe',     0.40, 40, true, true),
  ('managementconsulting','probe',    0.40, 41, true, true)
on conflict (name) do nothing;
