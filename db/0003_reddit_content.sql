-- 0003 — storage for the weekly Authority + Intelligence engine (run in Supabase SQL editor).
-- Holds the drafted authority / build-in-public posts so there's a record + you can mark what
-- you actually posted. Idempotent.

create table if not exists reddit_content (
  id           uuid primary key default gen_random_uuid(),
  kind         text not null,                 -- authority_post | bip_post
  subreddit    text,                          -- suggested target subreddit
  title        text,
  body         text,
  grounded_on  text,                          -- the trend/quote it was anchored on
  week_of      date not null default (now()::date),
  status       text not null default 'drafted', -- drafted | edited | posted | skipped
  created_at   timestamptz not null default now()
);

alter table reddit_content enable row level security;
