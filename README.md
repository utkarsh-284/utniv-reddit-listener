# Reddit Listening Engine

Catches agency knowledge-loss conversations on Reddit *while they're still live*, scores
them, stores everything in Supabase, and posts a curated, ranked card to Slack so you can
show up early and help — by hand.

- **Plan:** `business/sales/reddit-listening-engine-plan-2026-06-26.md`
- **Reddit access:** public **RSS feeds** (`SOURCE_MODE=rss`, default) — **no app, no API key, no signup.**
  Reddit gated new Data API app creation (Jun 2026) and Devvit can't read subreddits you don't
  moderate, so RSS is the working path. If you ever get OAuth keys, set `SOURCE_MODE=oauth`.
- **Scoring:** deterministic math (free) + one LLM call (OpenAI → NVIDIA NIM fallback).
- **Cost:** ~$2–5/month.

> **RSS tradeoff:** the feed gives title, body, author, timestamp, subreddit — enough for phrase
> pre-filter, LLM relevance/pain scoring, freshness, and VoC capture. It does NOT expose live
> upvote/comment counts, so velocity/engagement scoring is auto-disabled and the composite
> re-weights onto ICP + pain + freshness. Reddit rate-limits RSS, so the fetcher throttles
> between subs and backs off on 429 (`RSS_DELAY_SECONDS`, `RSS_BACKOFF_SECONDS`).

```
reddit-listener/
  run.py                      # entry point (python run.py)
  config/  phrases.py         # ~60 pre-filter/search phrases
  listener/
    settings.py               # env + tunables
    reddit_client.py          # PRAW: poll /new (+ optional search)
    scoring.py                # deterministic + LLM (OpenAI→NIM)
    store.py                  # Supabase persistence + dedup + run log
    slack.py                  # Block Kit card → webhook
    pipeline.py               # orchestration (fail-open)
  db/0001_reddit_listener.sql   # SQL migration (folder is 'db' not 'supabase' to avoid
                                # shadowing the installed supabase python package)
# the cron workflow lives at repo root: .github/workflows/reddit-listener.yml
```

---

## Setup — do these once

### 1. Reddit access — nothing to do ✅
Default `SOURCE_MODE=rss` reads public RSS feeds with **no app, no API key, no signup**. This is
deliberate: Reddit gated new Data API app creation in 2026, and Devvit can't read subreddits you
don't moderate. RSS just works.

*(Only if you ever obtain Data API keys: set `SOURCE_MODE=oauth` and fill `REDDIT_CLIENT_ID` +
`REDDIT_CLIENT_SECRET` — plus `REDDIT_USERNAME`/`REDDIT_PASSWORD` if Reddit issues you an
app-account. Everything else is identical.)*

### 2. Get a Slack incoming webhook (≈5 min)
This is the URL the engine posts alert cards to.

