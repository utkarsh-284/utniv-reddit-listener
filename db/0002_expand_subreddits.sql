-- 0002 — expand + diversify the watchlist (run this in the Supabase SQL editor).
-- Idempotent: safe to run more than once. Adds sort_order, 16 new subs, and groups
-- combined feeds so high-volume subs (10-21) don't crowd low-volume niche subs (30-41).

alter table reddit_subreddits add column if not exists sort_order int not null default 100;

-- group the original 8
update reddit_subreddits set sort_order = 10 where name = 'marketing';
update reddit_subreddits set sort_order = 11 where name = 'digital_marketing';
update reddit_subreddits set sort_order = 12 where name = 'advertising';
update reddit_subreddits set sort_order = 20 where name = 'projectmanagement';
update reddit_subreddits set sort_order = 30 where name = 'agency';
update reddit_subreddits set sort_order = 31 where name = 'agencylife';
update reddit_subreddits set sort_order = 32 where name = 'PublicRelations';
update reddit_subreddits set sort_order = 38 where name = 'msp';

-- add the 16 new subreddits (skip any that already exist)
insert into reddit_subreddits (name, role, promo_tolerance, sort_order, poll_new, active) values
  ('socialmedia',         'reach',   0.30, 13, true, true),
  ('SEO',                 'primary', 0.30, 14, true, true),
  ('PPC',                 'primary', 0.40, 15, true, true),
  ('webdev',              'reach',   0.30, 16, true, true),
  ('consulting',          'probe',   0.40, 17, true, true),
  ('sysadmin',            'probe',   0.50, 18, true, true),
  ('devops',              'probe',   0.50, 19, true, true),
  ('humanresources',      'informant',0.50,21, true, true),
  ('content_marketing',   'primary', 0.40, 33, true, true),
  ('branding',            'primary', 0.40, 34, true, true),
  ('web_design',          'primary', 0.40, 35, true, true),
  ('bigseo',              'primary', 0.40, 36, true, true),
  ('freelance',           'informant',0.30,37, true, true),
  ('CustomerSuccess',     'primary', 0.50, 39, true, true),
  ('ProductManagement',   'probe',   0.40, 40, true, true),
  ('managementconsulting','probe',   0.40, 41, true, true)
on conflict (name) do nothing;