1. Pick/create the Slack channel for alerts, e.g. **#reddit-signals**.
2. Go to **https://api.slack.com/apps** → **"Create New App"** → **"From scratch"**.
3. Name it `UTNIV Reddit Listener`, pick your workspace → **Create App**.
4. In the left menu, open **"Incoming Webhooks"** → toggle **Activate Incoming Webhooks → On**.
5. Click **"Add New Webhook to Workspace"** (bottom).
6. Choose the channel (**#reddit-signals**) → **Allow**.
7. Copy the **Webhook URL** (looks like `https://hooks.slack.com/services/T000/B000/xxxx`).
8. Put it in `.env` / GitHub secrets as `SLACK_WEBHOOK_URL`.

### 3. Supabase tables (≈2 min)
Same project as the scorecard; new `reddit_*` tables.

- In the Supabase dashboard → **SQL Editor** → paste the contents of
  `db/0001_reddit_listener.sql` → **Run**.
- Get `SUPABASE_URL` and the **service-role** key from **Project Settings → API**.
  Put them in `.env` / secrets (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`).
  ⚠️ The service-role key bypasses RLS — keep it server/CI-only, never in any frontend.

### 4. LLM keys
- `OPENAI_API_KEY` (your existing credit) — default model `gpt-4o-mini`.
- `NIM_API_KEY` + `NIM_BASE_URL` + `NIM_MODEL` — the free fallback (same as the scorecard's NIM).

---

## Run it

### Local pilot (recommended first)
```bash
cd reddit-listener
python -m venv .venv && . .venv/Scripts/activate     # Windows; use bin/activate on mac/linux
pip install -r requirements.txt
cp .env.example .env        # then fill in the values
python run.py
```
Check the Slack card and the `reddit_threads` / `reddit_runs` rows in Supabase. Tune
`ALERT_THRESHOLD` and the weights in `listener/settings.py` against what you see.

### Production (GitHub Actions, every 30 min)
1. Push the repo to GitHub.
2. Repo **Settings → Secrets and variables → Actions → New repository secret** for each:
   `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `OPENAI_API_KEY`, `OPENAI_MODEL`,
   `NIM_API_KEY`, `NIM_BASE_URL`, `NIM_MODEL`, `SLACK_WEBHOOK_URL`.
   (In **rss** mode the `REDDIT_*` secrets are **not needed**.)
   Optional **Variables**: `SOURCE_MODE` (default `rss`), `ALERT_THRESHOLD`, `MAX_AGE_HOURS`.

> **Note on rate limits & where it runs.** Reddit rate-limits RSS by IP. GitHub Actions runs from
> datacenter IPs, which Reddit throttles harder — the fetcher throttles + backs off, and because
> RSS feeds persist, the next 30-min run catches anything skipped. If GHA gets persistently 429'd,
> run the same `python run.py` from your own machine on a schedule (Windows Task Scheduler) — your
> residential IP is rarely throttled. Same code, same `.env`.
3. **Actions** tab → **reddit-listener** → **Run workflow** for a manual pilot run.
4. Once happy, the cron runs it automatically every 30 min.

---

## Which model does what
Two LLM jobs, two models — each matched to its task:
- **Scoring / triage** (`OPENAI_MODEL`, default `gpt-4o-mini`) — runs on every phrase-gated
  post. Cheap, fast, and proven accurate for this classification. NIM is the fallback.
- **Drafting / voice** (`DRAFT_MODEL`, default `gpt-5.4-mini`) — replies, follow-up digs, DM
  openers, per-run post ideas, and the weekly authority/BIP posts. Few calls per run, so a
  stronger model is worth it (~$1–2/mo). `gpt-5.x` is a reasoning model: the client auto-swaps
  `max_tokens`→`max_completion_tokens` and drops `temperature` (both would 400 otherwise).
  Optional `DRAFT_REASONING_EFFORT` (`low`/`medium`/`high`/`xhigh`; empty = fast default).

## How scoring works
- **Deterministic (free):** velocity (upvotes/hr), decay/freshness (favors <12h), engagement
  (comments/upvotes), per-sub promo tolerance, phrase hits.
- **LLM (only on phrase-matched posts):** ICP relevance, pain acuteness, trigger type, the
  best verbatim VoC quote, suggested action, one-line why.
- **Composite (0–100):** weighted blend (weights in `settings.py`). `>= ALERT_THRESHOLD`
  (default 65) → Slack. High-pain posts get a 🔥 flag even slightly under.
- Everything is stored regardless of threshold, so you can re-tune later against real data.

## Drafting (Mom Test engine)
Every reply-worthy thread gets a drafted engagement kit in the Slack card, built on
The Mom Test (talk about their life, specifics in the past, never pitch):
- **Draft comment** — varied shape per thread (opener rotation is keyed to the thread id,
  so drafts never share the same skeleton), grounded in the post's own words, ending in one
  genuine past-specific question. No links, no product mentions — enforced by regex too.
- **"If they reply, dig with"** — two deeper Mom-Test follow-ups (cost, last concrete time,
  what they tried) ready to paste when the OP responds.
- **DM opener** — an honest, no-pitch DM for after they engage, to land a real conversation.

Voice/rules live in `config/reddit_voice.py`; paste raw samples of your own writing into
`PERSONAL_SAMPLES` there to sharpen the voice further.

> **Paused:** the per-run "🎣 start a conversation" post suggestions are switched off — the
> ideas came out too generic/irrelevant. Each run now posts only the ICP alert cards. The
> code (`scoring.suggest_posts`, `slack.post_suggestions`) is left dormant for a future fix.
> The **weekly digest still drafts** authority + build-in-public posts (see below).

## Guardrails (UTNIV house rules)
- **Read-only.** No posting scope. Drafts land in Slack; you review, edit, and post by hand.
- **Fail-open.** If both LLM providers fail, the thread is stored with a deterministic-only
  score; the run never crashes and no data is lost.
- **Idempotent.** `reddit_id` unique constraint; re-polls refresh counts, never duplicate.
- **Secrets** live only in `.env` (gitignored) / GitHub secrets — never in code.
